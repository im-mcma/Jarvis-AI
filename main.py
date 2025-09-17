import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# --- Initial Setup ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Environment Variables & Constants ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user" # Placeholder for a real auth system

app = FastAPI(title="Jarvis Elite - Definitive AI Backend")

# --- CORS Middleware for React App ---
# This allows the React app on port 5173 to connect to this backend on port 8000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Database Connection ---
client: Optional[AsyncIOMotorClient] = None

@app.on_event("startup")
async def startup_db_client():
    global client
    client = AsyncIOMotorClient(MONGO_URI)
    logger.info("MongoDB connection established.")

@app.on_event("shutdown")
def shutdown_db_client():
    if client: client.close()

def get_coll(): return client["chat_ai_db"]["users"]

# --- Database Functions ---
async def db_create_conversation(user_id: str, conv_id: str, title: str):
    conv = {"_id": conv_id, "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
    await get_coll().update_one({"_id": user_id}, {"$push": {"conversations": conv}}, upsert=True)

async def db_get_conversations(user_id: str) -> List:
    doc = await get_coll().find_one({"_id": user_id}, {"conversations.messages": 0})
    if not doc or "conversations" not in doc: return []
    return sorted(doc["conversations"], key=lambda c: c["created_at"], reverse=True)

async def db_get_messages(user_id: str, conv_id: str) -> List:
    doc = await get_coll().find_one({"_id": user_id, "conversations._id": conv_id}, {"conversations.$": 1})
    return doc["conversations"][0].get("messages", []) if doc and doc.get("conversations") else []

async def db_save_message(user_id: str, conv_id: str, msg: Dict):
    await get_coll().update_one({"_id": user_id, "conversations._id": conv_id}, {"$push": {"conversations.$.messages": msg}})

# --- API Routes ---
@app.get("/api/models")
async def get_available_models():
    """Definitive, un-merged list of all models as requested."""
    return {
        "chat": {
            "name": "چت متنی",
            "models": {
                "gemma-2-9b-it": "Gemma 3 / 3n (RPM: 30, RPD: 14,400)",
                "gemini-1.0-pro-002": "Gemini 2.0 Flash-Lite (RPM: 30, RPD: 200)", # Closest technical ID
                "gemini-1.0-pro": "Gemini 2.0 Flash (RPM: 15, RPD: 200)",
                "gemini-1.5-flash-latest": "Gemini 2.5 Flash / Lite (RPM: 10-15)",
                "gemini-1.5-pro-latest": "Gemini 2.5 Pro (RPM: 5, RPD: 100)"
            }
        },
        "image": {
            "name": "تولید تصویر",
            "models": {"imagen-placeholder": "Gemini Image Generation (RPM: 10, RPD: 100)"}
        }
    }

@app.get("/api/conversations")
async def api_get_conversations(): return await db_get_conversations(USER_ID)

@app.get("/api/conversations/{conv_id}")
async def api_get_messages(conv_id: str): return await db_get_messages(USER_ID, conv_id)

# --- AI Core Logic ---
async def _placeholder_image_gen(prompt: str) -> str:
    await asyncio.sleep(3)
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/1024"

@app.websocket("/api/ws/gateway")
async def websocket_gateway(websocket: WebSocket):
    await websocket.accept()
    
    async def task_handler(mode: str, model: str, prompt: str, conv_id: Optional[str]):
        try:
            is_new_conv = not conv_id
            if is_new_conv:
                conv_id = str(uuid.uuid4())
                await db_create_conversation(USER_ID, conv_id, prompt)
                await websocket.send_json({"type": "conversation_created", "id": conv_id, "title": prompt[:50]})

            await db_save_message(USER_ID, conv_id, {"role": "user", "type": "text", "content": prompt, "timestamp": datetime.now(timezone.utc).isoformat()})

            if mode == "chat":
                # The 'get_messages' bug is fixed here by calling the correct db_get_messages
                messages = await db_get_messages(USER_ID, conv_id)
                api_msgs = [{"role": m["role"], "parts": [{"text": m["content"]}]} for m in messages if m['type'] == 'text']
                
                full_res, res_msg_id = "", str(uuid.uuid4())
                
                async with httpx.AsyncClient(timeout=120.0) as client:
                    async with client.stream("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}", json={"contents": api_msgs}) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            for line in chunk.decode('utf-8').splitlines():
                                if '"text":' in line:
                                    try:
                                        text = json.loads("{" + line.strip().rstrip(',') + "}").get("text", "")
                                        await websocket.send_json({"type": "text_chunk", "content": text, "msgId": res_msg_id})
                                        full_res += text
                                    except Exception: continue
                if full_res:
                    await db_save_message(USER_ID, conv_id, {"role": "model", "type": "text", "content": full_res, "timestamp": datetime.now(timezone.utc).isoformat(), "msgId": res_msg_id})

            elif mode == "image":
                image_url = await _placeholder_image_gen(prompt)
                res_msg_id = str(uuid.uuid4())
                await websocket.send_json({"type": "image_url", "content": image_url, "msgId": res_msg_id})
                await db_save_message(USER_ID, conv_id, {"role": "model", "type": "image", "content": image_url, "timestamp": datetime.now(timezone.utc).isoformat(), "msgId": res_msg_id})

        except Exception as e:
            await websocket.send_json({"type": "error", "content": str(e)})
        finally:
            await websocket.send_json({"type": "stream_end"})

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "generate":
                asyncio.create_task(task_handler(data["mode"], data["model"], data["prompt"], data.get("conversation_id")))
    except WebSocketDisconnect:
        logger.info("Client disconnected.")
