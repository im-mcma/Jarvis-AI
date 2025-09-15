# app.py

from datetime import timedelta
import os
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Dict, Any, AsyncGenerator

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from google.cloud import firestore
from google.oauth2 import service_account
from pydantic import BaseModel
from cachetools import TTLCache

# --- 1. Basic Configuration & Constants ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

USER_ID = "main_user"
CREDENTIALS_FILE = "credentials.json"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# --- 2. FastAPI App Initialization ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Jarvis v8.0 - Prestige Edition is starting up...")
    yield
    logger.info("🛑 Jarvis v8.0 is shutting down...")

app = FastAPI(
    title="Jarvis v8.0 - Prestige Edition",
    description="A completely rebuilt, professional-grade AI chat system with a superior UI/UX.",
    version="8.0.0",
    lifespan=lifespan
)
templates = Jinja2Templates(directory=".")

# --- 3. Firestore & Cache Initialization ---
db = None
try:
    if not GEMINI_API_KEY:
        logger.critical("FATAL: GEMINI_API_KEY environment variable is not set.")
        raise ValueError("GEMINI_API_KEY is missing.")
    
    if os.path.exists(CREDENTIALS_FILE):
        db = firestore.AsyncClient.from_service_account_json(CREDENTIALS_FILE)
        logger.info(f"✅ Firestore connected successfully via '{CREDENTIALS_FILE}'.")
    elif "GOOGLE_CREDENTIALS_JSON" in os.environ:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        credentials = service_account.Credentials.from_service_account_info(creds_json)
        db = firestore.AsyncClient(credentials=credentials, project=credentials.project_id)
        logger.info("✅ Firestore connected successfully via environment variable.")
    else:
        logger.warning("⚠️ Firestore credentials not found. Database features will be disabled.")
except Exception as e:
    logger.error(f"❌ Firestore connection failed: {e}", exc_info=True)
    db = None

cache = TTLCache(maxsize=100, ttl=timedelta(minutes=5).total_seconds())

# --- 4. Gemini Models Configuration ---
MODELS = {
    "gemini-2.5-pro": {"name": "🚀 Gemini 2.5 Pro", "description": "استدلال پیچیده، کدنویسی، درک چندوجهی"},
    "gemini-2.5-flash": {"name": "⚡️ Gemini 2.5 Flash", "description": "تفکر تطبیقی، کارایی هزینه‌ای"},
    "gemini-2.5-flash-lite": {"name": "💨 Gemini 2.5 Flash-Lite", "description": "توان عملیاتی بالا، مقرون‌به‌صرفه‌ترین"},
    "gemini-2.0-flash": {"name": "✨ Gemini 2.0 Flash", "description": "ویژگی‌های نسل بعدی، سرعت، استریم لحظه‌ای"},
    "gemini-2.0-flash-lite": {"name": "🍃 Gemini 2.0 Flash-Lite", "description": "کارایی هزینه‌ای و تأخیر کم"},
    "gemini-live-2.5-flash-preview": {"name": "🔴 Gemini 2.5 Flash Live", "description": "مکالمات صوتی و تصویری دوطرفه"},
    "gemini-2.5-flash-preview-native-audio-dialog": {"name": "🗣️ Gemini 2.5 Native Audio", "description": "خروجی‌های صوتی مکالمه‌ای طبیعی"},
    "gemini-2.0-flash-preview-image-generation": {"name": "🎨 Gemini 2.0 Image Gen", "description": "تولید و ویرایش مکالمه‌ای تصاویر"}
}

# --- 5. Pydantic Data Models ---
class MessagePart(BaseModel):
    text: str

class Message(BaseModel):
    role: str
    parts: List[MessagePart]

class WebSocketRequest(BaseModel):
    type: str
    conversation_id: str | None = None
    model: str
    message: Message

class ConversationTitleRequest(BaseModel):
    conversation_id: str
    first_message: str

# --- 6. Core AI and Database Logic ---
async def gemini_chat_stream(messages: List[Dict], model: str, stop_event: asyncio.Event) -> AsyncGenerator[str, None]:
    headers = {
        "Content-Type": "application/json"
    }
    payload = {
        "contents": messages,
        "generationConfig": {
            "candidateCount": 1
        }
    }
    params = {
        "key": GEMINI_API_KEY
    }
    
    url = f"{GEMINI_API_BASE_URL}/{model}:streamGenerateContent"
    
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers, params=params, timeout=timeout) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if stop_event.is_set():
                        break
                    decoded_chunk = chunk.decode('utf-8')
                    if not decoded_chunk.startswith("["):
                        decoded_chunk = "[" + decoded_chunk
                    if not decoded_chunk.endswith("]"):
                        decoded_chunk = decoded_chunk + "]"

                    try:
                        data = json.loads(decoded_chunk)
                        if data and "candidates" in data[0] and "parts" in data[0]["candidates"][0] and "text" in data[0]["candidates"][0]["parts"][0]:
                            text_content = data[0]["candidates"][0]["parts"][0]["text"]
                            yield text_content
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON decode error: {e}")
                        logger.debug(f"Malformed chunk: {decoded_chunk}")
                        
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error during stream: {e}", exc_info=True)
            yield f"ERROR: HTTP error: {e.response.text}"
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}", exc_info=True)
            yield f"ERROR: Unexpected error: {e}"


async def generate_title_for_conversation(user_id: str, conversation_id: str, first_message: str):
    if not db:
        logger.warning("Database not available, skipping title generation.")
        return

    prompt_messages = [
        {"role": "user", "parts": [{"text": f"Create a short, descriptive title (5 words max) for a conversation that starts with: '{first_message}'\n\nTitle:"}]},
        {"role": "model", "parts": [{"text": "AI Chat System"}]}
    ]
    try:
        response = ""
        async for chunk in gemini_chat_stream(prompt_messages, "gemini-2.5-flash-lite", asyncio.Event()):
            response += chunk
        
        if response.startswith("ERROR"):
            logger.error(f"Failed to generate title: {response}")
            return
            
        title = response.strip().replace("\"", "").replace(".", "")
        if title:
            doc_ref = db.collection("users").document(user_id).collection("conversations").document(conversation_id)
            await doc_ref.update({"title": title})
            logger.info(f"Generated and saved title '{title}' for conversation {conversation_id}")
    except Exception as e:
        logger.error(f"Error generating or saving title: {e}", exc_info=True)


async def get_messages_from_db(conversation_id: str) -> List[Dict]:
    if not db:
        return []
    
    try:
        doc_ref = db.collection("users").document(USER_ID).collection("conversations").document(conversation_id)
        doc = await doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            return data.get("messages", [])
        return []
    except Exception as e:
        logger.error(f"Error fetching messages: {e}", exc_info=True)
        return []

async def get_conversations_from_db() -> List[Dict]:
    if not db:
        return []
        
    try:
        conversations_ref = db.collection("users").document(USER_ID).collection("conversations")
        docs = await conversations_ref.order_by("created_at", direction=firestore.Query.DESCENDING).limit(50).stream()
        conversations = []
        async for doc in docs:
            data = doc.to_dict()
            conversations.append({"id": doc.id, "title": data.get("title", "مکالمه جدید")})
        return conversations
    except Exception as e:
        logger.error(f"Error fetching conversations: {e}", exc_info=True)
        return []

# --- 7. API Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "models": MODELS})

@app.get("/health", status_code=200)
async def health_check():
    return {"status": "ok"}
    
@app.get("/api/models")
async def get_models():
    return JSONResponse(content={"models": MODELS})

@app.get("/api/conversations")
async def get_conversations():
    conversations = await get_conversations_from_db()
    return JSONResponse(content={"conversations": conversations})

@app.get("/api/conversations/{conversation_id}")
async def get_conversation_details(conversation_id: str):
    messages = await get_messages_from_db(conversation_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JSONResponse(content={"messages": messages})

# --- 8. WebSocket Main Handler ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket, background_tasks: BackgroundTasks):
    await websocket.accept()
    stop_event = asyncio.Event()
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            request = WebSocketRequest(**data)
            
            # Stop any ongoing generation
            if request.type == "stop":
                stop_event.set()
                await websocket.send_json({"type": "status", "message": "Generation stopped."})
                continue
            
            # New chat or continue existing one
            conversation_id = request.conversation_id
            if not conversation_id:
                conversation_id = str(uuid.uuid4())
                await websocket.send_json({"type": "info", "conversation_id": conversation_id})
            
            user_message_dict = request.message.dict()
            db_messages = await get_messages_from_db(conversation_id)
            db_messages.append(user_message_dict)
            
            if db:
                doc_ref = db.collection("users").document(USER_ID).collection("conversations").document(conversation_id)
                await doc_ref.set({"created_at": firestore.SERVER_TIMESTAMP}, merge=True)
                await doc_ref.update({"messages": firestore.ArrayUnion([user_message_dict])})
                
            full_assistant_message = ""
            for chunk in gemini_chat_stream(db_messages, request.model, stop_event):
                if chunk.startswith("ERROR"):
                    await websocket.send_text(chunk)
                    full_assistant_message = "" # Reset to prevent saving an error message
                    break
                await websocket.send_text(chunk)
                full_assistant_message += chunk
            
            if full_assistant_message:
                assistant_message_dict = {
                    "role": "model",
                    "parts": [{"text": full_assistant_message}]
                }
                if db:
                    await doc_ref.update({"messages": firestore.ArrayUnion([assistant_message_dict])})
            
            # Generate title in background
            if len(db_messages) == 1 and db:
                background_tasks.add_task(generate_title_for_conversation, USER_ID, conversation_id, user_message_dict["parts"][0]["text"])

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected.")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        await websocket.close(code=1011, reason=f"An error occurred: {e}")
