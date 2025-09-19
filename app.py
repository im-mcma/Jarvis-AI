# -*- coding: utf-8 -*-
"""
Jarvis Argus - Refactored and Operational Version

Description:
- Object-Oriented Programming (OOP) structure (JarvisArgus class)
- Configuration read from environment variables (env) instead of secrets.toml
- Synchronous MongoDB access with PyMongo (more suitable for Streamlit) and resource caching
- Authentication with bcrypt and JWT
- Full support for the provided Gemini models list
- Real-time text streaming from Gemini and placeholders for media
- Message pagination, simple rate-limiting, logging, and validation
- Modern UI/UX with improved CSS and Streamlit best practices (e.g., st.chat_input)

Execution:
    export MONGO_URI="..."
    export GEMINI_API_KEY="..."
    export JWT_SECRET_KEY="..."
    streamlit run app.py

Requirements (minimum):
pip install streamlit pymongo bcrypt pyjwt google-generativeai python-dotenv pandas
"""

import os
import uuid
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Generator

import streamlit as st
import pandas as pd
import bcrypt
import jwt
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from bson import ObjectId
import google.generativeai as genai
from dotenv import load_dotenv

# ---------- Logging Setup ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("jarvis-argus")

# ---------- Load .env for local development (optional) ----------
load_dotenv()

# ---------- General Settings ----------
APP_TITLE = "Jarvis Argus — آرگوس"
APP_NICKNAME = "آرگوس"
PAGE_SIZE_MESSAGES = 30
MIN_SECONDS_BETWEEN_PROMPTS = 0.5  # Minimum delay to prevent spam

# ---------- Read Environment Variables ----------
def get_env_var(key: str, required: bool = True) -> Optional[str]:
    val = os.environ.get(key)
    if required and not val:
        err = f"Environment variable `{key}` is not set."
        logger.error(err)
        # Display error on the page if Streamlit is already running
        try:
            st.error(err)
            st.stop()
        except RuntimeError:
            raise RuntimeError(err)
    return val

MONGO_URI = get_env_var("MONGO_URI")
GEMINI_API_KEY = get_env_var("GEMINI_API_KEY")
JWT_SECRET_KEY = get_env_var("JWT_SECRET_KEY")

# ---------- Page Configuration and CSS ----------
st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="auto", page_icon="🛡️")
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

.st-emotion-cache-16txtl3 {{
    padding: 2rem 1.5rem;
}}

.stChatMessage {{
    background-color: #1f2937;
    border: 1px solid #374151;
    border-radius: 0.75rem;
    margin-bottom: 1rem;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
}}

.stChatMessage[data-testid="stChatMessage-user"] {{
    background-color: #1e3a8a;
    color: white;
}}

.st-emotion-cache-janbn0 {{
    color: #e5e7eb;
}}

.st-emotion-cache-4oy321 {{
    color: white;
}}

.main-header {{
    font-size: 2rem;
    font-weight: 700;
    color: #f9fafb;
    text-align: center;
    margin-bottom: 2rem;
}}

.model-card {{
    background: #0f172a;
    border: 1px solid #374151;
    padding: 1rem;
    border-radius: 0.75rem;
    margin-bottom: 0.5rem;
    transition: all 0.2s ease-in-out;
}}
.model-card:hover {{
    border-color: #38bdf8;
}}

.badge {{
    display: inline-block;
    padding: 0.25rem 0.6rem;
    border-radius: 9999px;
    font-size: 0.75rem;
    margin-left: 0.3rem;
    font-weight: 500;
}}

.rpm {{ background: #1e40af; color: #dbeafe; }}
.rpd {{ background: #581c87; color: #f3e8ff; }}
.cap {{ background: #065f46; color: #d1fae5; }}
</style>
""", unsafe_allow_html=True)

# ---------- Gemini Configuration ----------
try:
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.warning("Error configuring genai: %s", e)
    st.warning(f"Could not configure the Gemini API. Please check your API key. Error: {e}")

# ---------- Cache MongoDB Client ----------
@st.cache_resource
def get_db_client() -> MongoClient:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # The ismaster command is cheap and does not require auth.
        client.admin.command('ismaster')
        logger.info("MongoDB connection successful.")
        return client
    except ConnectionFailure as e:
        logger.error("MongoDB connection failed: %s", e)
        st.error("Could not connect to MongoDB. Please check the connection URI and network access.")
        st.stop()

# ---------- Models Definition (as provided) ----------
MODELS = {
    "چت متنی": {
        "Gemini 2.5 Pro": {"id": "gemini-1.5-pro-latest", "RPM": 5, "RPD": 100, "capabilities": "استدلال و پاسخ‌گویی پیچیده"},
        "Gemini 2.5 Flash": {"id": "gemini-1.5-flash-latest", "RPM": 10, "RPD": 250, "capabilities": "متعادل: سرعت و دقت"},
    },
    "تولید تصویر": {
        "Imagen 3": {"id": "imagen-3.0-generate-002", "RPM": 10, "RPD": 100, "capabilities": "تولید و ویرایش تصویر"},
    }
}

# ---------- Security / JWT Functions ----------
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
        st.toast("نشست شما منقضی شده است. لطفاً دوباره وارد شوید.", icon="🕒")
        return None
    except Exception as e:
        logger.warning("Invalid JWT: %s", e)
        return None

# ---------- Main Application Class ----------
class JarvisArgus:
    def __init__(self):
        self.client = get_db_client()
        self.db = self.client["jarvis_argus_db"]
        self.users = self.db["users"]
        self.conversations = self.db["conversations"]
        self._init_session_state()

    def _init_session_state(self):
        defaults = {
            "token": None,
            "page": "login",
            "current_conv_id": None,
            "messages": [],
            "conversations_list": [],
            "last_prompt_ts": 0.0,
            "messages_offset": 0,
            "selected_model_id": "gemini-1.5-flash-latest"
        }
        for k, v in defaults.items():
            if k not in st.session_state:
                st.session_state[k] = v

    # ---------------- DB Helpers (Synchronous) ----------------
    def db_get_user_by_email(self, email: str) -> Optional[dict]:
        if not email: return None
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
        cursor = self.conversations.find({"user_id": ObjectId(user_id)}).sort("created_at", -1)
        return list(cursor)

    def db_get_messages(self, conv_id: str, limit: int, offset: int) -> List[dict]:
        pipeline = [
            {"$match": {"_id": ObjectId(conv_id)}},
            {"$project": {
                "messages": {
                    "$slice": ["$messages", -(offset + limit), limit]
                }
            }}
        ]
        result = list(self.conversations.aggregate(pipeline))
        return result[0]['messages'] if result and 'messages' in result[0] else []

    def db_append_message(self, conv_id: str, msg: dict):
        self.conversations.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": msg}})

    # ---------------- Gemini Streaming (Synchronous) ----------------
    def stream_gemini_response(self, history: List[Dict[str, Any]], model_id: str) -> Generator[str, None, None]:
        if not GEMINI_API_KEY:
            yield "**خطا: کلید API گوگل تنظیم نشده است.**"
            return
        try:
            api_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in history]
            model = genai.GenerativeModel(model_id)
            response_stream = model.generate_content(api_history, stream=True)
            for chunk in response_stream:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            logger.exception("Gemini streaming error")
            yield f"**خطای API:** {e}"

    # ---------------- UI Renderers ----------------
    def render_login_signup(self):
        st.markdown(f'<p class="main-header">به {APP_NICKNAME} خوش آمدید</p>', unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns([1, 1.5, 1])
        with col2:
            tab_login, tab_signup = st.tabs(["ورود", "ثبت‌نام"])
            with tab_login:
                with st.form("login_form"):
                    email = st.text_input("ایمیل", key="login_email", placeholder="email@example.com")
                    pwd = st.text_input("رمز عبور", type="password", key="login_pwd", placeholder="••••••••")
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
                                st.error("ایمیل یا رمز عبور اشتباه است.")
            with tab_signup:
                with st.form("signup_form"):
                    name = st.text_input("نام کامل", key="su_name")
                    email2 = st.text_input("ایمیل", key="su_email")
                    p1 = st.text_input("رمز عبور", type="password", key="su_p1")
                    p2 = st.text_input("تکرار رمز عبور", type="password", key="su_p2")
                    if st.form_submit_button("ثبت‌نام", use_container_width=True):
                        if not all([name, email2, p1, p2]):
                            st.error("همهٔ فیلدها را پر کنید.")
                        elif p1 != p2:
                            st.error("رمزهای عبور مطابقت ندارند.")
                        elif len(p1) < 6:
                            st.error("رمز عبور باید حداقل ۶ کاراکتر باشد.")
                        elif self.db_get_user_by_email(email2):
                            st.error("این ایمیل قبلاً ثبت شده است.")
                        else:
                            self.db_create_user(name, email2, p1)
                            st.success("ثبت‌نام موفقیت‌آمیز بود. اکنون از تب ورود وارد شوید.")

    def _handle_conversation_selection(self, conv_id: str):
        if st.session_state.current_conv_id != conv_id:
            st.session_state.current_conv_id = conv_id
            st.session_state.messages_offset = 0
            st.session_state.messages = self.db_get_messages(conv_id, PAGE_SIZE_MESSAGES, 0)
    
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

            st.subheader("تاریخچه")
            convs = self.db_get_conversations(user_payload["sub"])
            for c in convs:
                cid = str(c["_id"])
                label = c.get("title", "بدون عنوان")
                st.button(label[:35], key=f"conv_{cid}", use_container_width=True, 
                          on_click=self._handle_conversation_selection, args=(cid,))

            st.divider()
            st.subheader("تنظیمات مدل")
            model_options = {f"{cat}: {name}": info["id"] for cat, group in MODELS.items() for name, info in group.items()}
            
            def format_func(option_id):
                for key, value in model_options.items():
                    if value == option_id:
                        return key
                return option_id

            st.selectbox(
                "مدل هوش مصنوعی:", 
                options=list(model_options.values()),
                format_func=format_func,
                key="selected_model_id"
            )

            st.divider()
            if st.button("خروج از حساب", use_container_width=True):
                st.session_state.clear()
                st.rerun()

    def render_chat_dashboard(self, user_payload: dict):
        self.render_sidebar(user_payload)

        # Display chat messages
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
        
        # Handle chat input
        if prompt := st.chat_input("پیام خود را بنویسید..."):
            self.process_chat_input(prompt, user_payload)

        if not st.session_state.current_conv_id:
             st.info("یک مکالمه جدید را شروع کنید یا از تاریخچه انتخاب کنید.")

    def process_chat_input(self, prompt: str, user_payload: dict):
        # Rate limit check
        now_ts = time.time()
        if now_ts - st.session_state.last_prompt_ts < MIN_SECONDS_BETWEEN_PROMPTS:
            st.toast("لطفاً کمی آهسته‌تر پیام ارسال کنید.", icon="⏳")
            return
        st.session_state.last_prompt_ts = now_ts
        
        # Create conversation if it doesn't exist
        conv_id = st.session_state.current_conv_id
        if not conv_id:
            conv_id = self.db_create_conversation(user_payload["sub"], prompt[:40])
            st.session_state.current_conv_id = conv_id

        # Append user message
        user_msg = {"role": "user", "content": prompt}
        st.session_state.messages.append(user_msg)
        self.db_append_message(conv_id, user_msg)
        
        # Stream and display AI response
        with st.chat_message("assistant"):
            placeholder = st.empty()
            full_response = ""
            text_history = [m for m in st.session_state.messages if m.get("role") != "system"] # Filter out system messages if any
            
            try:
                for chunk in self.stream_gemini_response(text_history, st.session_state.selected_model_id):
                    full_response += chunk
                    placeholder.markdown(full_response + "▌")
                placeholder.markdown(full_response)
            except Exception as e:
                full_response = f"متاسفانه خطایی رخ داد: {e}"
                placeholder.error(full_response)

        # Append AI message
        ai_msg = {"role": "assistant", "content": full_response}
        st.session_state.messages.append(ai_msg)
        self.db_append_message(conv_id, ai_msg)
        st.rerun()

    # ---------------- Main Router ----------------
    def run(self):
        token = st.session_state.get("token")
        user_payload = decode_jwt_token(token) if token else None

        if not user_payload:
            self.render_login_signup()
        else:
            self.render_chat_dashboard(user_payload)


# ---------- Run the App ----------
if __name__ == "__main__":
    app = JarvisArgus()
    app.run()
