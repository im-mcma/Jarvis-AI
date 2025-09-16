# app.py

from datetime import timedelta
import os
import json
import asyncio
import logging
import uuid # <-- بهبود: ایمپورت کردن ماژول فراموش شده
from contextlib import asynccontextmanager
from typing import List, Dict, Any, AsyncGenerator

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account
from pydantic import BaseModel
from cachetools import TTLCache

# --- 1. Basic Configuration & Constants ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

USER_ID = "main_user"
CREDENTIALS_FILE = "credentials.json"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.critical("FATAL: GEMINI_API_KEY environment variable is not set.")
    raise ValueError("GEMINI_API_KEY is missing.")

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# بهبود: تعریف نام collection ها به عنوان ثابت
USERS_COLLECTION = "users"
CONVERSATIONS_COLLECTION = "conversations"
MESSAGES_COLLECTION = "messages"


# --- 2. FastAPI App Initialization ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Jarvis v8.1 - Prestige Pro is starting up...")
    yield
    logger.info("🛑 Jarvis v8.1 is shutting down...")

app = FastAPI(
    title="Jarvis v8.1 - Prestige Pro",
    description="A professional-grade AI chat system with an improved UI/UX and scalable backend.",
    version="8.1.0",
    lifespan=lifespan
)
templates = Jinja2Templates(directory=".")

# --- 3. Firestore & Cache Initialization ---
db = None
try:
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

# --- 4. Gemini Models Configuration (Unchanged as requested) ---
MODELS = {
    "gemini-1.5-pro-latest": {"name": "🚀 Gemini 1.5 Pro", "description": "استدلال پیچیده، کدنویسی، درک چندوجهی"},
    "gemini-1.5-flash-latest": {"name": "⚡️ Gemini 1.5 Flash", "description": "تفکر تطبیقی، کارایی هزینه‌ای"},
    # Note: Some model names might have been updated by Google. Using more standard names.
    "gemini-pro": {"name": "✨ Gemini Pro", "description": "ویژگی‌های نسل بعدی، سرعت، استریم لحظه‌ای"},
}

# --- 5. Pydantic Data Models (Unchanged) ---
class MessagePart(BaseModel):
    text: str

class Message(BaseModel):
    role: str
    parts: List[MessagePart]

class WebSocketRequest(BaseModel):
    type: str
    conversation_id: str | None = None
    model: str | None = None
    message: Message | None = None

# --- 6. Core AI and Database Logic ---

# --- بهبود: بازنویسی کامل تابع استریم برای پایداری بیشتر ---
async def gemini_chat_stream(messages: List[Dict], model: str, stop_event: asyncio.Event) -> AsyncGenerator[Dict[str, Any], None]:
    headers = {"Content-Type": "application/json"}
    payload = {"contents": messages}
    url = f"{GEMINI_API_BASE_URL}/{model}:streamGenerateContent?key={GEMINI_API_KEY}"
    
    timeout = httpx.Timeout(120.0, connect=10.0) # Increased timeout
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                buffer = ""
                async for raw_chunk in response.aiter_bytes():
                    if stop_event.is_set():
                        logger.info("Stop event received, breaking stream.")
                        break
                    
                    buffer += raw_chunk.decode('utf-8', errors='ignore')

                    # The stream sends JSON objects separated by commas, enclosed in [].
                    # We process complete JSON objects from the buffer.
                    if buffer.startswith('['): buffer = buffer[1:]
                    if buffer.endswith(']'): buffer = buffer[:-1]

                    parts = buffer.split('},')
                    for i, part in enumerate(parts):
                        if i < len(parts) -1:
                            part += '}'
                        
                        try:
                            data = json.loads(part)
                            text_content = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            if text_content:
                                yield {"type": "chunk", "content": text_content}
                            buffer = ""
                        except json.JSONDecodeError:
                            buffer = part # Incomplete JSON, wait for next chunk
                            continue
        
        except httpx.HTTPStatusError as e:
            error_body = await e.response.aread()
            logger.error(f"HTTP error during stream: {e.response.status_code} - {error_body.decode()}", exc_info=True)
            yield {"type": "error", "content": f"HTTP error: {e.response.status_code}"}
        except Exception as e:
            logger.error(f"An unexpected error occurred in gemini_chat_stream: {e}", exc_info=True)
            yield {"type": "error", "content": f"Unexpected error: {str(e)}"}


async def generate_title_for_conversation(user_id: str, conversation_id: str, first_message: str, websocket: WebSocket):
    if not db: return
    
    prompt = f"Create a very short, concise title (4 words max, in Persian) for a conversation starting with: '{first_message}'. Respond ONLY with the title itself, no extra text."
    prompt_messages = [{"role": "user", "parts": [{"text": prompt}]}]
    
    try:
        title = ""
        # Using a fast model for title generation
        async for result in gemini_chat_stream(prompt_messages, "gemini-1.5-flash-latest", asyncio.Event()):
            if result["type"] == "chunk":
                title += result["content"]

        if title:
            clean_title = title.strip().replace("\"", "").replace("*", "")
            doc_ref = db.collection(USERS_COLLECTION).document(user_id).collection(CONVERSATIONS_COLLECTION).document(conversation_id)
            await doc_ref.update({"title": clean_title})
            logger.info(f"Generated and saved title '{clean_title}' for conversation {conversation_id}")
            # --- بهبود: ارسال پیام آپدیت عنوان به فرانت‌اند ---
            await websocket.send_json({"type": "title_update", "conversation_id": conversation_id, "title": clean_title})
    except Exception as e:
        logger.error(f"Error generating or saving title: {e}", exc_info=True)


# --- بهبود: تغییر ساختار ذخیره پیام‌ها به Subcollection برای مقیاس‌پذیری ---
async def get_messages_from_db(conversation_id: str) -> List[Dict]:
    if not db: return []
    try:
        messages_ref = db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION).document(conversation_id).collection(MESSAGES_COLLECTION)
        docs = messages_ref.order_by("timestamp").stream()
        return [doc.to_dict() async for doc in docs]
    except Exception as e:
        logger.error(f"Error fetching messages for {conversation_id}: {e}", exc_info=True)
        return []

async def get_conversations_from_db() -> List[Dict]:
    if not db: return []
    try:
        conversations_ref = db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION)
        docs = conversations_ref.order_by("created_at", direction=firestore.Query.DESCENDING).limit(50).stream()
        conversations = []
        async for doc in docs:
            data = doc.to_dict()
            conversations.append({"id": doc.id, "title": data.get("title", "مکالمه جدید...")})
        return conversations
    except Exception as e:
        logger.error(f"Error fetching conversations: {e}", exc_info=True)
        return []

# --- 7. API Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

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
    if not messages and db:
        # Check if conversation document exists but has no messages yet
        convo_doc = await db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION).document(conversation_id).get()
        if not convo_doc.exists:
            raise HTTPException(status_code=404, detail="Conversation not found")
    
    return JSONResponse(content={"messages": messages})

# --- 8. WebSocket Main Handler ---
# --- بهبود: بازنویسی کامل WebSocket Handler برای مدیریت بهتر state و خطاها ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    stop_event = asyncio.Event()
    generation_task = None
    
    async def chat_generator_task(request_data: WebSocketRequest):
        stop_event.clear()
        
        conversation_id = request_data.conversation_id
        is_new_conversation = False
        if not conversation_id:
            is_new_conversation = True
            conversation_id = str(uuid.uuid4())
            await websocket.send_json({"type": "info", "conversation_id": conversation_id})

        user_message_dict = request_data.message.dict()
        user_message_dict["timestamp"] = firestore.SERVER_TIMESTAMP

        if db:
            convo_ref = db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION).document(conversation_id)
            if is_new_conversation:
                await convo_ref.set({"created_at": firestore.SERVER_TIMESTAMP, "title": "مکالمه جدید..."})
            
            msg_ref = convo_ref.collection(MESSAGES_COLLECTION)
            await msg_ref.add(user_message_dict)

        history = await get_messages_from_db(conversation_id)
        
        full_assistant_message = ""
        try:
            async for result in gemini_chat_stream(history, request_data.model, stop_event):
                if result["type"] == "chunk":
                    await websocket.send_text(result["content"])
                    full_assistant_message += result["content"]
                elif result["type"] == "error":
                    await websocket.send_json({"type": "error", "message": result["content"]})
                    full_assistant_message = "" # Prevent saving error message
                    break
        finally:
            if full_assistant_message:
                assistant_message_dict = {
                    "role": "model",
                    "parts": [{"text": full_assistant_message}],
                    "timestamp": firestore.SERVER_TIMESTAMP
                }
                if db:
                    await msg_ref.add(assistant_message_dict)
            
            await websocket.send_json({"type": "stream_end"})

            # Generate title in background only for the first user message
            if is_new_conversation and db:
                asyncio.create_task(generate_title_for_conversation(USER_ID, conversation_id, user_message_dict["parts"][0]["text"], websocket))

    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            request = WebSocketRequest(**data)

            if request.type == "stop":
                logger.info(f"Stop request received for client {websocket.client}")
                stop_event.set()
                if generation_task:
                    generation_task.cancel()
                    generation_task = None
                continue
            
            if request.type == "chat":
                if generation_task and not generation_task.done():
                    logger.warning("Client sent new message while previous one was generating. Cancelling old task.")
                    stop_event.set()
                    generation_task.cancel()
                
                generation_task = asyncio.create_task(chat_generator_task(request))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {websocket.client}")
        stop_event.set()
        if generation_task:
            generation_task.cancel()
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        await websocket.close(code=1011, reason=f"An error occurred: {e}")
