#==============================================================================
# Jarvis: Senpai — Refactored single-file Streamlit app (synchronous)
#==============================================================================
"""
Usage:
  - Set environment variables: MONGO_URI, JWT_SECRET_KEY, (optional) GEMINI_API_KEY
  - Install requirements (example): pip install streamlit pymongo bcrypt pyjwt google-generative-ai python-dotenv pandas
  - Run: streamlit run app.py
"""

import os
import uuid
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import streamlit as st
import pandas as pd
import bcrypt
import jwt
from pymongo import MongoClient
from bson import ObjectId
import google.generativeai as genai
from dotenv import load_dotenv

#==============================================================================
# ۱. پیکربندی اولیه و بارگذاری ENV
#==============================================================================
load_dotenv()  # optional for local dev

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-senpai")

APP_NICKNAME = "Jarvis: Senpai"
APP_TITLE = f"{APP_NICKNAME} — دستیار هوشمند"
PAGE_SIZE_MESSAGES = 50
MIN_SECONDS_BETWEEN_PROMPTS = 0.6

def require_env(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        raise RuntimeError(f"Environment variable `{key}` is required but not set.")
    return v

# Required envs
try:
    MONGO_URI = require_env("MONGO_URI")
    JWT_SECRET_KEY = require_env("JWT_SECRET_KEY")
except Exception as e:
    st.set_page_config(page_title="Config error", layout="centered")
    st.error(f"تنظیمات محیطی ناقص: {e}")
    st.stop()

# Optional Gemini API key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.warning("genai.configure warning: %s", e)

#==============================================================================
# ۲. MODELS — کامل و طبق درخواست شما
#==============================================================================
MODELS: Dict[str, Dict[str, Dict[str, Any]]] = {
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

#==============================================================================
# ۳. استایل صفحه و کانفیگ Streamlit
#==============================================================================
st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="collapsed", page_icon="😺")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;600;700&display=swap');
html, body, [class*="st-"] { font-family: 'Vazirmatn', sans-serif; direction: rtl; }
.stChatMessage { border-radius: 12px; border: 1px solid #374151; background-color: #0b1220; margin-bottom: 1rem; padding:8px; }
.stChatMessage:has(div[data-testid="stChatMessageContent.user"]) { background-color:#063983; color:white; }
.model-card { background:#0f1724; border:1px solid #1f2937; padding:10px; border-radius:10px; margin-bottom:8px; }
.badge { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; margin-left:6px; }
.rpm { background:#063f7a; color:#fff; } .rpd { background:#6b21a8; color:#fff; } .cap { background:#064e3b; color:#fff; }
</style>
""", unsafe_allow_html=True)

#==============================================================================
# ۴. توابع امنیتی: bcrypt + JWT
#==============================================================================
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False

def create_jwt_token(user_info: dict) -> str:
    payload = {
        "sub": user_info["id"],
        "name": user_info["name"],
        "email": user_info["email"],
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

def decode_jwt_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        st.warning("نشست شما منقضی شده است — لطفاً دوباره وارد شوید.")
        return None
    except Exception as e:
        logger.warning("Invalid JWT: %s", e)
        return None

#==============================================================================
# ۵. پایگاه داده (pymongo synchronous) — ساده و قابل اتکا
#==============================================================================
client = MongoClient(MONGO_URI)
db = client["jarvis_senpai_db"]
users_coll = db["users"]
conversations_coll = db["conversations"]

def db_get_user_by_email_sync(email: str) -> Optional[dict]:
    if not email:
        return None
    return users_coll.find_one({"email": email.lower().strip()})

def db_create_user_sync(name: str, email: str, password: str) -> str:
    doc = {"name": name, "email": email.lower().strip(), "password": hash_password(password), "created_at": datetime.now(timezone.utc)}
    res = users_coll.insert_one(doc)
    logger.info("User created: %s", res.inserted_id)
    return str(res.inserted_id)

def db_create_conversation_sync(user_id: str, title: str) -> str:
    conv = {"user_id": ObjectId(user_id), "title": title or "مکالمه جدید", "created_at": datetime.now(timezone.utc), "messages": []}
    res = conversations_coll.insert_one(conv)
    return str(res.inserted_id)

def db_get_conversations_sync(user_id: str) -> List[dict]:
    cursor = conversations_coll.find({"user_id": ObjectId(user_id)}).sort("created_at", -1)
    return list(cursor)

def db_get_messages_sync(conv_id: str, limit: int = PAGE_SIZE_MESSAGES, offset: int = 0) -> List[dict]:
    doc = conversations_coll.find_one({"_id": ObjectId(conv_id)}, {"messages": 1})
    if not doc:
        return []
    msgs = doc.get("messages", []) or []
    start = max(0, len(msgs) - (offset + limit))
    end = len(msgs) - offset
    return msgs[start:end]

def db_append_message_sync(conv_id: str, msg: dict):
    conversations_coll.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": msg}})

def db_update_user_name_sync(user_id: str, name: str):
    users_coll.update_one({"_id": ObjectId(user_id)}, {"$set": {"name": name}})

#==============================================================================
# ۶. فراخوانی Gemini — sync-friendly با fallback به async اگر لازم بود
#==============================================================================
def call_gemini_sync(api_history: List[Dict[str, Any]], model_id: str) -> str:
    if not GEMINI_API_KEY:
        return "**خطا: کلید API گوگل تنظیم نشده است.**"
    try:
        model = genai.GenerativeModel(model_id)
        # try sync method if available
        if hasattr(model, "generate_content"):
            try:
                try:
                    resp = model.generate_content({"messages": api_history})
                except Exception:
                    resp = model.generate_content(api_history)
            except Exception as e:
                logger.exception("generate_content (sync) failed: %s", e)
                return f"**خطا در فراخوانی مدل (sync):** {e}"
            if getattr(resp, "text", None):
                return resp.text
            if getattr(resp, "candidates", None):
                text = ""
                for cand in resp.candidates:
                    if getattr(cand, "content", None):
                        for p in cand.content:
                            if getattr(p, "text", None):
                                text += p.text
                if text:
                    return text
            return str(resp)
        else:
            # fallback async path (safely run a fresh loop)
            async def _call_async():
                model_async = genai.GenerativeModel(model_id)
                stream = await model_async.generate_content_async(api_history, stream=False)
                if getattr(stream, "text", None):
                    return stream.text
                if getattr(stream, "candidates", None):
                    text = ""
                    for c in stream.candidates:
                        if getattr(c, "content", None):
                            for p in c.content:
                                if getattr(p, "text", None):
                                    text += p.text
                    return text or str(stream)
                return str(stream)
            try:
                return __import__("asyncio").run(_call_async())
            except Exception as e:
                logger.exception("generate_content_async fallback failed: %s", e)
                return f"**خطای async fallback:** {e}"
    except Exception as e:
        logger.exception("call_gemini_sync outer error: %s", e)
        return f"**خطا در آماده‌سازی مدل:** {e}"

def generate_media_sync(prompt: str, model_id: str) -> str:
    # placeholder media generation — replace with real Gemini Image/Video API if available
    if "image" in model_id.lower() or "imagen" in model_id.lower():
        seed = uuid.uuid4().hex[:10]
        return f"https://picsum.photos/seed/{seed}/1024/768"
    if "veo" in model_id.lower() or "video" in model_id.lower():
        return "https://www.w3schools.com/html/mov_bbb.mp4"
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"

#==============================================================================
# ۷. اپلیکیشن اصلی — JarvisSenpaiApp
#==============================================================================
class JarvisSenpaiApp:
    def __init__(self):
        self._init_session_state()

    def _init_session_state(self):
        defaults = {
            "token": None,
            "page": "login",
            "current_conv_id": None,
            "messages": [],
            "conversations_list": [],
            "initialized": False,
            "media_mode": False,
            "last_media": None,
            "last_prompt_ts": 0.0,
            "messages_offset": 0,
        }
        for k, v in defaults.items():
            if k not in st.session_state:
                st.session_state[k] = v

    # نمایش مدل‌ها
    def render_models_info(self):
        st.markdown("### 🔎 مدل‌های در دسترس")
        for cat, group in MODELS.items():
            st.write(f"**{cat}**")
            cols = st.columns(min(3, max(1, len(group))))
            i = 0
            for name, info in group.items():
                with cols[i % len(cols)]:
                    st.markdown(f"""
                        <div class="model-card">
                            <strong>{name}</strong>
                            <div style="margin-top:8px; margin-bottom:6px;">
                                <span class="badge rpm">RPM: {info['RPM']}</span>
                                <span class="badge rpd">RPD: {info['RPD']}</span>
                                <span class="badge cap">{info['capabilities']}</span>
                            </div>
                            <small style="opacity:0.85">id: <code>{info['id']}</code></small>
                        </div>
                    """, unsafe_allow_html=True)
                i += 1
        st.markdown("---")
        rows = {"نام مدل": [], "دسته": [], "capabilities": [], "RPM": [], "RPD": [], "model_id": []}
        for cat, g in MODELS.items():
            for n, inf in g.items():
                rows["نام مدل"].append(n)
                rows["دسته"].append(cat)
                rows["capabilities"].append(inf["capabilities"])
                rows["RPM"].append(inf["RPM"])
                rows["RPD"].append(inf["RPD"])
                rows["model_id"].append(inf["id"])
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

    # صفحهٔ ورود/ثبت‌نام
    def render_login_signup(self):
        st.title(f"{APP_NICKNAME} — ورود / ثبت‌نام")
        col1, col2 = st.columns([3, 1])
        with col1:
            tab1, tab2 = st.tabs(["ورود", "ثبت‌نام"])
            with tab1:
                with st.form("login_form"):
                    email = st.text_input("ایمیل", key="login_email")
                    pwd = st.text_input("رمز عبور", type="password", key="login_pwd")
                    if st.form_submit_button("ورود", use_container_width=True):
                        if not email or not pwd:
                            st.error("ایمیل و رمز را وارد کنید.")
                        else:
                            user = db_get_user_by_email_sync(email)
                            if user and verify_password(pwd, user.get("password", "")):
                                info = {"id": str(user["_id"]), "name": user.get("name", "کاربر"), "email": user["email"]}
                                st.session_state.token = create_jwt_token(info)
                                st.session_state.page = "dashboard"
                                st.session_state.initialized = False
                                st.experimental_rerun()
                            else:
                                st.error("ایمیل یا رمز اشتباه است.")
            with tab2:
                with st.form("signup_form"):
                    name = st.text_input("نام کامل", key="su_name")
                    email2 = st.text_input("ایمیل", key="su_email")
                    p1 = st.text_input("رمز عبور", type="password", key="su_p1")
                    p2 = st.text_input("تکرار رمز عبور", type="password", key="su_p2")
                    if st.form_submit_button("ثبت‌نام", use_container_width=True):
                        if not (name and email2 and p1 and p2):
                            st.error("همهٔ فیلدها را پر کنید.")
                        elif p1 != p2:
                            st.error("رمزها مطابقت ندارند.")
                        elif len(p1) < 6:
                            st.error("رمز باید حداقل ۶ کاراکتر باشد.")
                        elif db_get_user_by_email_sync(email2):
                            st.error("این ایمیل قبلاً ثبت شده است.")
                        else:
                            uid = db_create_user_sync(name, email2, p1)
                            st.success("ثبت‌نام موفق — اکنون وارد شوید.")
                            logger.info("New user: %s", uid)
        with col2:
            st.markdown(f"### 👋 خوش آمدید به {APP_NICKNAME}")
            st.markdown("- پیکربندی از متغیرهای محیطی خوانده می‌شود.")
            st.markdown("- مدل‌ها: چت متنی، تولید تصویر و تولید ویدیو.")

    # سایدبار
    def render_sidebar(self, user_payload: dict):
        with st.sidebar:
            st.header(f"👤 {user_payload.get('name', 'کاربر')}")
            if st.button("➕ مکالمه جدید", use_container_width=True):
                st.session_state.current_conv_id = None
                st.session_state.messages = []
                st.session_state.messages_offset = 0
                st.experimental_rerun()
            st.markdown("---")
            st.subheader("تاریخچه مکالمات")
            try:
                convs = db_get_conversations_sync(user_payload["sub"])
                st.session_state.conversations_list = convs
                for c in convs:
                    cid = str(c["_id"])
                    label = c.get("title", "بدون عنوان")[:30]
                    is_active = cid == st.session_state.current_conv_id
                    if st.button(f"{'🔹 ' if is_active else ''}{label}", key=f"conv_{cid}", use_container_width=True):
                        if not is_active:
                            st.session_state.current_conv_id = cid
                            st.session_state.messages_offset = 0
                            st.session_state.messages = db_get_messages_sync(cid, PAGE_SIZE_MESSAGES, 0)
                            st.experimental_rerun()
            except Exception as e:
                logger.exception("Error loading convs: %s", e)
                st.error("خطا در بارگذاری مکالمات.")
            st.markdown("---")
            if st.button("👤 پروفایل", use_container_width=True):
                st.session_state.page = "profile"
                st.experimental_rerun()
            if st.button("خروج از حساب", use_container_width=True):
                st.session_state.clear()
                st.experimental_rerun()

    # داشبورد
    def render_dashboard(self, user_payload: dict):
        self.render_sidebar(user_payload)
        st.header(f"💬 گفتگوی هوشمند — {APP_NICKNAME}")
        left, right = st.columns([3, 1])
        with right:
            st.session_state.media_mode = st.checkbox("فعال کردن حالت تصویر/ویدیو", value=st.session_state.media_mode)
            if st.session_state.media_mode:
                media_type = st.radio("نوع محتوا:", ["تولید تصویر", "تولید ویدیو"], horizontal=True)
                model_list = MODELS["تولید تصویر"] if media_type == "تولید تصویر" else MODELS["تولید ویدیو"]
                selected_model_name = st.selectbox("مدل:", list(model_list.keys()))
                technical_model_id = model_list[selected_model_name]["id"]
            else:
                model_cat = st.selectbox("دستهٔ مدل:", list(MODELS.keys()))
                selected_model_name = st.selectbox("انتخاب مدل:", list(MODELS[model_cat].keys()))
                technical_model_id = MODELS[model_cat][selected_model_name]["id"]

        st.markdown("---")

        # load messages if needed
        if st.session_state.current_conv_id and not st.session_state.messages:
            st.session_state.messages = db_get_messages_sync(st.session_state.current_conv_id, PAGE_SIZE_MESSAGES, 0)

        chat_box = st.container()

        if st.session_state.current_conv_id:
            if st.button("⤴️ بارگذاری پیام‌های قدیمی‌تر", use_container_width=True):
                st.session_state.messages_offset = st.session_state.get("messages_offset", 0) + PAGE_SIZE_MESSAGES
                older = db_get_messages_sync(st.session_state.current_conv_id, PAGE_SIZE_MESSAGES, st.session_state.messages_offset)
                st.session_state.messages = older + st.session_state.messages
                st.experimental_rerun()

        for m in st.session_state.messages:
            role = m.get("role", "user")
            with chat_box.chat_message(role):
                if m.get("type") == "image":
                    st.image(m["content"], caption=m.get("caption", "تصویر تولید شده"))
                elif m.get("type") == "video":
                    st.video(m["content"])
                else:
                    st.markdown(m["content"])

        # ورودی کاربر
        with st.form("chat_input_form", clear_on_submit=True):
            user_prompt = st.text_input("پیام خود را بنویسید...", key="user_prompt")
            submit = st.form_submit_button("ارسال")
            if submit and user_prompt:
                now = time.time()
                last = st.session_state.get("last_prompt_ts", 0.0)
                if now - last < MIN_SECONDS_BETWEEN_PROMPTS:
                    st.warning("لطفاً چند لحظه صبر کنید.")
                else:
                    st.session_state.last_prompt_ts = now
                    self.process_chat_input(user_prompt, technical_model_id, user_payload)

        st.markdown("---")
        with st.expander("ℹ️ اطلاعات مدل‌ها و قابلیت‌ها — جمع‌وجور"):
            self.render_models_info()

    # پردازش پیام کاربر
    def process_chat_input(self, prompt: str, model_id: str, user_payload: dict):
        try:
            user_id = user_payload["sub"]
            conv_id = st.session_state.current_conv_id
            if not conv_id:
                conv_id = db_create_conversation_sync(user_id, prompt[:40] or "مکالمه جدید")
                st.session_state.current_conv_id = conv_id
                st.session_state.messages_offset = 0

            user_msg = {"_id": str(uuid.uuid4()), "role": "user", "type": "text", "content": prompt, "ts": datetime.now(timezone.utc)}
            st.session_state.messages.append(user_msg)
            db_append_message_sync(conv_id, user_msg)

            with st.spinner("در حال پردازش..."):
                if st.session_state.media_mode:
                    media_url = generate_media_sync(prompt, model_id)
                    if "image" in model_id.lower() or "imagen" in model_id.lower():
                        ai_msg = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "image", "content": media_url, "ts": datetime.now(timezone.utc)}
                        st.image(media_url)
                    else:
                        ai_msg = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "video", "content": media_url, "ts": datetime.now(timezone.utc)}
                        st.video(media_url)
                    st.session_state.last_media = media_url
                else:
                    text_history = [m for m in st.session_state.messages if m.get("type", "text") == "text"]
                    api_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in text_history]
                    full_text = call_gemini_sync(api_history, model_id)
                    st.markdown(full_text)
                    ai_msg = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "text", "content": full_text, "ts": datetime.now(timezone.utc)}

                st.session_state.messages.append(ai_msg)
                db_append_message_sync(conv_id, ai_msg)

            st.experimental_rerun()
        except Exception as e:
            logger.exception("Error in process_chat_input: %s", e)
            st.error(f"خطا در پردازش پیام: {e}")

    # صفحهٔ پروفایل
    def render_profile(self, user_payload: dict):
        self.render_sidebar(user_payload)
        st.title("👤 پروفایل شما")
        with st.form("profile_form"):
            name = st.text_input("نام کامل", value=user_payload.get("name", ""))
            st.write(f"**ایمیل:** `{user_payload.get('email', '')}`")
            if st.form_submit_button("ذخیره"):
                try:
                    db_update_user_name_sync(user_payload["sub"], name)
                    st.success("پروفایل با موفقیت به‌روزرسانی شد.")
                    new_payload = {"id": user_payload["sub"], "name": name, "email": user_payload.get("email", "")}
                    st.session_state.token = create_jwt_token(new_payload)
                    time.sleep(0.6)
                    st.experimental_rerun()
                except Exception as e:
                    logger.exception("Profile update error: %s", e)
                    st.error("خطا در بروزرسانی پروفایل.")

    # Router
    def run(self):
        self._init_session_state()
        token = st.session_state.get("token")
        user_payload = decode_jwt_token(token) if token else None

        if not user_payload:
            self.render_login_signup()
            return

        if not st.session_state.get("initialized", False):
            try:
                st.session_state.conversations_list = db_get_conversations_sync(user_payload["sub"])
            except Exception as e:
                logger.exception("Error loading convs: %s", e)
            st.session_state.initialized = True

        page = st.session_state.get("page", "dashboard")
        if page == "dashboard":
            self.render_dashboard(user_payload)
        elif page == "profile":
            self.render_profile(user_payload)
        else:
            st.error("صفحهٔ نامعتبر")

#==============================================================================
# ۸. اجرای اپ
#==============================================================================
if __name__ == "__main__":
    app = JarvisSenpaiApp()
    try:
        app.run()
    except Exception as e:
        logger.exception("Unhandled exception in main: %s", e)
        st.error(f"خطای داخلی: {e}")
