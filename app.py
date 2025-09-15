import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx
from google.cloud import firestore
from google.oauth2 import service_account

# ----------------- تنظیمات کلید گوگل -----------------
def load_google_credentials():
    """ بارگذاری کلید سرویس از محیط (به صورت JSON یا Base64) """
    if os.getenv("GOOGLE_CREDENTIALS_JSON"):
        return json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    elif os.getenv("GOOGLE_CREDENTIALS_JSON_BASE64"):
        decoded = base64.b64decode(os.environ["GOOGLE_CREDENTIALS_JSON_BASE64"]).decode("utf-8")
        return json.loads(decoded)
    raise RuntimeError("❌ هیچ کلید سرویسی پیدا نشد. متغیر محیطی GOOGLE_CREDENTIALS_JSON یا BASE64 را ست کنید.")

creds_info = load_google_credentials()
credentials = service_account.Credentials.from_service_account_info(creds_info)
db = firestore.Client(credentials=credentials, project=creds_info["project_id"])

# ----------------- FastAPI -----------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ----------------- مدل‌ها (فقط رایگان) -----------------
MODELS = {
    "gemini-1.5-flash-latest": {
        "name": "⚡ Gemini 1.5 Flash (Latest)",
        "desc": "سریع، ارزان، مناسب گفتگو و کاربردهای real-time."
    },
    "gemini-1.5-pro-latest": {
        "name": "🧠 Gemini 1.5 Pro (Latest)",
        "desc": "قدرت‌مندتر، برای وظایف پیچیده‌تر و reasoning عمیق."
    },
    "gemini-1.0-pro": {
        "name": "💎 Gemini 1.0 Pro",
        "desc": "نسل قبلی، هنوز پشتیبانی می‌شود اما توصیه نمی‌شود برای پروژه جدید."
    },
    "gemini-pro": {
        "name": "💎 Gemini Pro (Legacy)",
        "desc": "مدل قدیمی‌تر، صرفاً برای سازگاری."
    },
    "gemini-ultra": {
        "name": "🚀 Gemini Ultra (Enterprise)",
        "desc": "پیشرفته‌ترین مدل؛ نیازمند دسترسی ویژه (معمولاً رایگان نیست)."
    },
}

# ----------------- صفحه اصلی -----------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "models": MODELS})

# ----------------- API: گفتگوها -----------------
@app.get("/api/conversations")
async def get_conversations():
    docs = db.collection("conversations").order_by("created_at", direction=firestore.Query.DESCENDING).stream()
    conversations = [{"id": d.id, "title": d.to_dict().get("title", "گفتگو بدون عنوان")} for d in docs]
    return conversations

@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    doc_ref = db.collection("conversations").document(conv_id)
    doc = doc_ref.get()
    if not doc.exists:
        return []
    return doc.to_dict().get("messages", [])

@app.post("/api/conversations")
async def create_conversation():
    doc_ref = db.collection("conversations").document()
    doc_ref.set({"title": "گفتگو جدید", "messages": [], "created_at": firestore.SERVER_TIMESTAMP})
    return {"id": doc_ref.id, "title": "گفتگو جدید"}

# ----------------- API: دفترچه -----------------
@app.get("/api/notebook")
async def get_notebook():
    doc = db.collection("notebook").document("main").get()
    return {"content": doc.to_dict().get("content", "") if doc.exists else ""}

@app.post("/api/notebook")
async def save_notebook(data: dict):
    db.collection("notebook").document("main").set({"content": data.get("content", "")})
    return {"status": "ok"}

# ----------------- WebSocket: چت -----------------
@app.websocket("/api/ws/chat")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        await ws.send_text("data: " + json.dumps({"error": "❌ GEMINI_API_KEY تنظیم نشده است"}))
        await ws.close()
        return

    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            try:
                data = await ws.receive_json()
                conv_id = data.get("conversation_id")
                model = data.get("model", "gemini-1.5-flash")
                message = data.get("message")

                # اضافه کردن پیام کاربر
                conv_ref = db.collection("conversations").document(conv_id)
                conv = conv_ref.get()
                if conv.exists:
                    conv_data = conv.to_dict()
                else:
                    conv_data = {"title": "گفتگو جدید", "messages": []}

                conv_data["messages"].append(message)
                conv_ref.set(conv_data)

                # درخواست به Gemini
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}",
                    json={"contents": [message]},
                )
                if response.status_code != 200:
                    await ws.send_text("data: " + json.dumps({"error": response.text}))
                    continue

                result = response.json()
                await ws.send_text("data: " + json.dumps(result))

                # ذخیره پیام مدل
                conv_data["messages"].append({"role": "model", "parts": result.get("candidates", [])[0].get("content", {}).get("parts", [])})
                conv_ref.set(conv_data)

            except Exception as e:
                await ws.send_text("data: " + json.dumps({"error": str(e)}))
                await asyncio.sleep(1)
