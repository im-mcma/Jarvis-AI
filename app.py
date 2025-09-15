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
templates = Jinja2Templates(directory="templates")

# --- 3. Firestore & Cache Initialization ---
db = None
try:
    if not GEMINI_API_KEY:
        logger.critical("FATAL: GEMINI_API_KEY environment variable is not set.")
    
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
    type: str = "chat"
    conversation_id: str | None = None
    model: str
    message: Message

# --- 6. Core AI and Database Logic ---
async def gemini_chat_stream(messages: List[Dict], model: str, stop_event: asyncio.Event) -> AsyncGenerator[Dict, None]:
    # ... (Implementation from v7.0 - No changes needed)
    pass

async def generate_title_for_conversation(user_id: str, conversation_id: str, first_message: str):
    # ... (Implementation from v7.0 - No changes needed)
    pass

async def get_messages_from_db(conversation_id: str) -> List[Dict]:
    # ... (Implementation from v7.0 - No changes needed)
    pass

async def get_conversations_from_db() -> List[Dict]:
    # ... (Implementation from v7.0 - No changes needed)
    pass

# --- 7. API Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "models": MODELS})

@app.get("/health", status_code=200)
async def health_check():
    return {"status": "ok"}

# ... (All other endpoints from v7.0 - No changes needed)

# --- 8. WebSocket Main Handler ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket, background_tasks: BackgroundTasks):
    # ... (Full implementation from v7.0 - No changes needed)
    pass
