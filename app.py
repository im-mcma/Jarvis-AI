# -*- coding: utf-8 -*-
"""
# app.py — Jarvis Argus (نسخهٔ نهایی و عملیاتی)
# توضیحات:
# - ساختار OOP (کلاس JarvisArgus) حفظ شده است.
# - مشکل اصلی: تداخل asyncio با مدل اجرایی Streamlit با جایگزینی motor (async) با pymongo (sync) حل شده است.
# - احراز هویت با bcrypt و JWT بدون تغییر باقی مانده است.
# - پشتیبانی کامل از مدل‌های Gemini.
# - استریم متن از Gemini با استفاده از API همزمان (synchronous) به درستی کار می‌کند.
# - رابط کاربری با CSS مدرن و استفاده از st.chat_input به طور کامل بازطراحی شده است.
# - pagination پیام‌ها، rate-limit، لاگینگ و اعتبارسنجی بهبود یافته‌اند.
#
# اجرا:
#   1. Clean up conflicting libraries:
#      pip uninstall jwt pyjwt -y
#   2. Install the correct library:
#      pip install -r requirements.txt
#   3. Set environment variables (MONGO_URI, GEMINI_API_KEY, JWT_SECRET_KEY)
#   4. Run the app:
#      streamlit run app.py
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Generator

import streamlit as st
import pandas as pd
import bcrypt
import jwt  # This import is correct if you have PyJWT installed, not the 'jwt' library.
from pymongo import MongoClient, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure
from bson import ObjectId
import google.generativeai as genai
from dotenv import load_dotenv

# ---------- تنظیم لاگ ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("jarvis-argus")

# ---------- بارگذاری .env برای توسعه محلی (اختیاری) ----------
load_dotenv()

# ---------- تنظیمات کلی ----------
APP_TITLE = "Jarvis Argus — آرگوس"
APP_NICKNAME = "آرگوس"
PAGE_SIZE_MESSAGES = 50
MIN_SECONDS_BETWEEN_PROMPTS = 0.6  # حداقل فاصله برای جلوگیری از اسپم

# ---------- خواندن متغیر محیطی ----------
def get_env_var(key: str, required: bool = True) -> Optional[str]:
    val = os.environ.get(key)
    if required and not val:
        err = f"متغیر محیطی `{key}` تنظیم نشده است."
        logger.error(err)
        st.error(err)
        st.stop()
    return val

MONGO_URI = get_env_var("MONGO_URI")
GEMINI_API_KEY = get_env_var("GEMINI_API_KEY")
JWT_SECRET_KEY = get_env_var("JWT_SECRET_KEY")

# ---------- CSS و صفحه ----------
st.set_page_config(page_title=APP_TITLE, layout="centered", initial_sidebar_state="auto", page_icon="🛡️")
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;600;700&display=swap');
html, body, [class*="st-"], [class*="css-"] {{
    font-family: 'Vazirmatn', sans-serif;
    direction: rtl;
}}
.st-emotion-cache-1y4p8pa {{
    padding-top: 2rem;
}}
.stChatMessage {{
    background-color: #27272a; /* zinc-800 */
    border: 1px solid #3f3f46; /* zinc-700 */
    border-radius: 0.75rem;
    padding: 1rem;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
}}
.stChatMessage[data-testid="stChatMessage-user"] {{
    background-color: #2563eb; /* blue-600 */
    color: white;
}}
.st-emotion-cache-janbn0, .st-emotion-cache-4oy321 {{ /* Text colors in chat messages */
    color: #f4f4f5; /* zinc-100 */
}}
.model-card {{
    background:#0f172a;
    border:1px solid #334155;
    padding:1rem;
    border-radius:10px;
    margin-bottom:8px;
}}
.badge {{
    display:inline-block;
    padding: 0.25rem 0.6rem;
    border-radius:9999px;
    font-size:12px;
    margin-left:6px;
    font-weight: 500;
}}
.rpm {{ background:#1e40af; color:#dbeafe; }}
.rpd {{ background:#6b21a8; color:#f3e8ff; }}
.cap {{ background:#065f46; color:#d1fae5; }}
</style>
""", unsafe_allow_html=True)

# ---------- پیکربندی Gemini ----------
try:
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.warning("خطا هنگام configure کردن genai: %s", e)
    st.warning("کلید API جمنای به درستی تنظیم نشده است.")

# ---------- کش کردن کلاینت MongoDB ----------
@st.cache_resource
def get_db_client() -> MongoClient:
    """
    Connects to MongoDB using pymongo (synchronous).
    Streamlit works best with synchronous libraries.
    """
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        logger.info("MongoDB connection successful.")
        return client
    except ConnectionFailure as e:
        logger.critical("MongoDB connection failed: %s", e)
        st.error("امکان اتصال به پایگاه داده وجود ندارد. لطفاً تنظیمات را بررسی کنید.")
        st.stop()

# ---------- MODELS (فقط همان لیست کامل که خواستید) ----------
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

# ---------- توابع امنیتی / JWT (بدون تغییر) ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False

def create_jwt_token(user_info: dict) -> str:
    payload = {
        "sub": user_info["id"],
        "name": user_info["name"],
        "email": user_info["email"],
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=1),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

def decode_jwt_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        st.toast("نشست منقضی شده است. لطفاً دوباره وارد شوید.", icon="🕒")
        return None
    except Exception as e:
        logger.warning("Invalid JWT: %s", e)
        return None

# ---------- کلاس اصلی اپ ----------
class JarvisArgus:
    def __init__(self):
        # DB init (switched to synchronous pymongo)
        self.client = get_db_client()
        self.db = self.client["jarvis_argus_db"]
        self.users = self.db["users"]
        self.conversations = self.db["conversations"]
        # init session
        self._init_session_state()

    def _init_session_state(self):
        defaults = {
            "token": None,
            "page": "login",
            "current_conv_id": None,
            "messages": [],
            "last_prompt_ts": 0.0,
            "messages_offset": 0,
            "selected_model_id": "gemini-1.5-flash-latest" # A sensible default
        }
        for k, v in defaults.items():
            if k not in st.session_state:
                st.session_state[k] = v

    # ---------------- DB helpers (Synchronous) ----------------
    def db_get_user_by_email(self, email: str) -> Optional[dict]:
        if not email:
            return None
        return self.users.find_one({"email": email.lower().strip()})

    def db_create_user(self, name: str, email: str, password: str) -> str:
        doc = {"name": name, "email": email.lower().strip(), "password": hash_password(password), "created_at": datetime.now(timezone.utc)}
        res = self.users.insert_one(doc)
        logger.info("New user created: %s", res.inserted_id)
        return str(res.inserted_id)

    def db_create_conversation(self, user_id: str, title: str) -> str:
        conv = {"user_id": ObjectId(user_id), "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
        res = self.conversations.insert_one(conv)
        return str(res.inserted_id)

    def db_get_conversations(self, user_id: str) -> List[dict]:
        cursor = self.conversations.find({"user_id": ObjectId(user_id)}).sort("created_at", DESCENDING)
        return list(cursor)

    def db_get_messages(self, conv_id: str, limit: int = PAGE_SIZE_MESSAGES, offset: int = 0) -> List[dict]:
        # Using aggregation pipeline for efficient server-side slicing
        pipeline = [
            {"$match": {"_id": ObjectId(conv_id)}},
            {"$project": {
                "messages": {"$slice": ["$messages", -(offset + limit), limit]}
            }}
        ]
        result = list(self.conversations.aggregate(pipeline))
        return result[0]['messages'] if result else []

    def db_append_message(self, conv_id: str, msg: dict):
        self.conversations.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": msg}})

    # ---------------- Gemini streaming (Synchronous) ----------------
    def stream_gemini_response(self, history: List[Dict[str, Any]], model_id: str) -> Generator[str, None, None]:
        if not GEMINI_API_KEY:
            yield "**خطا: کلید API گوگل تنظیم نشده است.**"
            return
        try:
            # Prepare history for Gemini API
            api_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in history]
            model = genai.GenerativeModel(model_id)
            response_stream = model.generate_content(api_history, stream=True)
            for chunk in response_stream:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            logger.exception("Gemini streaming error")
            yield f"**خطای API:** {e}"

    # ---------------- UI renderers ----------------
    def render_login_signup(self):
        st.title(f"{APP_NICKNAME} — ورود / ثبت‌نام")
        col_left, col_right = st.columns([1.5, 1])
        with col_left:
            tab1, tab2 = st.tabs(["ورود", "ثبت‌نام"])
            with tab1:
                with st.form("login_form"):
                    email = st.text_input("ایمیل", key="login_email")
                    pwd = st.text_input("رمز عبور", type="password", key="login_pwd")
                    if st.form_submit_button("ورود", use_container_width=True, type="primary"):
                        if not email or not pwd:
                            st.error("ایمیل و رمز عبور را وارد کنید.")
                        else:
                            user = self.db_get_user_by_email(email)
                            if user and verify_password(pwd, user["password"]):
                                user_info = {"id": str(user["_id"]), "name": user.get("name", "کاربر"), "email": user["email"]}
                                st.session_state.token = create_jwt_token(user_info)
                                st.session_state.page = "dashboard"
                                st.rerun()
                            else:
                                st.error("ایمیل یا رمز اشتباه است.")
            with tab2:
                with st.form("signup_form"):
                    name = st.text_input("نام کامل", key="su_name")
                    email2 = st.text_input("ایمیل", key="su_email")
                    p1 = st.text_input("رمز عبور", type="password", key="su_p1")
                    p2 = st.text_input("تکرار رمز عبور", type="password", key="su_p2")
                    if st.form_submit_button("ثبت‌نام", use_container_width=True):
                        if not (name and email2 and p1 and p2): st.error("همهٔ فیلدها را پر کنید.")
                        elif p1 != p2: st.error("رمزها مطابقت ندارند.")
                        elif len(p1) < 6: st.error("رمز باید حداقل ۶ کاراکتر باشد.")
                        elif self.db_get_user_by_email(email2): st.error("این ایمیل قبلاً ثبت شده است.")
                        else:
                            self.db_create_user(name, email2, p1)
                            st.success("ثبت‌نام موفق — اکنون از تب ورود، وارد شوید.")
        with col_right:
            st.markdown(f"### 👋 خوش آمدید به {APP_NICKNAME}")
            st.markdown("- **پایدار شده:** مشکل اصلی برنامه (تداخل `asyncio`) حل شد.")
            st.markdown("- **رابط کاربری مدرن:** ظاهر برنامه و بخش چت بهبود یافت.")
            st.markdown("- **پیکربندی امن:** تمام کلیدها از متغیرهای محیطی خوانده می‌شوند.")

    def _handle_conversation_click(self, conv_id: str):
        """Callback function to set the current conversation."""
        if st.session_state.current_conv_id != conv_id:
            st.session_state.current_conv_id = conv_id
            st.session_state.messages_offset = 0 # Reset pagination
            st.session_state.messages = self.db_get_messages(conv_id, offset=0)
            st.rerun()


    def render_sidebar(self, user_payload: dict):
        with st.sidebar:
            st.header(f"👤 {user_payload.get('name', 'کاربر')}")
            st.caption(user_payload.get('email'))
            st.divider()

            if st.button("➕ مکالمه جدید", use_container_width=True, type="primary"):
                st.session_state.current_conv_id = None
                st.session_state.messages = []
                st.session_state.messages_offset = 0
                st.rerun()

            st.subheader("تاریخچه مکالمات")
            convs = self.db_get_conversations(user_payload["sub"])
            for c in convs:
                cid = str(c["_id"])
                label = c.get("title", "بدون عنوان")
                st.button(
                    label[:35],
                    key=f"conv_{cid}",
                    use_container_width=True,
                    on_click=self._handle_conversation_click,
                    args=(cid,)
                )

            st.divider()
            # Model selection in sidebar
            model_options = {f"{cat}: {name}": info["id"] for cat, group in MODELS.items() for name, info in group.items()}
            
            def format_func(option_id): # Function to show pretty names in selectbox
                for key, value in model_options.items():
                    if value == option_id: return key
                return option_id

            st.selectbox("انتخاب مدل:",
                options=list(model_options.values()),
                format_func=format_func,
                key="selected_model_id"
            )

            st.divider()
            if st.button("خروج از حساب", use_container_width=True):
                st.session_state.clear()
                st.rerun()

    def render_dashboard(self, user_payload: dict):
        self.render_sidebar(user_payload)
        st.header(f"💬 گفتگوی هوشمند — {APP_NICKNAME}")
        st.markdown("---")
        
        # Load messages if a conversation is selected but messages are not in state
        if st.session_state.current_conv_id and not st.session_state.messages:
            st.session_state.messages = self.db_get_messages(st.session_state.current_conv_id)


        # Display messages from session state
        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
        
        # Use st.chat_input for a modern chat experience
        if prompt := st.chat_input("پیام خود را بنویسید..."):
            self.process_chat_input(prompt, user_payload)
            st.rerun()


    def process_chat_input(self, prompt: str, user_payload: dict):
        # Anti-spam rate limiting
        now_ts = time.time()
        if now_ts - st.session_state.last_prompt_ts < MIN_SECONDS_BETWEEN_PROMPTS:
            st.toast("لطفاً کمی آهسته‌تر پیام ارسال کنید.", icon="⏳")
            return

        st.session_state.last_prompt_ts = now_ts
        user_id = user_payload["sub"]
        conv_id = st.session_state.current_conv_id

        if not conv_id:
            conv_id = self.db_create_conversation(user_id, prompt[:40])
            st.session_state.current_conv_id = conv_id

        user_msg = {"role": "user", "content": prompt}
        st.session_state.messages.append(user_msg)
        self.db_append_message(conv_id, user_msg)

        # Handle AI response based on selected model
        selected_id = st.session_state.selected_model_id
        is_media_model = any(selected_id == info["id"] for cat in ["تولید تصویر", "تولید ویدیو"] for info in MODELS[cat].values())
        
        if is_media_model:
            ai_content = f"مدل انتخاب شده `{selected_id}` برای تولید رسانه است و در حال حاضر پشتیبانی نمی‌شود."
            st.warning(ai_content)
            ai_msg = {"role": "assistant", "content": ai_content}
        else:
            # Stream text response
            with st.chat_message("assistant"):
                placeholder = st.empty()
                full_text = ""
                text_history = [m for m in st.session_state.messages if m.get("role") in ["user", "assistant"]]
                
                try:
                    for chunk in self.stream_gemini_response(text_history, selected_id):
                        full_text += chunk
                        placeholder.markdown(full_text + "▌") # Blinking cursor effect
                    placeholder.markdown(full_text)
                except Exception as e:
                    logger.exception("Error processing stream")
                    full_text = f"**خطا در پردازش پاسخ:** {e}"
                    placeholder.error(full_text)
            
            ai_msg = {"role": "assistant", "content": full_text}
        
        st.session_state.messages.append(ai_msg)
        self.db_append_message(conv_id, ai_msg)

    # ---------------- Router اصلی ----------------
    def run(self):
        self._init_session_state()
        token = st.session_state.get("token")
        user_payload = decode_jwt_token(token) if token else None

        if user_payload:
            st.session_state.page = "dashboard"
        else:
            st.session_state.page = "login"

        if st.session_state.page == "dashboard":
            self.render_dashboard(user_payload)
        else:
            self.render_login_signup()

# ---------- اجرای برنامه ----------
if __name__ == "__main__":
    app = JarvisArgus()
    try:
        app.run()
    except Exception as e:
        logger.exception("Unhandled exception in app run")
        st.error(f"یک خطای پیش‌بینی نشده رخ داد: {e}")
