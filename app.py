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
from fastapi.middleware.security import SecurityHeadersMiddleware
from dotenv import load_dotenv
from google.cloud import firestore
from google.oauth2 import service_account
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from cachetools import TTLCache

# --- Basic Configuration ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- App Initialization ---
app = FastAPI(
    title="Jarvis-Ai v3.0 - Ultimate Edition",
    description="A visually stunning, production-ready AI chat assistant with advanced features.",
    version="3.0.0"
)
# Add basic security headers
app.add_middleware(SecurityHeadersMiddleware)
templates = Jinja2Templates(directory="templates")

# --- Firestore Initialization ---
try:
    if "GOOGLE_CREDENTIALS_JSON" in os.environ:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        credentials = service_account.Credentials.from_service_account_info(creds_json)
        db = firestore.AsyncClient(credentials=credentials, project=credentials.project_id)
        logger.info("✅ Firestore connected successfully via environment variable.")
    else:
        db = firestore.AsyncClient()
        logger.info("✅ Firestore connected successfully via local credentials file.")
except Exception as e:
    logger.error(f"❌ Firestore connection failed: {e}", exc_info=True)
    db = None

# --- Cache Configuration ---
cache = TTLCache(maxsize=100, ttl=timedelta(minutes=5).total_seconds())

# --- Gemini / AI Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.warning("⚠️ GEMINI_API_KEY environment variable not set.")

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# The definitive list of free-tier models available via the Generative Language API
# Note: The Google AI Studio UI may show more models that are experimental or not available through this specific API.
MODELS = {
    "gemini-1.5-pro-latest": {"name": "🧠 Gemini 1.5 Pro (Multimodal)", "type": "chat"},
    "gemini-1.5-flash-latest": {"name": "⚡️ Gemini 1.5 Flash (Multimodal)", "type": "chat"},
    "gemini-1.0-pro": {"name": "💎 Gemini 1.0 Pro", "type": "chat"},
    "embedding-001": {"name": "🔡 Embedding-001 (Text)", "type": "embedding"},
    # The 'aqa' model is specialized and requires a different request format.
    # It's better to handle it with a separate function if used extensively.
    # For now, we focus on chat models.
}
USER_ID = "main_user"

# --- Pydantic Models ---
class MessagePart(BaseModel):
    text: str

class Message(BaseModel):
    role: str
    parts: List[MessagePart]

class GenerationConfig(BaseModel):
    temperature: float = 0.7

class WebSocketRequest(BaseModel):
    type: str = "chat" # New field for different request types like 'stop'
    conversation_id: str
    model: str
    message: Message
    generation_config: GenerationConfig

# --- WebSocket Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_json(self, data: dict, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_json(data)
            
    async def send_text(self, text: str, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(text)

manager = ConnectionManager()

# --- Gemini API Logic ---
async def gemini_chat_stream(messages: List, model: str, config: GenerationConfig, stop_event: asyncio.Event):
    api_url = f"{GEMINI_API_BASE_URL}/{model}:streamGenerateContent?key={GEMINI_API_KEY}"
    system_instruction = {
        "role": "system",
        "parts": [{"text": "You are a helpful assistant named Jarvis. All your responses must be in Persian unless the user explicitly asks for another language. Structure your responses with Markdown for readability."}]
    }
    payload = {
        "contents": [system_instruction] + messages,
        "generationConfig": config.dict()
    }
    
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            async with client.stream("POST", api_url, json=payload) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if stop_event.is_set():
                        logger.info("Stop event received, halting stream.")
                        break
                    yield chunk
        except Exception as e:
            logger.error(f"❌ Gemini API Error: {e}", exc_info=True)
            error_data = {"error": f"An error occurred with the Gemini API: {str(e)}"}
            yield f'data: {json.dumps(error_data)}\n\n'.encode('utf-8')

# --- Main WebSocket Handler ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    client_id = str(uuid.uuid4())
    await manager.connect(websocket, client_id)
    stop_stream = asyncio.Event()

    try:
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "stop":
                logger.info(f"Client {client_id} requested to stop generation.")
                stop_stream.set()
                continue
            
            # Reset stop event for new message
            stop_stream.clear()
            
            req = WebSocketRequest(**data)
            if not db:
                await manager.send_json({"error": "Database not connected"}, client_id)
                continue

            # 1. Save user message and update conversation timestamp
            user_msg_doc = {
                "role": req.message.role, "parts": [p.dict() for p in req.message.parts],
                "timestamp": firestore.SERVER_TIMESTAMP, "model": req.model
            }
            conv_ref = db.collection("users").document(USER_ID).collection("conversations").document(req.conversation_id)
            await conv_ref.collection("messages").add(user_msg_doc)
            await conv_ref.update({"last_updated": firestore.SERVER_TIMESTAMP})
            cache.pop(f"convs_{USER_ID}", None) # Invalidate cache

            # 2. Get history and manage context
            history = await get_messages_from_db(req.conversation_id)
            # (Optional: Add background task for summarization here if needed)

            # 3. Send "thinking" status and start streaming
            await manager.send_json({"status": "generating", "model": req.model}, client_id)
            
            full_model_response = ""
            async for chunk in gemini_chat_stream(history, req.model, req.generation_config, stop_stream):
                # Forward chunk to client
                await manager.send_text(chunk.decode('utf-8'), client_id)
                # Accumulate full response
                try:
                    cleaned_chunk = chunk.decode('utf-8').replace('data: ', '').strip()
                    if cleaned_chunk:
                        json_data = json.loads(cleaned_chunk)
                        if "candidates" in json_data:
                            full_model_response += json_data["candidates"][0]["content"]["parts"][0]["text"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

            # 4. Save complete model response
            if full_model_response:
                model_msg_doc = {
                    "role": "model", "parts": [{"text": full_model_response}],
                    "timestamp": firestore.SERVER_TIMESTAMP, "model": req.model
                }
                await conv_ref.collection("messages").add(model_msg_doc)
            
            await manager.send_json({"status": "done"}, client_id)

    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected.")
    except Exception as e:
        logger.error(f"An unexpected error occurred with client {client_id}: {e}", exc_info=True)
        await manager.send_json({"error": "An unexpected server error occurred."}, client_id)
    finally:
        manager.disconnect(client_id)

# --- (Other API routes like /api/conversations, /share, etc. can be kept from v2.0) ---
# --- For brevity, they are omitted here but should be included in your final file. ---
# Make sure to also include the `get_conversations_from_db`, `get_messages_from_db`, `generate_title_for_conversation`
# and the corresponding API endpoints `/`, `/api/conversations`, etc. from the previous version.
