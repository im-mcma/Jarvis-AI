import os
import time
import logging
import base64
import threading
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import chainlit as cl
import bcrypt
import jwt
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
import google.generativeai as genai
from dotenv import load_dotenv

# ------------------------------------------------------------
# تنظیمات لاگ
# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("jarvis-argus-chainlit")

# ------------------------------------------------------------
# بارگذاری متغیرهای محیطی
# ------------------------------------------------------------
load_dotenv()
MONGO_URI = os.environ.get("MONGO_URI")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")

# مقدار پیش‌فرض/کنترلی
APP_TITLE = "Jarvis Argus — آرگوس (Chainlit)"
PAGE_SIZE_MESSAGES = 50
MIN_SECONDS_BETWEEN_PROMPTS = 0.6

if not MONGO_URI:
    logger.warning("MONGO_URI تنظیم نشده است — DB غیرفعال خواهد بود.")
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY تنظیم نشده — فراخوانی مدل غیرفعال خواهد شد.")
if not JWT_SECRET_KEY:
    logger.warning("JWT_SECRET_KEY تنظیم نشده — توکن‌ها امن نیستند.")

# ------------------------------------------------------------
# پیکربندی Gemini (اگر کلید موجود باشد)
# ------------------------------------------------------------
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info("Gemini API configured")
except Exception as e:
    logger.exception("خطا هنگام configure کردن Gemini: %s", e)

# ------------------------------------------------------------
# لیست مدل‌ها — دقیقاً مطابق با نسخهٔ شما
# ------------------------------------------------------------
MODELS = {
    "چت متنی": {
        "Gemini 2.5 Pro": {"id": "gemini-2.5-pro", "RPM": 5, "RPD": 100, "capabilities": "استدلال و پاسخ‌گویی پیچیده"},
        "Gemini 2.5 Flash": {"id": "gemini-2.5-flash", "RPM": 10, "RPD": 250, "capabilities": "متعادل: سرعت و دقت"},
        "Gemini 2.5 Flash-Lite": {"id": "gemini-2.5-flash-lite", "RPM": 15, "RPD": 1000, "capabilities": "بهینه برای حجم بالا"},
        "Gemini 2.0 Pro": {"id": "gemini-2.0-pro", "RPM": 15, "RPD": 200, "capabilities": "پایدار و سازگار"},
        "Gemini 2.0 Flash": {"id": "gemini-2.0-flash", "RPM": 30, "RPD": 200, "capabilities": "سریع، برای درخواست‌های فراوان"}
    },
    "تولید تصویر": {
        "Gemini 2.5 Flash Image": {"id": "gemini-2.5-flash-image-preview", "RPM": 10, "RPD": 100, "capabilities": "تولید و ویرایش تصویر"},
        "Gemini 2.0 Flash Image": {"id": "gemini-2.0-flash-image", "RPM": 15, "RPD": 200, "capabilities": "پایدار در تولید تصویر"}
    },
    "تولید ویدیو": {
        "Veo 3": {"id": "veo-3", "RPM": 5, "RPD": 50, "capabilities": "تولید ویدیو و صدا/افکت"}
    }
}

# ------------------------------------------------------------
# کلاس JarvisArgus — منطق اصلی برنامه (با کامنت‌های بلوکی)
# ------------------------------------------------------------
class JarvisArgus:
    """
    کلاس اصلی که:
    - اتصال به MongoDB را مدیریت می‌کند (sync client اما با asyncio.to_thread اجرا می‌شود)
    - عملیات احراز هویت و JWT را انجام می‌دهد
    - پیام‌ها را ذخیره و بازیابی می‌کند
    - استریم پاسخ Gemini را مدیریت می‌کند
    """

    def __init__(self):
        # اتصال pymongo (همگام)
        self.client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000) if MONGO_URI else None
        if self.client:
            try:
                self.client.admin.command('ping')
                logger.info("MongoDB connected")
            except Exception as e:
                logger.exception("MongoDB connection failed: %s", e)
                self.client = None

        if self.client:
            self.db = self.client["jarvis_argus_db"]
            self.users = self.db["users"]
            self.conversations = self.db["conversations"]
        else:
            self.db = self.users = self.conversations = None

    # ---------------- امنیتی: hash / verify / token ----------------
    def _hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def _verify_password(self, password: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception:
            return False

    def _create_jwt(self, user_info: dict) -> str:
        payload = {
            "sub": user_info["id"],
            "name": user_info.get("name"),
            "email": user_info.get("email"),
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
        }
        return jwt.encode(payload, JWT_SECRET_KEY or "dev-secret", algorithm="HS256")

    def _decode_jwt(self, token: str) -> Optional[dict]:
        if not token:
            return None
        try:
            return jwt.decode(token, JWT_SECRET_KEY or "dev-secret", algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return None
        except Exception as e:
            logger.warning("Invalid JWT: %s", e)
            return None

    # ---------------- DB wrappers (async-friendly using to_thread) ----------------
    async def db_get_user_by_email(self, email: str) -> Optional[dict]:
        if not self.users or not email:
            return None
        return await asyncio.to_thread(lambda: self.users.find_one({"email": email.lower().strip()}))

    async def db_create_user(self, name: str, email: str, password: str) -> Optional[str]:
        if not self.users:
            return None
        doc = {"name": name, "email": email.lower().strip(), "password": self._hash_password(password), "created_at": datetime.now(timezone.utc)}
        res = await asyncio.to_thread(lambda: self.users.insert_one(doc))
        logger.info("New user created: %s", res.inserted_id)
        return str(res.inserted_id)

    async def db_create_conversation(self, user_id: str, title: str) -> Optional[str]:
        if not self.conversations:
            return None
        conv = {"user_id": ObjectId(user_id), "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
        res = await asyncio.to_thread(lambda: self.conversations.insert_one(conv))
        return str(res.inserted_id)

    async def db_get_conversations(self, user_id: str) -> List[dict]:
        if not self.conversations:
            return []
        return await asyncio.to_thread(lambda: list(self.conversations.find({"user_id": ObjectId(user_id)}).sort("created_at", DESCENDING)))

    async def db_get_messages(self, conv_id: str, limit: int = PAGE_SIZE_MESSAGES, offset: int = 0) -> List[dict]:
        if not self.conversations:
            return []
        def agg():
            pipeline = [
                {"$match": {"_id": ObjectId(conv_id)}},
                {"$project": {"messages": {"$slice": ["$messages", -(offset + limit), limit]}}}
            ]
            result = list(self.conversations.aggregate(pipeline))
            return result[0]['messages'] if result else []
        return await asyncio.to_thread(agg)

    async def db_append_message(self, conv_id: str, msg: dict):
        if not self.conversations:
            return
        await asyncio.to_thread(lambda: self.conversations.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": msg}}))

    # ---------------- کمک: تبدیل فایل تصویر ارسال‌شده به data URI ----------------
    def _image_element_to_data_uri(self, el) -> Optional[str]:
        # el.path is available for uploaded files in Chainlit
        try:
            path = el.path
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            mime = "image/jpeg"
            if path.lower().endswith('.png'):
                mime = "image/png"
            return f"data:{mime};base64,{data}"
        except Exception as e:
            logger.warning("Error converting image to data-uri: %s", e)
            return None

    # ---------------- استریم Gemini: producer (thread) -> asyncio.Queue -> consumer (async)
    async def stream_gemini_to_chainlit(self, history: List[Dict[str, Any]], model_id: str, msg: cl.Message):
        """
        این تابع یک thread تولید می‌کند که از SDK جمنای (که ممکن است sync باشد) خروجی می‌گیرد
        و آن را در asyncio.Queue قرار می‌دهد؛ سپس consumer توکن‌ها را به پیام Chainlit استریم می‌کند.
        """
        if not GEMINI_API_KEY:
            await cl.Message(content="**خطا: کلید API جمنای تنظیم نشده است.**").send()
            return

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def producer():
            try:
                api_history = []
                for m in history:
                    api_history.append({
                        "role": "user" if m.get("role") == "user" else "model",
                        "parts": [{"text": m.get("content", "")}]
                    })

                model = genai.GenerativeModel(model_id)
                stream = model.generate_content(api_history, stream=True)
                for chunk in stream:
                    text = getattr(chunk, "text", None) or ""
                    if text:
                        asyncio.run_coroutine_threadsafe(q.put(text), loop)
            except Exception as e:
                logger.exception("Gemini streaming error: %s", e)
                asyncio.run_coroutine_threadsafe(q.put(f"[ERROR: {e}]"), loop)
            finally:
                asyncio.run_coroutine_threadsafe(q.put(None), loop)

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()

        # consumer: دریافت از صف و استریم به پیام
        while True:
            token = await q.get()
            if token is None:
                break
            await msg.stream_token(token)

        # نهایی‌سازی پیام (msg.content به‌روز می‌شود)
        await msg.update()

    # ---------------- مدیریت فرمان‌ها (/signup, /login, ...) ----------------
    async def handle_command(self, message: cl.Message):
        content = (message.content or "").strip()
        parts = content.split()
        cmd = parts[0].lower() if parts else ""

        if cmd == "/help":
            await cl.Message(content=(
                "دستورات:
"
                "/signup نام ایمیل رمز — ثبت‌نام
"
                "/login ایمیل رمز — ورود
"
                "/new — مکالمه جدید
"
                "/convs — لیست مکالمات
"
                "/select <conv_id> — انتخاب مکالمه
"
            )).send()
            return

        if cmd == "/signup":
            if len(parts) < 4:
                await cl.Message(content="فرمت: /signup نام ایمیل رمز").send()
                return
            name, email, password = parts[1], parts[2], parts[3]
            existing = await self.db_get_user_by_email(email)
            if existing:
                await cl.Message(content="این ایمیل قبلاً ثبت شده.").send()
                return
            uid = await self.db_create_user(name, email, password)
            if uid:
                token = self._create_jwt({"id": uid, "name": name, "email": email})
                cl.user_session.set("jwt", token)
                cl.user_session.set("user", {"id": uid, "name": name, "email": email})
                await cl.Message(content="ثبت‌نام موفق — وارد شدید.").send()
            else:
                await cl.Message(content="خطا در ثبت‌نام (DB)").send()
            return

        if cmd == "/login":
            if len(parts) < 3:
                await cl.Message(content="فرمت: /login ایمیل رمز").send()
                return
            email, password = parts[1], parts[2]
            user = await self.db_get_user_by_email(email)
            if user and self._verify_password(password, user["password"]):
                user_info = {"id": str(user["_id"]), "name": user.get("name", "کاربر"), "email": user["email"]}
                token = self._create_jwt(user_info)
                cl.user_session.set("jwt", token)
                cl.user_session.set("user", user_info)
                await cl.Message(content="ورود موفق — خوش آمدید!").send()
            else:
                await cl.Message(content="ایمیل یا رمز نادرست است.").send()
            return

        if cmd == "/new":
            cl.user_session.set("messages", [])
            cl.user_session.set("current_conv_id", None)
            await cl.Message(content="مکالمهٔ جدید ساخته شد.").send()
            return

        if cmd == "/convs":
            user = cl.user_session.get("user")
            if not user:
                await cl.Message(content="ابتدا /login انجام دهید.").send()
                return
            convs = await self.db_get_conversations(user["id"]) or []
            if not convs:
                await cl.Message(content="هیچ مکالمه‌ای وجود ندارد.").send()
                return
            text = "لیست مکالمات:
" + "
".join([f"{str(c['_id'])} — {c.get('title','بدون عنوان')}" for c in convs[:20]])
            await cl.Message(content=text).send()
            return

        if cmd == "/select":
            if len(parts) < 2:
                await cl.Message(content="فرمت: /select <conv_id>").send()
                return
            conv_id = parts[1]
            msgs = await self.db_get_messages(conv_id)
            cl.user_session.set("current_conv_id", conv_id)
            cl.user_session.set("messages", msgs or [])
            await cl.Message(content=f"مکالمه {conv_id} انتخاب شد. {len(msgs)} پیام بارگذاری شد.").send()
            return

        await cl.Message(content="فرمان ناشناخته — از /help استفاده کن.").send()

    # ---------------- پردازش پیام‌های کاربر (غیر فرمانی) ----------------
    async def handle_user_message(self, message: cl.Message):
        user = cl.user_session.get("user")
        if not user:
            await cl.Message(content="ابتدا /signup یا /login را اجرا کنید.").send()
            return

        now_ts = time.time()
        last_ts = cl.user_session.get("last_prompt_ts") or 0
        if now_ts - last_ts < MIN_SECONDS_BETWEEN_PROMPTS:
            await cl.Message(content="لطفاً کمی آهسته‌تر پیام بفرستید.").send()
            return
        cl.user_session.set("last_prompt_ts", now_ts)

        # آماده‌سازی پیام کاربر و افزودن تصاویر (در صورت وجود)
        messages = cl.user_session.get("messages") or []
        if message.elements:
            imgs = [el for el in (message.elements or []) if getattr(el, 'mime', '') and 'image' in el.mime]
            if imgs:
                data_uri = self._image_element_to_data_uri(imgs[0])
                if data_uri:
                    user_msg = {"role": "user", "content": (message.content or "") + "
[image:data-uri...]"}
                else:
                    user_msg = {"role": "user", "content": (message.content or "")}
            else:
                user_msg = {"role": "user", "content": (message.content or "")}
        else:
            user_msg = {"role": "user", "content": (message.content or "")}

        messages.append(user_msg)
        cl.user_session.set("messages", messages)

        # ذخیره در DB
        conv_id = cl.user_session.get("current_conv_id")
        if not conv_id:
            conv_id = await self.db_create_conversation(user["id"], (message.content or "").strip()[:40])
            cl.user_session.set("current_conv_id", conv_id)

        await self.db_append_message(conv_id, user_msg)

        # انتخاب مدل
        selected_model = cl.user_session.get("selected_model_id") or "gemini-2.5-flash"
        is_media_model = any(selected_model == info["id"] for cat in ["تولید تصویر", "تولید ویدیو"] for info in MODELS.get(cat, {}).values())
        if is_media_model:
            ai_content = f"مدل انتخاب شده `{selected_model}` برای تولید رسانه است و در حالت چت متن پشتیبانی نمی‌شود."
            await cl.Message(content=ai_content, author="assistant").send()
            await self.db_append_message(conv_id, {"role": "assistant", "content": ai_content})
            return

        # ایجاد پیام Placeholder و استریم
        msg = cl.Message(content="", author="assistant")
        await self.stream_gemini_to_chainlit(messages, selected_model, msg)

        # بعد از اتمام استریم، محتوا را از msg بخوان و ذخیره کن
        final_content = msg.content
        await self.db_append_message(conv_id, {"role": "assistant", "content": final_content})


# ------------------------------------------------------------
# نمونه‌سازی و Hookهای Chainlit
# ------------------------------------------------------------
jarvis = JarvisArgus()

@cl.set_starters
async def set_starters():
    return [cl.Starter(label="سلام — معرفی", message="سلام! من آرگوس هستم. از /help برای دیدن دستورات استفاده کن.")]

@cl.on_chat_start
async def on_chat_start():
    # مقداردهی اولیه session
    cl.user_session.set("messages", [])
    cl.user_session.set("current_conv_id", None)
    cl.user_session.set("last_prompt_ts", 0)
    default_model = "gemini-2.5-flash"
    cl.user_session.set("selected_model_id", default_model)

    # ارسال تنظیمات برای انتخاب مدل (ChatSettings)
    model_values = []
    for cat, group in MODELS.items():
        for name, info in group.items():
            model_values.append(f"{cat}: {name}||{info['id']}")

    select = cl.Select(id="Model", label="انتخاب مدل", values=model_values, initial_index=0)
    settings = await cl.ChatSettings([select]).send()
    sel = settings.get("Model")
    if sel and "||" in sel:
        _id = sel.split("||", 1)[1]
        cl.user_session.set("selected_model_id", _id)

    await cl.Message(content=f"خوش آمدید به {APP_TITLE}. از /help برای دستورالعمل‌ها استفاده کنید.").send()

@cl.on_message
async def main(message: cl.Message):
    text = (message.content or "").strip()
    if text.startswith("/"):
        await jarvis.handle_command(message)
        return
    await jarvis.handle_user_message(message)
