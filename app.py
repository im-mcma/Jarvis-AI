import os
import uuid
import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from google.cloud import firestore

# --- Configuration & Initialization ---
load_dotenv()

# Initialize FastAPI app
app = FastAPI(title="Jarvis-Ai")
templates = Jinja2Templates(directory="templates")

# Initialize Firestore Client
# نکته مهم: برای Render.com، باید محتوای فایل JSON کلید سرویس اکانت را
# در یک متغیر محیطی به نام GOOGLE_CREDENTIALS_JSON کپی کنید.
# این کد آن را می‌خواند و برای اتصال استفاده می‌کند.
try:
    if "GOOGLE_CREDENTIALS_JSON" in os.environ:
        import json
        creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_info(creds_json)
        db = firestore.AsyncClient(credentials=credentials)
        print("Firestore connected via environment variable.")
    else:
        # For local development, it uses the GOOGLE_APPLICATION_CREDENTIALS file path
        db = firestore.AsyncClient()
        print("Firestore connected via local credentials file.")
except Exception as e:
    print(f"FATAL: Could not connect to Firestore. {e}")
    db = None

# Gemini API Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL_DEFAULT = "gemini-1.5-flash-latest"
MODELS = {
    "gemini-1.5-flash-latest": {"name": "⚡️ Gemini 1.5 Flash"},
    "gemini-1.5-pro-latest": {"name": "🧠 Gemini 1.5 Pro"},
    "gemini-pro": {"name": "💎 Gemini Pro"},
}
# A persistent user ID for single-user setup
USER_ID = "main_user"

# --- Firestore Helper Functions ---
async def get_conversations_from_db():
    if not db: return []
    convs_ref = db.collection("users").document(USER_ID).collection("conversations").order_by("last_updated", direction=firestore.Query.DESCENDING)
    docs = await convs_ref.stream()
    return [{"id": doc.id, "title": doc.to_dict().get("title", "گفتگوی بدون عنوان")} async for doc in docs]

async def get_messages_from_db(conversation_id: str):
    if not db: return []
    messages_ref = db.collection("users").document(USER_ID).collection("conversations").document(conversation_id).collection("messages").order_by("timestamp")
    docs = await messages_ref.stream()
    return [doc.to_dict() async for doc in docs]

# --- Gemini API Client (Streaming) ---
async def gemini_chat_stream(messages: list, model: str):
    if not GEMINI_API_KEY:
        yield 'data: {"error": "کلید API برای Gemini تنظیم نشده است."}\n\n'
        return

    url = f"{GEMINI_API_BASE}/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}&alt=sse"
    
    # Add system instruction for Persian responses
    persian_instruction = {
        "role": "user",
        "parts": [{"text": "شما یک دستیار هوش مصنوعی به نام Jarvis هستید. همیشه به زبان فارسی روان و محترمانه پاسخ بده."}]
    }
    persian_response = {"role": "model", "parts": [{"text": "باشه، حتماً. من جارویس هستم و همیشه به زبان فارسی پاسخ خواهم داد."}]}
    
    final_messages = [persian_instruction, persian_response] + messages

    payload = {"contents": final_messages}
    
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    error_content = await response.aread()
                    yield f'data: {{"error": "API Error: {error_content.decode()}"}}\n\n'
                    return
                async for chunk in response.aiter_text():
                    yield chunk
    except Exception as e:
        yield f'data: {{"error": "Request Error: {str(e)}"}}\n\n'

# --- API Endpoints ---
@app.get("/")
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "models": MODELS})

@app.get("/api/conversations")
async def get_conversations():
    if not db: return JSONResponse({"error": "Firestore not connected"}, status_code=500)
    conversations = await get_conversations_from_db()
    return JSONResponse(conversations)

@app.post("/api/conversations")
async def create_conversation():
    if not db: return JSONResponse({"error": "Firestore not connected"}, status_code=500)
    new_conv_id = str(uuid.uuid4())
    await db.collection("users").document(USER_ID).collection("conversations").document(new_conv_id).set({
        "title": "گفتگوی جدید",
        "last_updated": firestore.SERVER_TIMESTAMP,
    })
    return JSONResponse({"id": new_conv_id, "title": "گفتگوی جدید"})

@app.get("/api/conversations/{conversation_id}")
async def get_messages(conversation_id: str):
    if not db: return JSONResponse({"error": "Firestore not connected"}, status_code=500)
    messages = await get_messages_from_db(conversation_id)
    return JSONResponse(messages)

@app.get("/api/notebook")
async def get_notebook():
    if not db: return JSONResponse({"content": ""})
    doc_ref = db.collection("users").document(USER_ID).collection("notebook").document("main")
    doc = await doc_ref.get()
    return JSONResponse({"content": doc.to_dict().get("content", "") if doc.exists else ""})

@app.post("/api/notebook")
async def update_notebook(request: Request):
    if not db: return JSONResponse({"error": "Firestore not connected"}, status_code=500)
    data = await request.json()
    await db.collection("users").document(USER_ID).collection("notebook").document("main").set({
        "content": data.get("content"),
        "last_updated": firestore.SERVER_TIMESTAMP
    })
    return JSONResponse({"status": "success"})

# --- WebSocket for Real-time Chat ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            conversation_id = data.get("conversation_id")
            model = data.get("model", MODEL_DEFAULT)
            user_message = data.get("message")

            if not db or not conversation_id:
                await websocket.send_text('data: {"error": "خطا در اتصال به پایگاه داده یا شناسه گفتگو نامعتبر است."}\n\n')
                continue

            # Save user message to Firestore
            user_message_doc = {"role": "user", "parts": user_message["parts"], "timestamp": firestore.SERVER_TIMESTAMP}
            await db.collection("users").document(USER_ID).collection("conversations").document(conversation_id).collection("messages").add(user_message_doc)
            
            # Update conversation title and timestamp
            if len(await get_messages_from_db(conversation_id)) <= 1: # first message
                 # simple title generation from the first 40 chars of the prompt
                new_title = user_message["parts"][0].get("text", "گفتگو")[:40]
                await db.collection("users").document(USER_ID).collection("conversations").document(conversation_id).update({
                    "title": new_title, "last_updated": firestore.SERVER_TIMESTAMP
                })

            # Get history and call Gemini
            history = await get_messages_from_db(conversation_id)
            model_response_text = ""
            
            stream_generator = gemini_chat_stream(history, model)
            async for chunk in stream_generator:
                await websocket.send_text(chunk)
                if chunk.startswith('data: '):
                    try:
                        json_data = json.loads(chunk[6:])
                        model_response_text += json_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    except:
                        pass # Ignore parsing errors on intermediate chunks
            
            # Save final model response to Firestore
            if model_response_text:
                model_message_doc = {"role": "model", "parts": [{"text": model_response_text}], "timestamp": firestore.SERVER_TIMESTAMP}
                await db.collection("users").document(USER_ID).collection("conversations").document(conversation_id).collection("messages").add(model_message_doc)

    except WebSocketDisconnect:
        print("WebSocket client disconnected.")
    except Exception as e:
        print(f"An error occurred in the WebSocket: {e}")
        try:
            await websocket.send_text(f'data: {{"error": "یک خطای داخلی در سرور رخ داد: {str(e)}"}}')
        except:
            pass
