import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# For image generation, you might need Vertex AI credentials
# GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
USER_ID = "main_user"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Jarvis Elite - Multi-Modal AI Gateway")
templates = Jinja2Templates(directory="templates")
client: Optional[AsyncIOMotorClient] = None

@app.on_event("startup")
async def startup():
    global client
    client = AsyncIOMotorClient(MONGO_URI)

@app.on_event("shutdown")
def shutdown():
    if client: client.close()

def get_coll(): return client["chat_ai_db"]["users"]

# Database functions remain mostly the same...

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/models")
async def get_available_models():
    """
    Returns the complete list of all 14 models, categorized by their function.
    """
    return JSONResponse(content={
        # Models are now categorized by their primary function
        "categories": {
            "chat": {
                "name": "چت و مکالمه",
                "models": {
                    "gemma-2-9b-it": "Gemma 3 (RPM: 30, RPD: 14,400)",
                    "gemini-1.5-flash-latest": "Gemini 2.0/2.5 Flash & Lite (RPM: 15-30)",
                    "gemini-1.5-pro-latest": "Gemini 2.5 Pro (RPM: 5, RPD: 100)"
                }
            },
            "image": {
                "name": "تولید تصویر",
                "models": {
                    # This would be the technical model ID for image generation
                    "imagen-2.0": "Image Generation (RPM: 10, RPD: 100)"
                }
            },
            "data": {
                "name": "تحلیل داده",
                "models": {
                    "text-embedding-004": "Gemini Embedding (RPM: 100, RPD: 1,000)"
                }
            }
        }
    })

# --- Placeholder function for Image Generation ---
async def generate_image_from_prompt(prompt: str) -> str:
    """
    This is a placeholder. In a real scenario, this would use Google's Vertex AI
    SDK to call the Imagen API and return an image URL.
    """
    logger.info(f"Placeholder: Generating image for prompt: '{prompt}'")
    # Simulate API call delay
    await asyncio.sleep(5)
    # Return a placeholder image URL
    return f"https://picsum.photos/seed/{prompt.replace(' ', '')}/512/512"


# --- Smart Backend Logic ---
async def _call_gemini_api_smart(payload: Dict, model: str, stream: bool = False):
    if not GEMINI_API_KEY: raise ValueError("کلید API گوگل تنظیم نشده است.")

    if "embedding" in model:
        action, adapted_payload = "embedContent", {"content": payload["contents"][0]}
    else:
        action, adapted_payload = ("streamGenerateContent" if stream else "generateContent"), payload

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{action}?key={GEMINI_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            res = await client.post(url, json=adapted_payload, headers={"Content-Type": "application/json"})
            res.raise_for_status()
            return res
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise RuntimeError(f"سهمیه رایگان مدل «{model}» تمام شده است. لطفاً یک مدل دیگر انتخاب کنید.")
        raise RuntimeError(f"خطای API گوگل ({e.response.status_code}): {e.response.text}")


@app.websocket("/api/ws/gateway")
async def websocket_gateway(websocket: WebSocket):
    await websocket.accept()
    
    async def gateway_task(mode: str, model: str, prompt: str):
        try:
            # ** THE MASTER LOGIC HUB **
            await websocket.send_json({"type": "status", "content": f"در حال پردازش با مدل {model}..."})
            
            if mode == "chat":
                history = await get_messages(USER_ID, "main_chat") # simplified conv_id for now
                api_msgs = [{"role": m["role"], "parts": m["parts"]} for m in history]
                api_msgs.append({"role": "user", "parts": [{"text": prompt}]})

                full_res = ""
                res = await _call_gemini_api_smart({"contents": api_msgs}, model, stream=True)
                async for chunk in res.aiter_bytes():
                    for line in chunk.decode('utf-8').splitlines():
                        if '"text":' in line:
                            try:
                                text = json.loads("{" + line.strip().rstrip(',') + "}").get("text", "")
                                await websocket.send_json({"type": "text_chunk", "content": text})
                                full_res += text
                            except Exception: continue

            elif mode == "image":
                image_url = await generate_image_from_prompt(prompt)
                await websocket.send_json({"type": "image_url", "content": image_url})

            elif mode == "data":
                api_payload = {"contents": [{"parts": [{"text": prompt}]}]}
                res = await _call_gemini_api_smart(api_payload, model, stream=False)
                vector = res.json()["embedding"]["values"]
                info_msg = f"✅ **عملیات Embedding موفق بود**\n\n- **طول بردار:** `{len(vector)}`\n- **۳ مقدار اول:** `[{vector[0]:.4f}, ...]`"
                await websocket.send_json({"type": "text_chunk", "content": info_msg})

        except Exception as e:
            await websocket.send_json({"type": "error", "content": str(e)})
        finally:
            await websocket.send_json({"type": "stream_end"})

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "generate":
                asyncio.create_task(
                    gateway_task(data["mode"], data["model"], data["prompt"])
                )
    except WebSocketDisconnect:
        logger.info("Client disconnected.")
