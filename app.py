import os
import json
import uuid
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
from dotenv import load_dotenv

# --- Load environment variables ---
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# WARNING: This is a placeholder for a single user.
# In a production app, implement a proper authentication system
# to assign a unique user ID to each user.
USER_ID = "main_user"

# --- Logging setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI app setup ---
app = FastAPI(title="Jarvis AI Chat - MongoDB Streaming")
templates = Jinja2Templates(directory="templates")

# --- MongoDB Setup ---
client = AsyncIOMotorClient(MONGO_URI)
db = client["chat_ai_db"]
USERS_COLLECTION = db["users"]

# --- Database Functions ---
async def save_message(user_id: str, conversation_id: str, message: Dict[str, Any]):
    """Saves a single message to a conversation."""
    await USERS_COLLECTION.update_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"$push": {"conversations.$.messages": message}},
        upsert=True
    )
    
async def create_new_conversation(user_id: str, conversation_id: str):
    """Creates a new conversation document for a user."""
    await USERS_COLLECTION.update_one(
        {"_id": user_id},
        {"$push": {"conversations": {"_id": conversation_id, "title": "مکالمه جدید...", "created_at": datetime.utcnow(), "messages": []}}},
        upsert=True
    )

async def get_messages(user_id: str, conversation_id: str) -> List[Dict[str, Any]]:
    """Retrieves all messages for a specific conversation."""
    user_doc = await USERS_COLLECTION.find_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"conversations.$": 1}
    )
    if user_doc and "conversations" in user_doc and user_doc["conversations"]:
        return user_doc["conversations"][0].get("messages", [])
    return []

async def get_conversations(user_id: str) -> List[Dict[str, Any]]:
    """Retrieves a list of all conversations for a user."""
    user_doc = await USERS_COLLECTION.find_one({"_id": user_id})
    if not user_doc or "conversations" not in user_doc:
        return []
    return [{"id": conv["_id"], "title": conv.get("title", "مکالمه جدید...")} for conv in user_doc["conversations"]]

async def delete_conversation(user_id: str, conversation_id: str):
    """Deletes a specific conversation."""
    await USERS_COLLECTION.update_one(
        {"_id": user_id},
        {"$pull": {"conversations": {"_id": conversation_id}}}
    )

# --- Gemini Chat Streaming ---
async def gemini_chat_stream(messages: List[Dict[str, Any]], model: str, stop_event: asyncio.Event):
    """
    Streams the response from the Gemini API, handling JSON chunking.
    """
    if not GEMINI_API_KEY:
        yield {"type": "error", "content": "Gemini API key is not configured."}
        return

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {"contents": messages}

    timeout = httpx.Timeout(120.0, connect=10.0)
    decoder = json.JSONDecoder()
    
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    yield {"type": "error", "content": f"API Error: {response.status_code} - {error_body.decode()}"}
                    return

                buffer = ""
                async for chunk in response.aiter_bytes():
                    if stop_event.is_set():
                        break
                    
                    buffer += chunk.decode("utf-8", errors="ignore")
                    
                    # Process as many JSON objects as possible from the buffer
                    while True:
                        try:
                            # Attempt to decode a full JSON object from the buffer
                            data, idx = decoder.raw_decode(buffer.strip())
                            text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            
                            if text:
                                yield {"type": "chunk", "content": text}
                            
                            # Remove the processed part from the buffer
                            buffer = buffer[idx:].strip()
                            
                        except json.JSONDecodeError:
                            # Not a complete JSON object yet, break and wait for more data
                            break
                            
        except Exception as e:
            yield {"type": "error", "content": f"Streaming failed: {str(e)}"}

# --- FastAPI Routes ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
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
    
    generation_task: asyncio.Task = None
    stop_event = asyncio.Event()

    async def chat_generator_task(message_text: str, conversation_id: str):
        """Task to handle the full chat generation process."""
        try:
            # 1. If it's a new conversation, create it first
            if not conversation_id:
                conversation_id = str(uuid.uuid4())
                await create_new_conversation(USER_ID, conversation_id)
                await websocket.send_json({"type": "info", "conversation_id": conversation_id})

            # 2. Get previous messages to maintain context
            conversation_history = await get_messages(USER_ID, conversation_id)
            
            # Prepare messages for the Gemini API (remove custom fields like 'timestamp')
            messages_for_api = [
                {"role": msg["role"], "parts": msg["parts"]}
                for msg in conversation_history
            ]
            
            user_message = {"role": "user", "parts": [{"text": message_text}]}
            messages_for_api.append(user_message)
            
            # 3. Save the new user message to the database
            user_message_with_ts = {"role": "user", "parts": [{"text": message_text}], "timestamp": datetime.utcnow()}
            await save_message(USER_ID, conversation_id, user_message_with_ts)

            # 4. Stream the response and build the full message
            full_assistant_message = ""
            async for result in gemini_chat_stream(messages_for_api, "gemini-1.5-pro-latest", stop_event):
                if result["type"] == "chunk":
                    await websocket.send_text(result["content"])
                    full_assistant_message += result["content"]
                elif result["type"] == "error":
                    await websocket.send_json({"type": "error", "message": result["content"]})
                    # Break the loop on a critical error
                    break
            
            # 5. Save the complete assistant message
            assistant_message = {"role": "model", "parts": [{"text": full_assistant_message}], "timestamp": datetime.utcnow()}
            await save_message(USER_ID, conversation_id, assistant_message)
            
            # 6. Signal the end of the stream
            await websocket.send_json({"type": "stream_end"})
            
        except asyncio.CancelledError:
            # Task was cancelled, log and exit gracefully
            logger.info("Chat generation task was cancelled.")
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
            await websocket.send_json({"type": "error", "message": "An unexpected server error occurred."})

    try:
        while True:
            data_raw = await websocket.receive_text()
            data = json.loads(data_raw)
            
            if data.get("type") == "stop":
                stop_event.set()
                if generation_task and not generation_task.done():
                    # Wait a short while for the task to finish gracefully
                    try:
                        await asyncio.wait_for(generation_task, timeout=2)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        logger.info("Generation task stopped and cancelled forcefully.")
                continue
                
            if data.get("type") == "chat":
                message_text = data["message"]["parts"][0]["text"]
                conversation_id = data.get("conversation_id")
                
                # If a previous task is still running, stop it first
                if generation_task and not generation_task.done():
                    stop_event.set()
                    try:
                        # Wait for the previous task to complete or be cancelled
                        await asyncio.wait_for(generation_task, timeout=5)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        logger.warning("Previous task did not complete in time, cancelled.")
                
                # Clear the stop event for the new task and start it
                stop_event.clear()
                generation_task = asyncio.create_task(chat_generator_task(message_text, conversation_id))

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected.")
        stop_event.set()
        if generation_task and not generation_task.done():
            generation_task.cancel()
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
