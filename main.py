# main.py

import os
import requests
import json
import base64
from io import BytesIO
from PIL import Image
import fitz  # PyMuPDF
from fastapi import FastAPI, Request, File, UploadFile, Form, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from typing import List, Optional

# -------------------- ⚙️ 1. پیکربندی و وابستگی‌ها --------------------
# خواندن امن متغیر محیطی از Render.com
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("متغیر محیطی GEMINI_API_KEY تنظیم نشده است! لطفاً آن را در بخش Environment سرویس Render خود تنظیم کنید.")

# تعریف شخصیت‌های مختلف برای مدل هوش مصنوعی
PERSONAS = {
    "Jarvis (دستیار هوشمند)": """
    You are Jarvis, a sophisticated, witty, and incredibly helpful AI assistant.
    - Your personality is confident, calm, and slightly sarcastic, but always respectful.
    - Address the user as "Sir" or "Ma'am" occasionally and naturally.
    - Keep your responses concise, with a touch of personality.
    """,
    "دستیار برنامه‌نویس": """
    You are an expert programmer AI assistant.
    - Provide clear, efficient, and well-commented code.
    - Explain complex concepts simply.
    - Prioritize best practices and modern standards.
    """,
    "نویسنده خلاق": """
    You are a creative writing assistant.
    - Help the user brainstorm ideas, write stories, poems, and scripts.
    - Use vivid imagery and engaging language.
    """
}

# -------------------- 🚀 2. کلاس‌های API --------------------
class GeminiClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        # استفاده از نسخه v1beta که قابلیت system_instruction را دارد
        self.base_url = "https://generativelace.googleapis.com/v1beta/models"

    def _pil_to_base64(self, pil_image: Image.Image) -> str:
        """تبدیل تصویر PIL به رشته Base64."""
        buffered = BytesIO()
        # کیفیت تصویر را برای کاهش حجم تنظیم کنید
        pil_image.save(buffered, format="JPEG", quality=85)
        return base64.b64encode(buffered.getvalue()).decode("utf-8")

    def _prepare_multimodal_parts(self, text: str, image: Optional[Image.Image], file_content: Optional[str]) -> List:
        """آماده‌سازی محتوای چندحالته (متن، تصویر، فایل) برای API."""
        parts = []
        
        # ترکیب متن کاربر و محتوای فایل
        combined_text = text
        if file_content:
            combined_text = f"{text}\n\n--- محتوای فایل ضمیمه شده ---\n{file_content}"
        
        if combined_text:
            parts.append({"text": combined_text.strip()})
        
        if image:
            base64_image = self._pil_to_base64(image)
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64_image}})
            
        return parts

    def generate_stream(self, model_id: str, history: list, system_prompt: str, generation_config: dict, 
                        image: Optional[Image.Image] = None, file_content: Optional[str] = None):
        """تولید محتوا از API به صورت استریم."""
        url = f"{self.base_url}/{model_id}:streamGenerateContent"
        headers = {"Content-Type": "application/json"}
        params = {"key": self.api_key}

        # آخرین پیام کاربر را از تاریخچه جدا کرده و با محتوای چندحالته ترکیب می‌کنیم
        user_message_text = ""
        if history and history[-1]['role'] == 'user':
            user_message_text = history.pop()['parts'][0]['text']

        user_parts = self._prepare_multimodal_parts(user_message_text, image, file_content)
        
        # تاریخچه را به همراه پیام جدید کاربر ارسال می‌کنیم
        api_history = history + [{"role": "user", "parts": user_parts}]

        payload = {
            "contents": api_history,
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": generation_config
        }
        
        try:
            with requests.post(url, headers=headers, params=params, json=payload, stream=True, timeout=180) as resp:
                if resp.status_code != 200:
                    error_data = resp.json()
                    error_message = error_data.get('error', {}).get('message', 'خطای ناشناس از API')
                    yield f"event: error\ndata: {json.dumps({'error': error_message})}\n\n"
                    return
                
                # استفاده از Server-Sent Events (SSE) برای ارتباط با فرانت‌اند
                for line in resp.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith('data: '):
                            try:
                                data = json.loads(decoded_line[6:])
                                text_chunk = data["candidates"][0]["content"]["parts"][0]["text"]
                                yield f"data: {json.dumps({'token': text_chunk})}\n\n"
                            except (KeyError, IndexError, json.JSONDecodeError):
                                continue
        except requests.RequestException as e:
            yield f"event: error\ndata: {json.dumps({'error': f'خطای شبکه: {str(e)}'})}\n\n"

# -------------------- 🌐 3. برنامه FastAPI --------------------
app = FastAPI(
    title="Jarvis AI Assistant API",
    description="یک ربات چت هوشمند مبتنی بر FastAPI و Gemini API، آماده برای استقرار در Render.com.",
    version="2.0.0",
)

# اتصال پوشه static برای سرو کردن فایل‌های CSS و JS
app.mount("/static", StaticFiles(directory="static"), name="static")

# ایجاد یک نمونه از کلاینت Gemini
app.state.gemini_client = GeminiClient(API_KEY)

# -------------------- 🔄 4. مدل‌های Pydantic و نقاط پایانی API --------------------
class HistoryItem(BaseModel):
    role: str
    parts: list[dict]

class ChatRequest(BaseModel):
    model_name: str
    persona_name: str
    temperature: float
    max_tokens: int
    chat_history: List[HistoryItem]

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """سرو کردن فایل اصلی HTML برنامه."""
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/chat")
async def chat_handler(
    request_data: str = Form(...),
    image: Optional[UploadFile] = File(None),
    file: Optional[UploadFile] = File(None),
):
    """مدیریت درخواست‌های چت شامل متن، تصویر و فایل."""
    try:
        data_dict = json.loads(request_data)
        req = ChatRequest(**data_dict)
    except (json.JSONDecodeError, ValidationError) as e:
        raise HTTPException(status_code=400, detail=f"فرمت درخواست نامعتبر است: {e}")

    system_prompt = PERSONAS.get(req.persona_name, "You are a helpful assistant.")
    gen_config = {"temperature": req.temperature, "maxOutputTokens": req.max_tokens}

    pil_image = None
    file_content = None

    if image:
        try:
            image_bytes = await image.read()
            pil_image = Image.open(BytesIO(image_bytes))
        except Exception:
            raise HTTPException(status_code=400, detail="فایل تصویر نامعتبر است.")
    
    if file:
        file_bytes = await file.read()
        try:
            if file.filename and file.filename.lower().endswith(".pdf"):
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                file_content = "".join([page.get_text() for page in doc])
            else:
                # برای فایل‌های متنی دیگر
                file_content = file_bytes.decode('utf-8', errors='ignore')
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"خطا در پردازش فایل: {e}")

    # تبدیل تاریخچه به فرمت مناسب
    api_history = [item.dict() for item in req.chat_history]

    return StreamingResponse(
        app.state.gemini_client.generate_stream(
            model_id=req.model_name,
            history=api_history,
            system_prompt=system_prompt,
            generation_config=gen_config,
            image=pil_image,
            file_content=file_content
        ),
        media_type="text/event-stream"
    )

# -------------------- 🏁 5. اجرای اصلی (برای Render) --------------------
# Render از یک سرور WSGI مانند Uvicorn برای اجرای برنامه استفاده می‌کند.
# نیازی به بلوک if __name__ == "__main__": uvicorn.run(...) نیست.
# Render از طریق "Start Command" برنامه را اجرا می‌کند، مثلا:
# uvicorn main:app --host 0.0.0.0 --port 10000
