# main.py
import os
import asyncio
import uuid
import json
import logging
from datetime import timedelta
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from google.cloud import firestore
from google.oauth2 import service_account
from pydantic import BaseModel
from typing import List, Dict, Any, AsyncGenerator
from cachetools import TTLCache

# --- 1. Basic Configuration & Logging ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 2. FastAPI App Initialization ---
app = FastAPI(
    title="Jarvis v4.0 - Phoenix Edition",
    description="A completely refactored, high-performance AI chat system based on a detailed professional prompt.",
    version="4.0.0"
)
templates = Jinja2Templates(directory="templates")

# --- 3. Firestore & Cache Initialization ---
try:
    if "GOOGLE_CREDENTIALS_JSON" in os.environ:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        credentials = service_account.Credentials.from_service_account_info(creds_json)
        db = firestore.AsyncClient(credentials=credentials, project=credentials.project_id)
        logger.info("✅ Firestore connected successfully via environment variable.")
    else:
        # Fallback for local development
        db = firestore.AsyncClient()
        logger.info("✅ Firestore connected successfully via local credentials file.")
except Exception as e:
    logger.error(f"❌ Firestore connection failed: {e}", exc_info=True)
    db = None

# Cache for conversation lists with a 5-minute TTL
cache = TTLCache(maxsize=100, ttl=timedelta(minutes=5).total_seconds())

# --- 4. Gemini Models Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
USER_ID = "main_user" # For single-user application

MODELS = {
    "gemini-1.5-pro-latest": {
        "name": "🧠 Gemini 1.5 Pro", "type": "multimodal",
        "features": ["system_instruction", "long_context", "video_input"]
    },
    "gemini-1.5-flash-latest": {
        "name": "⚡️ Gemini 1.5 Flash", "type": "multimodal",
        "features": ["system_instruction", "fast_response"]
    },
    "gemini-1.0-pro": {
        "name": "💎 Gemini 1.0 Pro", "type": "chat",
        "features": []
    },
}

# --- 5. Pydantic Data Models ---
class MessagePart(BaseModel):
    text: str

class Message(BaseModel):
    role: str
    parts: List[MessagePart]

class WebSocketRequest(BaseModel):
    type: str = "chat"
    conversation_id: str
    model: str
    message: Message

# --- 6. Advanced Stream Management ---
async def gemini_chat_stream(messages: List, model: str, stop_event: asyncio.Event) -> AsyncGenerator[Dict, None]:
    api_url = f"{GEMINI_API_BASE_URL}/{model}:streamGenerateContent?key={GEMINI_API_KEY}"
    
    model_info = MODELS.get(model, {})
    payload_contents = messages
    
    if "system_instruction" in model_info.get("features", []):
        system_instruction = {"role": "system", "parts": [{"text": "You are Jarvis, a helpful AI assistant. Always respond in Persian unless instructed otherwise. Use Markdown for formatting."}]}
        payload_contents = [system_instruction] + messages

    payload = {"contents": payload_contents}
    
    buffer = b""
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            async with client.stream("POST", api_url, json=payload) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if stop_event.is_set():
                        logger.info("Stream stop requested by client.")
                        break
                    
                    buffer += chunk
                    # Process complete messages from the buffer
                    while b'\n\n' in buffer:
                        message_part, buffer = buffer.split(b'\n\n', 1)
                        if message_part.startswith(b'data: '):
                            json_str = message_part[6:].decode('utf-8')
                            try:
                                yield json.loads(json_str)
                            except json.JSONDecodeError:
                                logger.warning(f"Could not decode JSON chunk: {json_str}")
        except httpx.HTTPStatusError as e:
            error_details = e.response.json()
            logger.error(f"Gemini API HTTP Error: {error_details}")
            yield {"error": error_details.get("error", {}).get("message", "Unknown API error")}
        except Exception as e:
            logger.error(f"Stream Error: {e}", exc_info=True)
            yield {"error": "An unexpected error occurred during streaming."}

# --- 7. Database Optimization Functions ---
async def get_messages_from_db(conversation_id: str) -> List[Dict]:
    if not db: return []
    # Fetch only the last 20 messages for context efficiency
    docs_query = db.collection("users").document(USER_ID).collection("conversations").document(conversation_id).collection("messages").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(20)
    docs = await docs_query.stream()
    messages = [doc.to_dict() for doc in docs]
    messages.reverse() # Restore chronological order
    return messages

async def get_conversations_from_db() -> List[Dict]:
    cache_key = f"convs_{USER_ID}"
    if cache_key in cache:
        return cache[cache_key]

    if not db: return []
    convs_ref = db.collection("users").document(USER_ID).collection("conversations").order_by("last_updated", direction=firestore.Query.DESCENDING)
    docs = await convs_ref.stream()
    conversations = []
    for doc in docs:
        conv_data = doc.to_dict()
        conversations.append({
            "id": doc.id,
            "title": conv_data.get("title", "گفتگوی جدید"),
            "last_message_snippet": conv_data.get("last_message_snippet", "")
        })
    cache[cache_key] = conversations
    return conversations

# --- 8. API Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "models": MODELS})

@app.get("/api/conversations")
async def list_conversations():
    if not db: raise HTTPException(status_code=503, detail="Database not connected")
    convs = await get_conversations_from_db()
    return JSONResponse(convs)

@app.post("/api/conversations/{conversation_id}/title")
async def generate_conversation_title(conversation_id: str, background_tasks: BackgroundTasks):
    # This endpoint now schedules a background task for immediate response
    background_tasks.add_task(update_conversation_title, conversation_id)
    return JSONResponse({"status": "Title generation started."}, status_code=202)

async def update_conversation_title(conversation_id: str):
    logger.info(f"Generating title for conversation {conversation_id}...")
    # This function now runs in the background
    # It would call Gemini with a prompt to generate a title, then update Firestore.
    # ... (Implementation similar to previous versions)

# --- 9. WebSocket Main Handler ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    stop_stream_event = asyncio.Event()

    try:
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "stop":
                stop_stream_event.set()
                continue

            req = WebSocketRequest(**data)
            stop_stream_event.clear()

            # Save user message and update conversation document with last message snippet
            conv_ref = db.collection("users").document(USER_ID).collection("conversations").document(req.conversation_id)
            user_message_text = req.message.parts[0].text
            await conv_ref.collection("messages").add(req.message.dict())
            await conv_ref.update({
                "last_updated": firestore.SERVER_TIMESTAMP,
                "last_message_snippet": f"شما: {user_message_text[:50]}..."
            })
            cache.pop(f"convs_{USER_ID}", None) # Invalidate cache

            history = await get_messages_from_db(req.conversation_id)
            
            full_model_response = ""
            async for data_chunk in gemini_chat_stream(history, req.model, stop_stream_event):
                await websocket.send_json(data_chunk)
                if "error" not in data_chunk:
                    try:
                        full_model_response += data_chunk["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError):
                        pass

            # Save complete model response and update snippet
            if full_model_response:
                model_message = {"role": "model", "parts": [{"text": full_model_response}]}
                await conv_ref.collection("messages").add(model_message)
                await conv_ref.update({
                    "last_updated": firestore.SERVER_TIMESTAMP,
                    "last_message_snippet": f"جارویس: {full_model_response[:50]}..."
                })
                cache.pop(f"convs_{USER_ID}", None)

    except WebSocketDisconnect:
        logger.info("Client disconnected.")
    except Exception as e:
        logger.error(f"WebSocket Error: {e}", exc_info=True)
        try:
            await websocket.send_json({"error": "An unexpected server error occurred."})
        except:
            pass
