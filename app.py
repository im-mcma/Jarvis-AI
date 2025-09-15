# main.py
import os
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
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from cachetools import TTLCache

# --- Basic Configuration ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- App Initialization ---
app = FastAPI(
    title="Jarvis-Ai v2.0",
    description="An advanced, feature-rich AI chat assistant using FastAPI and Google Gemini.",
    version="2.0.0"
)
templates = Jinja2Templates(directory="templates")

# --- Firestore Initialization ---
try:
    if "GOOGLE_CREDENTIALS_JSON" in os.environ:
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        credentials = service_account.Credentials.from_service_account_info(creds_json)
        db = firestore.AsyncClient(credentials=credentials, project=credentials.project_id)
        logging.info("✅ Firestore connected successfully via environment variable.")
    else:
        db = firestore.AsyncClient()
        logging.info("✅ Firestore connected successfully via local credentials file.")
except Exception as e:
    logging.error(f"❌ Firestore connection failed: {e}")
    db = None

# --- Cache Configuration ---
# Cache for 10 minutes, max 100 users' conversation lists
cache = TTLCache(maxsize=100, ttl=timedelta(minutes=10).total_seconds())

# --- Gemini / AI Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logging.warning("⚠️ WARNING: GEMINI_API_KEY environment variable not set.")

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL_DEFAULT = "gemini-1.5-flash-latest"
MODELS = {
    "gemini-1.5-flash-latest": {"name": "⚡️ Gemini 1.5 Flash"},
    "gemini-1.5-pro-latest": {"name": "🧠 Gemini 1.5 Pro"},
    "gemini-pro": {"name": "💎 Gemini Pro"},
    "embedding-001": {"name": "Embedding-001 (Text)"},
    "aqa": {"name": "AQA (Question Answering)"},
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
    conversation_id: str
    model: str = MODEL_DEFAULT
    message: Message
    generation_config: GenerationConfig

# --- Firestore & Helper Functions ---
async def get_conversations_from_db():
    user_cache_key = f"convs_{USER_ID}"
    if user_cache_key in cache:
        logging.info("Serving conversations from cache.")
        return cache[user_cache_key]

    if not db: return []
    convs_ref = db.collection("users").document(USER_ID).collection("conversations").order_by("last_updated", direction=firestore.Query.DESCENDING)
    docs = await convs_ref.stream()
    convs = [{"id": doc.id, "title": doc.to_dict().get("title", "گفتگوی بدون عنوان")} async for doc in docs]
    cache[user_cache_key] = convs # Store in cache
    return convs

async def get_messages_from_db(conversation_id: str) -> List[Dict]:
    if not db: return []
    msgs_ref = db.collection("users").document(USER_ID).collection("conversations").document(conversation_id).collection("messages").order_by("timestamp")
    docs = await msgs_ref.stream()
    messages = []
    for doc in docs:
        data = doc.to_dict()
        messages.append({
            "role": data.get("role"),
            "parts": [{"text": part.get("text")} for part in data.get("parts", [])]
        })
    return messages

async def aqa_gemini_request(client, messages, config):
    # Specific implementation for the AQA model if needed
    # This is a placeholder for its unique request structure
    last_user_message = next((msg for msg in reversed(messages) if msg['role'] == 'user'), None)
    if not last_user_message:
        return '{"error": "No user message found for AQA model"}'
    
    aqa_payload = {
        "answerable_question": last_user_message['parts'][0]['text']
    }
    api_url = f"{GEMINI_API_BASE_URL}/aqa:generateAnswer?key={GEMINI_API_KEY}"
    response = await client.post(api_url, json=aqa_payload)
    response.raise_for_status()
    # The AQA response is not a stream, so we simulate a single-chunk stream
    api_response = response.json()
    # Reformat to match the standard streaming output for consistency
    formatted_response = {
        "candidates": [{
            "content": {
                "parts": [{"text": api_response.get('answer', {}).get('text', 'No answer found.')}]
            }
        }]
    }
    return f'data: {json.dumps(formatted_response)}\n\n'

async def gemini_chat_stream(messages: List, model: str, config: GenerationConfig):
    api_url = f"{GEMINI_API_BASE_URL}/{model}:streamGenerateContent?key={GEMINI_API_KEY}"
    # System Instruction
    system_instruction = {
        "role": "system",
        "parts": [{"text": "You are a helpful assistant named Jarvis. All your responses must be in Persian unless the user explicitly asks for another language."}]
    }
    payload = {
        "contents": [system_instruction] + messages,
        "generationConfig": config.dict()
    }
    
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            if model == "aqa":
                 # Handle non-streaming models like AQA
                single_response = await aqa_gemini_request(client, messages, config)
                yield single_response.encode('utf-8')
                return

            async with client.stream("POST", api_url, json=payload) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.HTTPStatusError as e:
            error_details = e.response.text
            logging.error(f"❌ Gemini API Error: {error_details}")
            error_json = json.dumps({"error": f"Gemini API Error: {error_details}"})
            yield f'data: {error_json}\n\n'.encode('utf-8')
        except Exception as e:
            logging.error(f"❌ An unexpected error occurred during API call: {e}")
            error_json = json.dumps({"error": f"An unexpected error occurred: {str(e)}"})
            yield f'data: {error_json}\n\n'.encode('utf-8')


# --- Background Tasks ---
async def generate_title_for_conversation(conv_id: str, history: List):
    """Generates and saves a title using the flash model."""
    if not db: return
    try:
        title_prompt = "این گفتگو را در حداکثر ۵ کلمه برای یک عنوان خلاصه کن. فقط عنوان را برگردان."
        messages_for_title = history + [{"role": "user", "parts": [{"text": title_prompt}]}]
        
        full_title = ""
        # Using the regular stream function to get the title
        async for chunk in gemini_chat_stream(messages_for_title, "gemini-1.5-flash-latest", GenerationConfig(temperature=0.3)):
             try:
                cleaned_chunk = chunk.decode('utf-8').replace('data: ', '').strip()
                if cleaned_chunk:
                    json_data = json.loads(cleaned_chunk)
                    full_title += json_data["candidates"][0]["content"]["parts"][0]["text"]
             except (json.JSONDecodeError, KeyError, IndexError):
                pass
        
        if full_title:
            new_title = full_title.strip().replace("*", "").replace("\"", "")
            await db.collection("users").document(USER_ID).collection("conversations").document(conv_id).update({
                "title": new_title
            })
            cache.pop(f"convs_{USER_ID}", None) # Invalidate cache
            logging.info(f"Generated title for {conv_id}: {new_title}")

    except Exception as e:
        logging.error(f"Failed to generate title for {conv_id}: {e}")

async def summarize_conversation_if_needed(conv_id: str, history: List):
    """Summarizes the conversation history if it's too long."""
    if not db or len(history) < 20: # Trigger summarization at 20 messages
        return
    try:
        logging.info(f"Conversation {conv_id} is long, starting summarization...")
        summary_prompt = "خلاصه ای جامع از این گفتگو تا به اینجا تهیه کن تا به عنوان تاریخچه در درخواست های بعدی استفاده شود. خلاصه باید تمام نکات کلیدی را پوشش دهد."
        messages_for_summary = history + [{"role": "user", "parts": [{"text": summary_prompt}]}]

        full_summary = ""
        async for chunk in gemini_chat_stream(messages_for_summary, "gemini-1.5-flash-latest", GenerationConfig(temperature=0.5)):
            try:
                cleaned_chunk = chunk.decode('utf-8').replace('data: ', '').strip()
                if cleaned_chunk:
                    json_data = json.loads(cleaned_chunk)
                    full_summary += json_data["candidates"][0]["content"]["parts"][0]["text"]
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        
        if full_summary:
            # Create a new summary message
            summary_message = {
                "role": "user", # Role is 'user' to provide context as if the user summarized it
                "parts": [{"text": f"[خلاصه قبلی]:\n{full_summary}"}],
                "timestamp": firestore.SERVER_TIMESTAMP
            }
            # Delete old messages (optional, be careful with this)
            # For now, we just add the summary
            await db.collection("users").document(USER_ID).collection("conversations").document(conv_id).collection("messages").add(summary_message)
            logging.info(f"Successfully added summary to conversation {conv_id}")

    except Exception as e:
        logging.error(f"Failed to summarize {conv_id}: {e}")

# --- API Routes ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "models": MODELS})

# ... (Existing API routes: /api/conversations, /api/notebook, etc. remain largely the same)
# Let's add the new "share" routes

@app.post("/api/conversations/{conv_id}/share")
async def share_conversation(conv_id: str):
    if not db: raise HTTPException(status_code=503, detail="Firestore not connected")
    
    messages = await get_messages_from_db(conv_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Conversation not found or is empty.")
        
    share_id = str(uuid.uuid4())
    await db.collection("shared_conversations").document(share_id).set({
        "messages": messages,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "original_conv_id": conv_id
    })
    return JSONResponse({"share_id": share_id})

@app.get("/share/{share_id}", response_class=HTMLResponse)
async def view_shared_conversation(request: Request, share_id: str):
    if not db: raise HTTPException(status_code=503, detail="Firestore not connected")
    
    doc_ref = db.collection("shared_conversations").document(share_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Shared conversation not found.")
    
    return templates.TemplateResponse("share.html", {
        "request": request,
        "messages": doc.to_dict().get("messages", [])
    })

# --- WebSocket Chat Endpoint ---
@app.websocket("/api/ws/chat")
async def websocket_chat(ws: WebSocket, background_tasks: BackgroundTasks):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            req = WebSocketRequest(**data)
            
            if not db:
                await ws.send_text('data: {"error": "خطا در اتصال به پایگاه داده"}\n\n')
                continue

            user_msg_doc = {
                "role": req.message.role,
                "parts": [{"text": p.text for p in req.message.parts}],
                "timestamp": firestore.SERVER_TIMESTAMP
            }
            await db.collection("users").document(USER_ID).collection("conversations").document(req.conversation_id).collection("messages").add(user_msg_doc)
            cache.pop(f"convs_{USER_ID}", None)

            history = await get_messages_from_db(req.conversation_id)

            if len(history) == 2: # After user's first and model's first reply
                background_tasks.add_task(generate_title_for_conversation, req.conversation_id, history)
            
            # background_tasks.add_task(summarize_conversation_if_needed, req.conversation_id, history)

            await ws.send_text('data: {"status": "generating"}\n\n') # Thinking indicator

            full_model_response = ""
            async for chunk in gemini_chat_stream(history, req.model, req.generation_config):
                await ws.send_bytes(chunk)
                try:
                    cleaned_chunk = chunk.decode('utf-8').replace('data: ', '').strip()
                    if cleaned_chunk:
                        json_data = json.loads(cleaned_chunk)
                        if "candidates" in json_data:
                            text_part = json_data["candidates"][0]["content"]["parts"][0]["text"]
                            full_model_response += text_part
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

            if full_model_response:
                model_msg_doc = {
                    "role": "model",
                    "parts": [{"text": full_model_response}],
                    "timestamp": firestore.SERVER_TIMESTAMP
                }
                await db.collection("users").document(USER_ID).collection("conversations").document(req.conversation_id).collection("messages").add(model_msg_doc)

    except WebSocketDisconnect:
        logging.info("WebSocket disconnected")
    except Exception as e:
        logging.error(f"WebSocket error: {e}", exc_info=True)
        try:
            error_message = json.dumps({"error": f"An unexpected server error occurred: {str(e)}"})
            await ws.send_text(f'data: {error_message}\n\n')
        except:
            pass
