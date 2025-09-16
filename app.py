# app.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, BackgroundTasks, status
from datetime import timedelta
import os
import json
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import List, Dict, Any, AsyncGenerator

import httpx
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account
from pydantic import BaseModel
from cachetools import TTLCache

# --- 1. تنظیمات اولیه و ثابت‌ها (Basic Configuration & Constants) ---
# بارگذاری متغیرهای محیطی از فایل .env
load_dotenv()

# تنظیمات اولیه لاگ‌گیری برای نمایش اطلاعات مفید در کنسول
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ثابت‌های کلیدی برنامه
USER_ID = "main_user"
CREDENTIALS_FILE = "credentials.json"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# بررسی وجود کلید API، در صورت نبودن، برنامه را متوقف کن
if not GEMINI_API_KEY:
    logger.critical("FATAL: GEMINI_API_KEY environment variable is not set.")
    raise ValueError("GEMINI_API_KEY is missing.")

# URL پایه برای ارتباط با API مدل Gemini
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# بهبود: تعریف نام collectionها به عنوان ثابت برای جلوگیری از خطای تایپی
USERS_COLLECTION = "users"
CONVERSATIONS_COLLECTION = "conversations"
MESSAGES_COLLECTION = "messages"


# --- 2. راه‌اندازی برنامه FastAPI (FastAPI App Initialization) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    تابع مدیریت چرخه حیات برنامه (lifespan)
    این تابع در زمان شروع و پایان برنامه اجرا می‌شود.
    """
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

# --- 3. راه‌اندازی Firestore و Cache (Firestore & Cache Initialization) ---
db = None
try:
    # اتصال به Firestore با استفاده از فایل JSON یا متغیر محیطی
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

# ایجاد یک کش در حافظه (in-memory cache) با TTL (Time-To-Live)
# این کش برای ذخیره‌سازی موقت داده‌ها استفاده می‌شود
cache = TTLCache(maxsize=100, ttl=timedelta(minutes=5).total_seconds())

# --- 4. تنظیمات مدل‌های Gemini (Gemini Models Configuration) ---
MODELS = {
    "gemini-1.5-pro-latest": {"name": "🚀 Gemini 1.5 Pro", "description": "استدلال پیچیده، کدنویسی، درک چندوجهی"},
    "gemini-1.5-flash-latest": {"name": "⚡️ Gemini 1.5 Flash", "description": "تفکر تطبیقی، کارایی هزینه‌ای"},
    "gemini-pro": {"name": "✨ Gemini Pro", "description": "ویژگی‌های نسل بعدی، سرعت، استریم لحظه‌ای"},
}

# --- 5. مدل‌های داده Pydantic (Pydantic Data Models) ---
# تعریف ساختار داده‌های ورودی و خروجی با Pydantic
class MessagePart(BaseModel):
    text: str

class Message(BaseModel):
    role: str
    parts: List[MessagePart]

class WebSocketRequest(BaseModel):
    # نوع درخواست: "chat" یا "stop"
    type: str
    # شناسه مکالمه، در صورت جدید بودن None است
    conversation_id: str | None = None
    # مدل انتخاب شده برای چت
    model: str | None = None
    # پیام کاربر
    message: Message | None = None

# --- 6. منطق اصلی هوش مصنوعی و پایگاه داده (Core AI and Database Logic) ---

async def gemini_chat_stream(messages: List[Dict], model: str, stop_event: asyncio.Event) -> AsyncGenerator[Dict[str, Any], None]:
    """
    اتصال به API استریم Gemini و دریافت پاسخ به صورت تکه‌تکه.
    """
    headers = {"Content-Type": "application/json"}
    payload = {"contents": messages}
    url = f"{GEMINI_API_BASE_URL}/{model}:streamGenerateContent?key={GEMINI_API_KEY}"
    
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                # بررسی کد وضعیت HTTP
                if response.status_code != 200:
                    error_body = await response.aread()
                    logger.error(f"Non-200 response from Gemini: {response.status_code} - {error_body.decode()}")
                    yield {"type": "error", "content": f"API Error: {error_body.decode()}"}
                    return
                
                # خواندن تکه‌های داده به صورت استریم
                buffer = ""
                async for raw_chunk in response.aiter_bytes():
                    if stop_event.is_set():
                        logger.info("Stop event received, breaking stream.")
                        break
                    
                    buffer += raw_chunk.decode('utf-8', errors='ignore')
                    
                    # پاک‌سازی براکت‌های ابتدایی و انتهایی از داده‌های استریم
                    if buffer.startswith('['): buffer = buffer[1:]
                    if buffer.endswith(']'): buffer = buffer[:-1]

                    parts = buffer.split('},')
                    for i, part in enumerate(parts):
                        if i < len(parts) - 1:
                            part += '}'
                        
                        try:
                            data = json.loads(part)
                            text_content = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            if text_content:
                                yield {"type": "chunk", "content": text_content}
                                buffer = ""
                        except json.JSONDecodeError:
                            buffer = part
                            continue
                
                # مدیریت بافر باقیمانده
                if buffer:
                    try:
                        data = json.loads(buffer)
                        text_content = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        if text_content:
                            yield {"type": "chunk", "content": text_content}
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse remaining buffer: {buffer}")
            
        except httpx.RequestError as e:
            logger.error(f"HTTP request error in gemini_chat_stream: {e}", exc_info=True)
            yield {"type": "error", "content": f"Network error connecting to Gemini API: {str(e)}"}
        except Exception as e:
            logger.error(f"An unexpected error occurred in gemini_chat_stream: {e}", exc_info=True)
            yield {"type": "error", "content": f"Unexpected error: {str(e)}"}


async def generate_title_for_conversation(user_id: str, conversation_id: str, first_message: str, websocket: WebSocket):
    """
    عنوان‌گذاری خودکار مکالمه با استفاده از Gemini Flash
    """
    if not db: return
    
    prompt = f"Create a very short, concise title (4 words max, in Persian) for a conversation starting with: '{first_message}'. Respond ONLY with the title itself, no extra text."
    prompt_messages = [{"role": "user", "parts": [{"text": prompt}]}]
    
    try:
        title = ""
        # استفاده از مدل Gemini 1.5 Flash برای سرعت بالا
        async for result in gemini_chat_stream(prompt_messages, "gemini-1.5-flash-latest", asyncio.Event()):
            if result["type"] == "chunk":
                title += result["content"]

        if title:
            clean_title = title.strip().replace("\"", "").replace("*", "")
            doc_ref = db.collection(USERS_COLLECTION).document(user_id).collection(CONVERSATIONS_COLLECTION).document(conversation_id)
            await doc_ref.update({"title": clean_title})
            logger.info(f"Generated and saved title '{clean_title}' for conversation {conversation_id}")
            # ارسال عنوان جدید به کلاینت از طریق WebSocket
            await websocket.send_json({"type": "title_update", "conversation_id": conversation_id, "title": clean_title})
    except Exception as e:
        logger.error(f"Error generating or saving title: {e}", exc_info=True)


async def get_messages_from_db(conversation_id: str) -> List[Dict]:
    """
    بازیابی تاریخچه پیام‌های یک مکالمه از Firestore
    """
    if not db: return []
    try:
        messages_ref = db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION).document(conversation_id).collection(MESSAGES_COLLECTION)
        docs = messages_ref.order_by("timestamp").stream()
        return [doc.to_dict() async for doc in docs]
    except Exception as e:
        logger.error(f"Error fetching messages for {conversation_id}: {e}", exc_info=True)
        return []

async def delete_conversation_from_db(conversation_id: str):
    """
    حذف کامل یک مکالمه و تمام پیام‌های مرتبط با آن از Firestore
    """
    if not db:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not configured")
    try:
        convo_ref = db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION).document(conversation_id)

        # حذف تمام پیام‌ها در subcollection
        messages_ref = convo_ref.collection(MESSAGES_COLLECTION)
        docs = await messages_ref.limit(500).get()
        while docs:
            batch = db.batch()
            for doc in docs:
                batch.delete(doc.reference)
            await batch.commit()
            docs = await messages_ref.limit(500).get()

        await convo_ref.delete()
        logger.info(f"Successfully deleted conversation {conversation_id} and its messages.")
    except Exception as e:
        logger.error(f"Error deleting conversation {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete conversation: {e}")

async def get_conversations_from_db() -> List[Dict]:
    """
    بازیابی لیست مکالمات کاربر از Firestore
    """
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

# --- 7. نقاط انتهایی API (API Endpoints) ---

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """
    سرویس‌دهی به فایل index.html
    """
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/models")
async def get_models():
    """
    بازگرداندن لیست مدل‌های پشتیبانی شده
    """
    return JSONResponse(content={"models": MODELS})

@app.get("/api/conversations")
async def get_conversations():
    """
    بازگرداندن لیست مکالمات کاربر
    """
    if not db:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not configured")
    conversations = await get_conversations_from_db()
    return JSONResponse(content={"conversations": conversations})

@app.delete("/api/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: str):
    """
    حذف یک مکالمه مشخص
    """
    await delete_conversation_from_db(conversation_id)
    # کد 204 به معنی موفقیت و بدون محتوا است.

@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    """
    بازگرداندن تاریخچه پیام‌های یک مکالمه
    """
    if not db:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not configured")
    
    messages = await get_messages_from_db(conversation_id)
    if not messages:
        convo_doc = await db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION).document(conversation_id).get()
        if not convo_doc.exists:
            raise HTTPException(status_code=404, detail="Conversation not found")
    
    return JSONResponse(content={"messages": messages})

# --- 8. هندلر اصلی WebSocket (WebSocket Main Handler) ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    """
    نقطه پایانی اصلی برای ارتباط WebSocket
    """
    await websocket.accept()
    # رویداد برای مدیریت توقف تولید پاسخ توسط کاربر
    stop_event = asyncio.Event()
    generation_task = None
    
    async def chat_generator_task(request_data: WebSocketRequest):
        """
        وظیفه اصلی تولید پاسخ و مدیریت جریان چت
        """
        stop_event.clear()
        
        conversation_id = request_data.conversation_id
        is_new_conversation = False
        if not conversation_id:
            is_new_conversation = True
            conversation_id = str(uuid.uuid4())
            # اطلاع‌رسانی به کلاینت درباره شناسه مکالمه جدید
            await websocket.send_json({"type": "info", "conversation_id": conversation_id})

        user_message_dict = request_data.message.dict()
        user_message_dict["timestamp"] = firestore.SERVER_TIMESTAMP

        if db:
            convo_ref = db.collection(USERS_COLLECTION).document(USER_ID).collection(CONVERSATIONS_COLLECTION).document(conversation_id)
            if is_new_conversation:
                # ایجاد سند مکالمه جدید
                await convo_ref.set({"created_at": firestore.SERVER_TIMESTAMP, "title": "مکالمه جدید..."})
            
            msg_ref = convo_ref.collection(MESSAGES_COLLECTION)
            # ذخیره پیام کاربر در دیتابیس
            await msg_ref.add(user_message_dict)

        # دریافت تاریخچه کامل مکالمه
        history = await get_messages_from_db(conversation_id)
        
        full_assistant_message = ""
        try:
            # شروع استریم Gemini
            async for result in gemini_chat_stream(history, request_data.model, stop_event):
                if result["type"] == "chunk":
                    await websocket.send_text(result["content"])
                    full_assistant_message += result["content"]
                elif result["type"] == "error":
                    await websocket.send_json({"type": "error", "message": result["content"]})
                    full_assistant_message = ""
                    break
        finally:
            if full_assistant_message:
                assistant_message_dict = {
                    "role": "model",
                    "parts": [{"text": full_assistant_message}],
                    "timestamp": firestore.SERVER_TIMESTAMP
                }
                if db:
                    try:
                        # ذخیره پیام دستیار در دیتابیس
                        await msg_ref.add(assistant_message_dict)
                    except Exception as e:
                        logger.error(f"Failed to save assistant message: {e}", exc_info=True)
            
            # ارسال پیام پایان استریم
            await websocket.send_json({"type": "stream_end"})

            # اگر مکالمه جدید بود، عنوان آن را به صورت پس‌زمینه تولید کن
            if is_new_conversation and db:
                asyncio.create_task(generate_title_for_conversation(USER_ID, conversation_id, user_message_dict["parts"][0]["text"], websocket))

    try:
        # حلقه اصلی دریافت پیام‌ها از WebSocket
        while True:
            raw_data = await websocket.receive_text()
            try:
                data = json.loads(raw_data)
                request = WebSocketRequest(**data)
            except json.JSONDecodeError:
                logger.error("Received malformed JSON message on WebSocket.")
                continue

            # مدیریت درخواست توقف
            if request.type == "stop":
                logger.info(f"Stop request received for client {websocket.client}")
                stop_event.set()
                if generation_task and not generation_task.done():
                    generation_task.cancel()
                continue
            
            # مدیریت درخواست چت
            if request.type == "chat":
                # اگر وظیفه قبلی هنوز در حال اجراست، آن را کنسل کن
                if generation_task and not generation_task.done():
                    logger.warning("Client sent new message while previous one was generating. Cancelling old task.")
                    stop_event.set()
                    generation_task.cancel()
                    
                generation_task = asyncio.create_task(chat_generator_task(request))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {websocket.client}")
        stop_event.set()
        if generation_task and not generation_task.done():
            generation_task.cancel()
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        await websocket.close(code=1011, reason=f"An error occurred: {e}")
