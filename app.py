import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Jarvis Elite - Intelligent AI Gateway")
templates = Jinja2Templates(directory="templates")
client: Optional[AsyncIOMotorClient] = None

@app.on_event("startup")
async def startup_db_client():
    global client
    client = AsyncIOMotorClient(MONGO_URI)

@app.on_event("shutdown")
def shutdown_db_client():
    if client: client.close()

def get_coll(): return client["chat_ai_db"]["users"]

# --- Database Functions ---
async def save_message(user_id: str, conv_id: str, msg: Dict):
    await get_coll().update_one({"_id": user_id, "conversations._id": conv_id}, {"$push": {"conversations.$.messages": msg}})

async def create_conversation(user_id: str, conv_id: str, title: str):
    await get_coll().update_one(
        {"_id": user_id},
        {"$push": {"conversations": {"_id": conv_id, "title": title, "created_at": datetime.now(timezone.utc), "messages": []}}},
        upsert=True
    )

async def get_messages(user_id: str, conv_id: str) -> List:
    doc = await get_coll().find_one({"_id": user_id, "conversations._id": conv_id}, {"conversations.$": 1})
    return doc["conversations"][0].get("messages", []) if doc and doc.get("conversations") else []

async def get_conversations(user_id: str) -> List:
    doc = await get_coll().find_one({"_id": user_id})
    if not doc or "conversations" not in doc: return []
    convs = sorted(doc["conversations"], key=lambda c: c.get("created_at"), reverse=True)
    return [{"id": c["_id"], "title": c.get("title")} for c in convs]

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/models")
async def get_available_models():
    """
    Returns the complete, un-filtered list based on user's final request.
    """
    return JSONResponse(content={
        "models": {
            # ID های فنی با نام های لیست شما مپ شده اند
            "gemma-2-9b-it": { # Corresponds to Gemma 3
                "name": "✅ Gemma 2 / 3 (بیشترین سهمیه)",
                "description": "سریع و سبک با بالاترین سهمیه رایگان. (RPM: 30, RPD: 14,400)"
            },
            "gemini-1.5-flash-latest": { # Covers all modern Flash models
                "name": "🚀 Gemini Flash (نسل 2.0 تا 2.5)",
                "description": "تعادل عالی بین سرعت، قدرت و سهمیه. (RPM: 15 تا 30)"
            },
            "gemini-1.5-pro-latest": { # Covers all modern Pro models
                "name": "🧠 Gemini Pro (نسل 2.5)",
                "description": "قدرتمندترین مدل برای کارهای پیچیده. (RPM: 5, RPD: 100)"
            },
            "text-embedding-004": { # Newest Embedding model
                "name": "🔢 Gemini Embedding (تخصصی)",
                "description": "برای چت نیست! متن را به بردار عددی تبدیل می‌کند. (RPM: 100, RPD: 1,000)"
            }
        }
    })

# Other API Routes (get convos, messages...) remain here...
@app.get("/api/conversations")
async def api_get_all_conversations(): return await get_conversations(USER_ID)

@app.get("/api/conversations/{conversation_id}/messages")
async def api_get_conversation_messages(conversation_id: str): return {"messages": await get_messages(USER_ID, conversation_id)}

# --- Smart Backend Logic ---
async def _call_gemini_api(payload: Dict, model: str, stream: bool = False):
    if not GEMINI_API_KEY: raise ValueError("کلید API گوگل تنظیم نشده است.")

    # ** THIS IS THE SMART LOGIC YOU REQUESTED **
    # 1. Detect the model type and set the correct API endpoint (action)
    if "embedding" in model:
        action = "embedContent"
        # 2. Adapt the payload for the embedding API
        # It doesn't use "role", it needs "content" at the top level
        adapted_payload = {"content": {"parts": payload["contents"][0]["parts"]}}
    else: # For all chat models
        action = "streamGenerateContent" if stream else "generateContent"
        adapted_payload = payload
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{action}?key={GEMINI_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            res = await client.post(url, json=adapted_payload, headers={"Content-Type": "application/json"})
            res.raise_for_status()
            return res
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise RuntimeError(f"سهمیه رایگان مدل «{model}» تمام شده. لطفاً یک مدل دیگر انتخاب کنید.")
        raise RuntimeError(f"خطای API گوگل ({e.response.status_code}): {e.response.text}")
    except Exception as e:
        raise RuntimeError(f"خطای اتصال: {e}")

@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    task: Optional[asyncio.Task] = None

    async def chat_generator(msg: str, conv_id: Optional[str], model: str):
        try:
            if is_new_conv := not conv_id:
                conv_id = str(uuid.uuid4())
                await create_conversation(USER_ID, conv_id, msg[:50])
                await websocket.send_json({"type": "info", "conversation_id": conv_id})

            await save_message(USER_ID, conv_id, {"role": "user", "parts": [{"text": msg}], "timestamp": datetime.now(timezone.utc).isoformat()})

            # ** SMART MODEL HANDLING **
            if "embedding" in model:
                # Prepare payload for embedding API call
                history = await get_messages(USER_ID, conv_id)
                # We typically embed the last message, not the whole history
                api_payload = {"contents": [{"parts": [{"text": msg}]}]}
                res = await _call_gemini_api(api_payload, model, stream=False)
                embedding_vector = res.json()["embedding"]["values"]
                # Send a confirmation message instead of text stream
                info_msg = f"✅ عملیات Embedding با موفقیت انجام شد.\n- طول بردار: {len(embedding_vector)}\n- چند مقدار اول: {embedding_vector[:3]}..."
                await websocket.send_text(info_msg)
                await save_message(USER_ID, conv_id, {"role": "model", "parts": [{"text": info_msg}], "timestamp": datetime.now(timezone.utc).isoformat()})
                return # End of task for this model type

            # --- Default path for all chat models ---
            history = await get_messages(USER_ID, conv_id)
            api_msgs = [{"role": m["role"], "parts": m["parts"]} for m in history]
            
            full_res = ""
            res = await _call_gemini_api({"contents": api_msgs}, model, stream=True)
            async for chunk in res.aiter_bytes():
                # This parsing is more robust for Gemini's stream format
                buffer = chunk.decode('utf-8', errors='ignore')
                for line in buffer.splitlines():
                    if '"text":' in line:
                        try:
                            text_part = json.loads("{" + line.strip().rstrip(',') + "}")
                            text_chunk = text_part.get("text", "")
                            await websocket.send_text(text_chunk)
                            full_res += text_chunk
                        except json.JSONDecodeError: continue

            if full_res:
                await save_message(USER_ID, conv_id, {"role": "model", "parts": [{"text": full_res}], "timestamp": datetime.now(timezone.utc).isoformat()})

        except Exception as e:
            logger.error(f"Chat generator error: {e}")
            await websocket.send_json({"type": "error", "content": str(e)})
        finally:
            await websocket.send_json({"type": "stream_end"})

    try:
        while True:
            data = await websocket.receive_json()
            if task and not task.done(): task.cancel()
            
            if data.get("type") == "chat":
                task = asyncio.create_task(
                    chat_generator(data["message"], data.get("conversation_id"), data["model"])
                )
    except (WebSocketDisconnect, asyncio.CancelledError):
        if task and not task.done(): task.cancel()
