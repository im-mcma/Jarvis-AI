import os
import json
import uuid
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
from dotenv import load_dotenv

# --- Load environment ---
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI app ---
app = FastAPI(title="Jarvis AI Chat - MongoDB Streaming")
templates = Jinja2Templates(directory="templates")

# --- MongoDB Setup ---
client = AsyncIOMotorClient(MONGO_URI)
db = client["chat_ai_db"]
USERS_COLLECTION = db["users"]

# --- Database Functions ---
async def save_message(user_id: str, conversation_id: str, message: dict):
    convo = await USERS_COLLECTION.find_one({"_id": user_id, "conversations._id": conversation_id})
    if not convo:
        await USERS_COLLECTION.update_one(
            {"_id": user_id},
            {"$push": {"conversations": {"_id": conversation_id, "title": "مکالمه جدید...", "created_at": datetime.utcnow(), "messages": []}}},
            upsert=True
        )
    await USERS_COLLECTION.update_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"$push": {"conversations.$.messages": message}}
    )

async def get_messages(user_id: str, conversation_id: str):
    user_doc = await USERS_COLLECTION.find_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"conversations.$": 1}
    )
    if user_doc and "conversations" in user_doc:
        return user_doc["conversations"][0].get("messages", [])
    return []

async def get_conversations(user_id: str):
    user_doc = await USERS_COLLECTION.find_one({"_id": user_id})
    if not user_doc or "conversations" not in user_doc:
        return []
    return [{"id": conv["_id"], "title": conv.get("title", "مکالمه جدید...")} for conv in user_doc["conversations"]]

async def delete_conversation(user_id: str, conversation_id: str):
    await USERS_COLLECTION.update_one(
        {"_id": user_id},
        {"$pull": {"conversations": {"_id": conversation_id}}}
    )

# --- Gemini Chat Streaming ---
async def gemini_chat_stream(messages, model, stop_event):
    """
    Stream پاسخ از Gemini API.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {"contents": messages}

    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    yield {"type": "error", "content": f"API Error: {error_body.decode()}"}
                    return

                buffer = ""
                async for chunk in response.aiter_bytes():
                    if stop_event.is_set():
                        break
                    buffer += chunk.decode("utf-8", errors="ignore")
                    if buffer.strip():
                        try:
                            data = json.loads(buffer)
                            text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            if text:
                                yield {"type": "chunk", "content": text}
                                buffer = ""
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield {"type": "error", "content": str(e)}

# --- FastAPI Routes ---
@app.get("/", response_class=HTMLResponse)
async def root(request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/conversations")
async def api_get_conversations():
    convs = await get_conversations(USER_ID)
    return JSONResponse(content={"conversations": convs})

@app.get("/api/conversations/{conversation_id}/messages")
async def api_get_messages(conversation_id: str):
    msgs = await get_messages(USER_ID, conversation_id)
    return JSONResponse(content={"messages": msgs})

@app.delete("/api/conversations/{conversation_id}")
async def api_delete_conversation(conversation_id: str):
    await delete_conversation(USER_ID, conversation_id)
    return JSONResponse(content={"status": "deleted"})

# --- WebSocket Endpoint ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    stop_event = asyncio.Event()
    generation_task = None

    async def chat_generator_task(message_text, conversation_id):
        stop_event.clear()
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
            await websocket.send_json({"type": "info", "conversation_id": conversation_id})

        user_message = {"role": "user", "parts": [{"text": message_text}], "timestamp": datetime.utcnow()}
        await save_message(USER_ID, conversation_id, user_message)

        full_assistant_message = ""
        async for result in gemini_chat_stream([user_message], "gemini-1.5-pro-latest", stop_event):
            if result["type"] == "chunk":
                await websocket.send_text(result["content"])
                full_assistant_message += result["content"]
            elif result["type"] == "error":
                await websocket.send_json({"type": "error", "message": result["content"]})

        assistant_message = {"role": "model", "parts": [{"text": full_assistant_message}], "timestamp": datetime.utcnow()}
        await save_message(USER_ID, conversation_id, assistant_message)
        await websocket.send_json({"type": "stream_end"})

    try:
        while True:
            data_raw = await websocket.receive_text()
            data = json.loads(data_raw)
            if data.get("type") == "stop":
                stop_event.set()
                if generation_task and not generation_task.done():
                    generation_task.cancel()
                continue
            if data.get("type") == "chat":
                message_text = data["message"]["parts"][0]["text"]
                conversation_id = data.get("conversation_id")
                if generation_task and not generation_task.done():
                    stop_event.set()
                    generation_task.cancel()
                generation_task = asyncio.create_task(chat_generator_task(message_text, conversation_id))

    except WebSocketDisconnect:
        stop_event.set()
        if generation_task and not generation_task.done():
            generation_task.cancel()
