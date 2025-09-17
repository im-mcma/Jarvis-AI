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

# --- Load environment variables ---
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

USER_ID = "main_user"

# --- Logging setup ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- FastAPI app setup ---
app = FastAPI(title="Jarvis AI Chat - MongoDB Streaming")
templates = Jinja2Templates(directory="templates")

# --- MongoDB Setup ---
client: Optional[AsyncIOMotorClient] = None
db = None
USERS_COLLECTION = None

@app.on_event("startup")
async def startup_db_client():
    global client, db, USERS_COLLECTION
    logger.info("Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client["chat_ai_db"]
    USERS_COLLECTION = db["users"]
    logger.info("MongoDB connection established.")

@app.on_event("shutdown")
async def shutdown_db_client():
    if client:
        logger.info("Closing MongoDB connection...")
        client.close()
        logger.info("MongoDB client closed.")

# --- Database Functions ---
async def save_message(user_id: str, conversation_id: str, message: Dict[str, Any]):
    await USERS_COLLECTION.update_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"$push": {"conversations.$.messages": message}},
        upsert=True
    )

async def create_new_conversation(user_id: str, conversation_id: str, initial_title: str = "مکالمه جدید...") -> Dict[str, Any]:
    new_conv_doc = {
        "_id": conversation_id,
        "title": initial_title,
        "created_at": datetime.now(timezone.utc),
        "messages": []
    }
    await USERS_COLLECTION.update_one(
        {"_id": user_id},
        {"$push": {"conversations": new_conv_doc}},
        upsert=True
    )
    return new_conv_doc

async def get_messages(user_id: str, conversation_id: str) -> List[Dict[str, Any]]:
    user_doc = await USERS_COLLECTION.find_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"conversations.$": 1}
    )
    if user_doc and "conversations" in user_doc and user_doc["conversations"]:
        return user_doc["conversations"][0].get("messages", [])
    return []

async def get_conversations(user_id: str) -> List[Dict[str, Any]]:
    user_doc = await USERS_COLLECTION.find_one({"_id": user_id}, {"conversations._id": 1, "conversations.title": 1, "conversations.created_at": 1})
    if not user_doc or "conversations" not in user_doc:
        return []
    conversations = sorted(user_doc["conversations"], key=lambda c: c.get("created_at", datetime.min), reverse=True)
    return [{"id": conv["_id"], "title": conv.get("title", "مکالمه جدید..."), "created_at": conv.get("created_at").isoformat()} for conv in conversations]

async def delete_conversation(user_id: str, conversation_id: str):
    await USERS_COLLECTION.update_one(
        {"_id": user_id},
        {"$pull": {"conversations": {"_id": conversation_id}}}
    )

async def update_conversation_title(user_id: str, conversation_id: str, new_title: str):
    await USERS_COLLECTION.update_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"$set": {"conversations.$.title": new_title}}
    )

# --- FastAPI Routes ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/models")
async def get_available_models():
    """
    Returns a comprehensive list of available free-tier models based on official Gemini pricing page.
    """
    return JSONResponse(content={
        "models": {
            # ** NEW: Comprehensive and ordered list **
            "gemini-1.5-flash-latest": {
                "name": "⚡️ Gemini 1.5 Flash (پیشنهادی)",
                "description": "مدل بسیار سریع و کارآمد، بهترین گزینه برای شروع و اکثر کارها. سهمیه رایگان: 15 درخواست در دقیقه."
            },
            "gemini-1.0-pro": {
                "name": "✅ Gemini 1.0 Pro (پایدار)",
                "description": "مدل استاندارد و بسیار پایدار با بالاترین سهمیه رایگان. سهمیه رایگان: 60 درخواست در دقیقه."
            },
            "gemini-1.5-pro-latest": {
                "name": "🧠 Gemini 1.5 Pro (پیشرفته)",
                "description": "قدرتمندترین مدل با قابلیت درک زمینه بزرگ (تا 1 میلیون توکن). مناسب برای کارهای پیچیده اما با کمترین سهمیه رایگان. سهمیه رایگان: 2 درخواست در دقیقه."
            }
        }
    })

# ... (تمام توابع دیگر مثل get_conversations, delete_conversation و ... همانند قبل باقی می‌مانند) ...

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

@app.put("/api/conversations/{conversation_id}/title")
async def api_update_conversation_title(conversation_id: str, request: Request):
    data = await request.json()
    new_title = data.get("title", "").strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    await update_conversation_title(USER_ID, conversation_id, new_title)
    return JSONResponse(content={"status": "updated", "new_title": new_title})


# --- Gemini API Utils ---
async def _call_gemini_api(payload: Dict[str, Any], model_name: str, stream: bool = False, timeout_s: int = 120):
    if not GEMINI_API_KEY:
        raise ValueError("Gemini API key is not configured.")
    
    action = "streamGenerateContent" if stream else "generateContent"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:{action}?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    timeout = httpx.Timeout(timeout_s, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if stream:
                response = await client.stream("POST", url, json=payload, headers=headers)
                response.raise_for_status()
                return response

            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response
            
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error from Gemini API: {e.response.status_code} - {e.response.text}")
            
            if e.response.status_code == 429:
                try:
                    error_data = e.response.json()
                    error_message = error_data[0].get("error", {}).get("message", "Rate limit exceeded.")
                    raise RuntimeError(f"شما از سهمیه رایگان مدل '{model_name}' فراتر رفته‌اید. لطفاً مدل دیگری را امتحان کنید یا کمی صبر کنید. جزئیات: {error_message}")
                except Exception:
                    raise RuntimeError(f"شما از سهمیه رایگان مدل '{model_name}' فراتر رفته‌اید. لطفاً کمی صبر کنید و دوباره امتحان کنید.")
            
            raise RuntimeError(f"خطای API از طرف گوگل: {e.response.text}") from e
            
        except Exception as e:
            logger.error(f"An unexpected error occurred during API call: {e}")
            raise RuntimeError(f"یک خطای غیرمنتظره در ارتباط با API رخ داد: {e}")

# ... (تمام توابع دیگر مثل gemini_chat_stream, _generate_conversation_title و websocket_endpoint همانند نسخه قبلی باقی می‌مانند) ...
# (کدهای WebSocket و بقیه را اینجا قرار دهید - آنها نیازی به تغییر ندارند)
async def gemini_chat_stream(messages: List[Dict[str, Any]], model: str, stop_event: asyncio.Event):
    """
    Streams the response from the Gemini API, handling JSON chunking.
    """
    logger.debug(f"Starting Gemini stream with model: {model}, message count: {len(messages)}")
    payload = {"contents": messages}
    decoder = json.JSONDecoder()
    
    try:
        response = await _call_gemini_api(payload, model, stream=True)
    except Exception as e:
        yield {"type": "error", "content": str(e)}
        return

    buffer = ""
    async for chunk in response.aiter_bytes():
        if stop_event.is_set():
            break
        
        buffer += chunk.decode("utf-8", errors="ignore")
        
        while buffer:
            try:
                data, idx = decoder.raw_decode(buffer.strip())
                text_parts = [
                    part.get("text", "")
                    for candidate in data.get("candidates", [])
                    for part in candidate.get("content", {}).get("parts", [])
                    if part.get("text")
                ]
                
                if text_parts:
                    yield {"type": "chunk", "content": "".join(text_parts)}
                
                buffer = buffer[idx:].strip()
                            
            except json.JSONDecodeError:
                break
            except Exception as e:
                logger.error(f"Error processing JSON chunk: {e}")
                yield {"type": "error", "content": f"Error processing stream: {str(e)}"}
                return

async def _generate_conversation_title(user_message: str, model_response_preview: str):
    logger.info(f"Attempting to generate title for: '{user_message[:50]}'")
    try:
        title_generation_prompt = {
            "contents": [{
                "role": "user", 
                "parts": [{
                    "text": f"برای این مکالمه یک عنوان کوتاه و مناسب به فارسی پیشنهاد بده (حداکثر 10 کلمه). فقط عنوان را برگردان، بدون متن اضافی:\n\nپیام کاربر: \"{user_message}\"\nپاسخ مدل (شروع): \"{model_response_preview}\""
                }]
            }]
        }
        
        # Use a fast model for title generation
        response = await _call_gemini_api(title_generation_prompt, "gemini-1.5-flash-latest", stream=False, timeout_s=30)
        json_response = response.json()
        
        generated_title_parts = [
            part.get("text", "")
            for candidate in json_response.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
            if part.get("text")
        ]
        
        title = "".join(generated_title_parts).strip().replace('"', '').replace("'", '').replace('**', '').replace('\n', ' ').strip()
        title = (title[:70] + '...') if len(title) > 70 else title

        if title:
            return title
    except Exception as e:
        logger.error(f"Failed to generate conversation title: {e}")
    return user_message[:50] + "..."


@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection accepted.")
    
    generation_task: Optional[asyncio.Task] = None
    stop_event = asyncio.Event()

    async def cancel_previous_task():
        nonlocal generation_task
        if generation_task and not generation_task.done():
            stop_event.set()
            try:
                await asyncio.wait_for(generation_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                generation_task.cancel()
                logger.warning("Previous task cancelled forcefully.")
            finally:
                generation_task = None
                stop_event.clear()

    async def chat_generator_task(message_text: str, conversation_id: Optional[str], model: str):
        is_new_conversation = not conversation_id
        if is_new_conversation:
            conversation_id = str(uuid.uuid4())
            await create_new_conversation(USER_ID, conversation_id)
            await websocket.send_json({"type": "info", "conversation_id": conversation_id})

        conversation_history = await get_messages(USER_ID, conversation_id)
        messages_for_api = [{"role": msg["role"], "parts": msg["parts"]} for msg in conversation_history]
        user_message = {"role": "user", "parts": [{"text": message_text}]}
        messages_for_api.append(user_message)
        
        user_message_with_ts = {**user_message, "timestamp": datetime.now(timezone.utc)}
        await save_message(USER_ID, conversation_id, user_message_with_ts)

        full_assistant_message = ""
        async for result in gemini_chat_stream(messages_for_api, model, stop_event):
            if result["type"] == "chunk":
                await websocket.send_text(result["content"])
                full_assistant_message += result["content"]
            elif result["type"] == "error":
                await websocket.send_json({"type": "error", "content": result["content"]})
                break

        if full_assistant_message:
            assistant_message = {"role": "model", "parts": [{"text": full_assistant_message}], "timestamp": datetime.now(timezone.utc)}
            await save_message(USER_ID, conversation_id, assistant_message)
        
        await websocket.send_json({"type": "stream_end"})

        if is_new_conversation and full_assistant_message:
            new_title = await _generate_conversation_title(message_text, full_assistant_message[:200])
            await update_conversation_title(USER_ID, conversation_id, new_title)
            await websocket.send_json({"type": "title_update", "conversation_id": conversation_id, "title": new_title})
    
    try:
        while True:
            data_raw = await websocket.receive_text()
            data = json.loads(data_raw)
            
            if data.get("type") == "stop":
                await cancel_previous_task()
                continue
                
            if data.get("type") == "chat":
                await cancel_previous_task()
                stop_event.clear()
                generation_task = asyncio.create_task(
                    chat_generator_task(
                        data["message"]["parts"][0]["text"],
                        data.get("conversation_id"),
                        data.get("model", "gemini-1.5-flash-latest") # Default to flash
                    )
                )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected.")
        await cancel_previous_task()
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
