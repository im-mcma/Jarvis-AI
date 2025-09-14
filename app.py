import os
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------- تنظیمات ---------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_DEFAULT = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

MODELS = {
    "gemini-2.5-pro": {
        "name": "🧠 Pro",
        "desc": "مدل چندمنظوره با توانایی‌های پیشرفته",
        "use_case": "چت چندمنظوره، تولید متن باکیفیت، استدلال پیچیده، درک چندرسانه‌ای، کدنویسی پیشرفته",
        "emoji": "🧠",
    },
    "gemini-2.5-flash": {
        "name": "⚡️ Flash",
        "desc": "سریع برای پردازش‌های کم‌هزینه",
        "use_case": "خلاصه‌سازی، پردازش اسناد طولانی، پاسخ سریع",
        "emoji": "⚡️",
    },
    "gemini-2.5-flash-lite": {
        "name": "⚡️ Flash Lite",
        "desc": "مقرون‌به‌صرفه برای حجم بالا",
        "use_case": "پردازش سریع با هزینه کم",
        "emoji": "⚡️",
    },
    "gemini-2.0-flash": {
        "name": "2.0 ⚡️ Flash",
        "desc": "نسخه پایدار فلش",
        "use_case": "وظایف عمومی سریع",
        "emoji": "⚡️",
    },
    "gemini-1.5-flash": {
        "name": "1.5 ⚡️ Flash",
        "desc": "مدل کوچک‌تر و سریع‌تر",
        "use_case": "کارهای سبک و روزمره",
        "emoji": "⚡️",
    },
    "gemma-3": {
        "name": "🧪 Gemma 3",
        "desc": "مدل باز برای تحقیق",
        "use_case": "تحقیق و توسعه",
        "emoji": "🧪",
    },
    "gemma-3n": {
        "name": "📱 Gemma 3n",
        "desc": "بهینه‌شده برای موبایل",
        "use_case": "کار روی موبایل و لبه",
        "emoji": "📱",
    },
    "gemini-embedding": {
        "name": "🔗 Embedding",
        "desc": "مدل تعبیه برداری",
        "use_case": "جستجو و خوشه‌بندی",
        "emoji": "🔗",
    },
}

# ---------------- اپ اصلی ----------------
app = FastAPI(title="Jarvis-Gemini")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- مدل‌ها ----------------
class ChatRequest(BaseModel):
    messages: list
    model: str = MODEL_DEFAULT


# ---------------- توابع Gemini ----------------
async def gemini_chat(messages, model=MODEL_DEFAULT):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    url = f"{GEMINI_API_BASE}/models/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": m["role"], "parts": [{"text": m["content"]}]} for m in messages]
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


# ---------------- API endpoints ----------------
@app.post("/api/chat")
async def chat(req: ChatRequest):
    resp = await gemini_chat(req.messages, req.model)
    return JSONResponse(content=resp)


@app.get("/api/models")
async def models():
    return MODELS


@app.get("/api/health")
async def health():
    return {"status": "ok", "default_model": MODEL_DEFAULT}


@app.websocket("/api/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            messages = data.get("messages", [])
            model = data.get("model", MODEL_DEFAULT)
            resp = await gemini_chat(messages, model=model)
            await websocket.send_json(resp)
    except WebSocketDisconnect:
        return
    except Exception as e:
        await websocket.send_json({"error": str(e)})


# ---------------- Static ----------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
FRONTEND_PUBLIC = os.path.join(BASE_DIR, "frontend", "public")

if os.path.isdir(FRONTEND_PUBLIC):
    app.mount("/", StaticFiles(directory=FRONTEND_PUBLIC, html=True), name="static")

@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(FRONTEND_PUBLIC, "five.ico"))


# ---------------- Run ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
