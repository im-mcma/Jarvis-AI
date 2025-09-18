# app.py — Jarvis Argus (نسخهٔ رفرم‌شده و عملیاتی)
# توضیحات:
# - ساختار OOP (کلاس JarvisArgus)
# - خواندن کانفیگ از متغیر محیطی (env) به جای secrets.toml
# - MongoDB (motor, async) با cache_resource
# - احراز هویت با bcrypt و JWT
# - پشتیبانی از مدل‌های Gemini که شما فرستادید (MODELS کامل)
# - استریم متن از Gemini و placeholders برای رسانه
# - pagination پیام‌ها، rate-limit ساده، logging و validation
#
# اجرا:
#   export MONGO_URI="..." 
#   export GEMINI_API_KEY="..." 
#   export JWT_SECRET_KEY="..."
#   streamlit run app.py
#
# نیازمندی‌ها (حداقل):
# pip install streamlit motor pymongo bcrypt pyjwt google-generative-ai python-dotenv

import os
import uuid
import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, AsyncGenerator, Optional

import streamlit as st
import pandas as pd
import bcrypt
import jwt
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import google.generativeai as genai
from dotenv import load_dotenv

# ---------- تنظیم لاگ ----------
logging.basicConfig(level=logging.INFO)
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
        # استفاده از st.error ممکن است قبل از init صفحه خطا دهد ولی اینجا کافی است خطای روشنی بدهیم.
        err = f"متغیر محیطی `{key}` تنظیم نشده است."
        logger.error(err)
        raise RuntimeError(err)
    return val

MONGO_URI = get_env_var("MONGO_URI")
GEMINI_API_KEY = get_env_var("GEMINI_API_KEY")
JWT_SECRET_KEY = get_env_var("JWT_SECRET_KEY")

# ---------- CSS و صفحه ----------
st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="collapsed", page_icon="🛡️")
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;600;700&display=swap');
html, body, [class*="st-"] {{ font-family: 'Vazirmatn', sans-serif; direction: rtl; }}
.header-row {{ display:flex; align-items:center; gap:12px; }}
.app-badge {{ background:#0ea5a0; color:white; padding:6px 10px; border-radius:999px; font-weight:600; }}
.stChatMessage {{ border-radius: 12px; border: 1px solid #374151; background-color: #0b1220; margin-bottom: 1rem; padding:8px; }}
.stChatMessage:has(div[data-testid="stChatMessageContent.user"]) {{ background-color:#063983; color:white; }}
.sidebar .stButton>button {{ width:100%; }}
.model-card {{ background:#0f1724; border:1px solid #1f2937; padding:10px; border-radius:10px; margin-bottom:8px; }}
.badge {{ display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; margin-left:6px; }}
.rpm {{ background:#063f7a; color:#fff; }}
.rpd {{ background:#6b21a8; color:#fff; }}
.cap {{ background:#064e3b; color:#fff; }}
</style>
""", unsafe_allow_html=True)

# ---------- پیکربندی Gemini ----------
try:
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.warning("خطا هنگام configure کردن genai: %s", e)

# ---------- کش کردن کلاینت MongoDB ----------
@st.cache_resource
def get_db_client() -> AsyncIOMotorClient:
    return AsyncIOMotorClient(MONGO_URI)

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

# ---------- توابع امنیتی / JWT ----------
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
        "iat": datetime.now(timezone.utc).timestamp(),
        "exp": (datetime.now(timezone.utc) + timedelta(days=1)).timestamp(),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

def decode_jwt_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        st.warning("نشست منقضی شده است. لطفاً دوباره وارد شوید.")
        return None
    except Exception as e:
        logger.warning("Invalid JWT: %s", e)
        return None

# ---------- کلاس اصلی اپ ----------
class JarvisArgus:
    def __init__(self):
        # DB init
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

    # ---------------- DB helpers ----------------
    async def db_get_user_by_email(self, email: str) -> Optional[dict]:
        if not email:
            return None
        return await self.users.find_one({"email": email.lower().strip()})

    async def db_create_user(self, name: str, email: str, password: str) -> str:
        doc = {"name": name, "email": email.lower().strip(), "password": hash_password(password), "created_at": datetime.now(timezone.utc)}
        res = await self.users.insert_one(doc)
        logger.info("New user created: %s", res.inserted_id)
        return str(res.inserted_id)

    async def db_create_conversation(self, user_id: str, title: str) -> str:
        conv = {"user_id": ObjectId(user_id), "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
        res = await self.conversations.insert_one(conv)
        return str(res.inserted_id)

    async def db_get_conversations(self, user_id: str) -> List[dict]:
        cursor = self.conversations.find({"user_id": ObjectId(user_id)}).sort("created_at", -1)
        return await cursor.to_list(length=200)

    async def db_get_messages(self, conv_id: str, limit: int = PAGE_SIZE_MESSAGES, offset: int = 0) -> List[dict]:
        doc = await self.conversations.find_one({"_id": ObjectId(conv_id)}, {"messages": 1})
        if not doc:
            return []
        msgs = doc.get("messages", []) or []
        # take slice for pagination: return chronological (old->new) in the slice
        start = max(0, len(msgs) - (offset + limit))
        end = len(msgs) - offset
        sliced = msgs[start:end]
        return sliced

    async def db_append_message(self, conv_id: str, msg: dict):
        await self.conversations.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": msg}})

    # ---------------- Gemini streaming ----------------
    async def stream_gemini_response(self, history: List[Dict[str, Any]], model_id: str) -> AsyncGenerator[str, None]:
        if not GEMINI_API_KEY:
            yield "**خطا: کلید API گوگل تنظیم نشده است.**"
            return
        try:
            api_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in history if m.get("type", "text") == "text"]
            model = genai.GenerativeModel(model_id)
            response_stream = await model.generate_content_async(api_history, stream=True)
            async for chunk in response_stream:
                txt = getattr(chunk, "text", None)
                if txt:
                    yield txt
        except Exception as e:
            logger.exception("Gemini streaming error")
            yield f"**خطای API:** {e}"

    # ---------------- media generation (placeholder) ----------------
    async def generate_media_impl(self, prompt: str, model_id: str) -> str:
        # شبیه‌سازی تولید تصویر/ویدیو. اگر API واقعی دارید، این قسمت را جایگزین کنید.
        if "image" in model_id.lower() or "imagen" in model_id.lower():
            await asyncio.sleep(2.2)
            return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"
        if "veo" in model_id.lower() or "video" in model_id.lower():
            await asyncio.sleep(4)
            return "https://www.w3schools.com/html/mov_bbb.mp4"
        await asyncio.sleep(2)
        return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"

    # ---------------- UI renderers ----------------
    async def render_login_signup(self):
        st.title(f"{APP_NICKNAME} — ورود / ثبت‌نام")
        col_left, col_right = st.columns([3, 1])
        with col_left:
            tab1, tab2 = st.tabs(["ورود", "ثبت‌نام"])
            with tab1:
                with st.form("login_form"):
                    email = st.text_input("ایمیل", key="login_email")
                    pwd = st.text_input("رمز عبور", type="password", key="login_pwd")
                    submit = st.form_submit_button("ورود", use_container_width=True)
                    if submit:
                        if not email or not pwd:
                            st.error("ایمیل و رمز عبور را وارد کنید.")
                        else:
                            user = await self.db_get_user_by_email(email)
                            if user and verify_password(pwd, user["password"]):
                                user_info = {"id": str(user["_id"]), "name": user.get("name", "کاربر"), "email": user["email"]}
                                st.session_state.token = create_jwt_token(user_info)
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
                    submit2 = st.form_submit_button("ثبت‌نام", use_container_width=True)
                    if submit2:
                        if not (name and email2 and p1 and p2):
                            st.error("همهٔ فیلدها را پر کنید.")
                        elif p1 != p2:
                            st.error("رمزها مطابقت ندارند.")
                        elif len(p1) < 6:
                            st.error("رمز باید حداقل ۶ کاراکتر باشد.")
                        elif await self.db_get_user_by_email(email2):
                            st.error("این ایمیل قبلاً ثبت شده است.")
                        else:
                            uid = await self.db_create_user(name, email2, p1)
                            st.success("ثبت‌نام موفق — اکنون وارد شوید.")
                            logger.info("New user registered: %s", uid)
        with col_right:
            st.markdown(f"### 👋 خوش آمدید به {APP_NICKNAME}")
            st.markdown("- پیکربندی از متغیرهای محیطی خوانده می‌شود.")
            st.markdown("- پاسخ متن استریم‌شونده و رسانه‌ها placeholder هستند (در صورت نیاز جایگزین نمایید).")

    async def render_sidebar(self, user_payload: dict):
        with st.sidebar:
            st.header(f"👤 {user_payload.get('name', 'کاربر')}")
            if st.button("➕ مکالمه جدید", use_container_width=True):
                st.session_state.current_conv_id = None
                st.session_state.messages = []
                st.session_state.messages_offset = 0
                st.experimental_rerun()
            st.markdown("---")
            st.subheader("تاریخچه مکالمات")
            @st.cache_data(ttl=30)
            async def _get_convs(uid: str):
                return await self.db_get_conversations(uid)
            try:
                convs = await _get_convs(user_payload["sub"])
                st.session_state.conversations_list = convs
                for c in convs:
                    cid = str(c["_id"])
                    label = c.get("title", "") or "بدون عنوان"
                    is_active = cid == st.session_state.current_conv_id
                    if st.button(f"{'🔹 ' if is_active else ''}{label[:35]}", key=f"conv_{cid}", use_container_width=True):
                        if not is_active:
                            st.session_state.current_conv_id = cid
                            st.session_state.messages_offset = 0
                            st.session_state.messages = await self.db_get_messages(cid, limit=PAGE_SIZE_MESSAGES, offset=0)
                            st.experimental_rerun()
            except Exception as e:
                logger.exception("Error loading conversations")
                st.error("خطا در بارگذاری تاریخچهٔ مکالمات.")
            st.markdown("---")
            if st.button("👤 پروفایل", use_container_width=True):
                st.session_state.page = "profile"
                st.experimental_rerun()
            if st.button("خروج از حساب", use_container_width=True):
                st.session_state.clear()
                st.experimental_rerun()

    def render_models_info(self):
        st.markdown("### 🔎 مدل‌های در دسترس")
        for cat, group in MODELS.items():
            st.write(f"**{cat}**")
            cols = st.columns(min(3, max(1, len(group))))
            i = 0
            for name, info in group.items():
                with cols[i % len(cols)]:
                    st.markdown(f'''
                        <div class="model-card">
                            <strong>{name}</strong>
                            <div style="margin-top:8px; margin-bottom:6px;">
                                <span class="badge rpm">RPM: {info['RPM']}</span>
                                <span class="badge rpd">RPD: {info['RPD']}</span>
                                <span class="badge cap">{info['capabilities']}</span>
                            </div>
                            <small style="opacity:0.85">id: <code>{info['id']}</code></small>
                        </div>
                        ''', unsafe_allow_html=True)
                i += 1
        st.markdown("---")
        data = {'نام مدل': [], 'دسته': [], 'capabilities': [], 'RPM': [], 'RPD': [], 'model_id': []}
        for cat, g in MODELS.items():
            for n, inf in g.items():
                data['نام مدل'].append(n)
                data['دسته'].append(cat)
                data['capabilities'].append(inf['capabilities'])
                data['RPM'].append(inf['RPM'])
                data['RPD'].append(inf['RPD'])
                data['model_id'].append(inf['id'])
        st.dataframe(pd.DataFrame(data), use_container_width=True)

    async def render_dashboard(self, user_payload: dict):
        await self.render_sidebar(user_payload)
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

        # بارگذاری پیام‌ها در اولین render برای convo انتخاب‌شده
        if st.session_state.current_conv_id and not st.session_state.messages:
            st.session_state.messages = await self.db_get_messages(st.session_state.current_conv_id, limit=PAGE_SIZE_MESSAGES, offset=0)

        chat_box = st.container()

        if st.session_state.current_conv_id:
            if st.button("⤴️ بارگذاری پیام‌های قدیمی‌تر", use_container_width=True):
                st.session_state.messages_offset = st.session_state.get("messages_offset", 0) + PAGE_SIZE_MESSAGES
                older = await self.db_get_messages(st.session_state.current_conv_id, limit=PAGE_SIZE_MESSAGES, offset=st.session_state.messages_offset)
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

        # ورودی کاربر با فرم (پاک شدن پس از ارسال)
        with st.form("chat_input_form", clear_on_submit=True):
            user_prompt = st.text_input("پیام خود را بنویسید...", key="user_prompt")
            submit = st.form_submit_button("ارسال")
            if submit and user_prompt:
                now_ts = time.time()
                last_ts = st.session_state.get("last_prompt_ts", 0.0)
                if now_ts - last_ts < MIN_SECONDS_BETWEEN_PROMPTS:
                    st.warning("لطفاً چند لحظه صبر کنید و سپس دوباره ارسال کنید.")
                else:
                    st.session_state.last_prompt_ts = now_ts
                    await self.process_chat_input(user_prompt, technical_model_id, user_payload)

        st.markdown("---")
        with st.expander("ℹ️ اطلاعات مدل‌ها و قابلیت‌ها — جمع‌وجور"):
            self.render_models_info()

    async def process_chat_input(self, prompt: str, model_id: str, user_payload: dict):
        user_id = user_payload["sub"]
        conv_id = st.session_state.current_conv_id
        if not conv_id:
            conv_id = await self.db_create_conversation(user_id, prompt[:40] or "مکالمه جدید")
            st.session_state.current_conv_id = conv_id
            st.session_state.messages_offset = 0

        user_msg = {"_id": str(uuid.uuid4()), "role": "user", "type": "text", "content": prompt, "ts": datetime.now(timezone.utc)}
        st.session_state.messages.append(user_msg)
        await self.db_append_message(conv_id, user_msg)

        # نمایش پاسخ
        with st.spinner("در حال پردازش..."):
            if st.session_state.media_mode:
                media_url = await self.generate_media_impl(prompt, model_id)
                if "image" in model_id.lower() or "imagen" in model_id.lower():
                    ai_msg = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "image", "content": media_url, "ts": datetime.now(timezone.utc)}
                    st.image(media_url)
                else:
                    ai_msg = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "video", "content": media_url, "ts": datetime.now(timezone.utc)}
                    st.video(media_url)
                st.session_state.last_media = media_url
            else:
                # استریم پاسخ متنی و نمایش تدریجی
                text_history = [m for m in st.session_state.messages if m.get("type", "text") == "text"]
                placeholder = st.empty()
                accumulated = ""
                try:
                    async for chunk in self.stream_gemini_response(text_history, model_id):
                        accumulated += chunk
                        # نمایش امن (متن ساده). اگر نیاز دارید markdown/HTML بگذارید تغییر دهید.
                        placeholder.markdown(accumulated)
                        # کمی sleep کوتاه تا UI فرصت رندر بگیرد (اختیاری)
                        await asyncio.sleep(0.01)
                    full_text = accumulated
                except Exception as e:
                    logger.exception("Error streaming response")
                    full_text = f"**خطا در دریافت پاسخ:** {e}"
                    placeholder.markdown(full_text)
                ai_msg = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "text", "content": full_text, "ts": datetime.now(timezone.utc)}

            st.session_state.messages.append(ai_msg)
            await self.db_append_message(conv_id, ai_msg)

        # بعد از پردازش ریلود UI تا پیام‌ها نمایش داده شوند
        st.experimental_rerun()

    async def render_profile(self, user_payload: dict):
        await self.render_sidebar(user_payload)
        st.title("👤 پروفایل شما")
        with st.form("profile_form"):
            name = st.text_input("نام کامل", value=user_payload.get("name", ""))
            st.write(f"**ایمیل:** `{user_payload.get('email', '')}`")
            submit = st.form_submit_button("ذخیره")
            if submit:
                await self.users.update_one({"_id": ObjectId(user_payload["sub"])}, {"$set": {"name": name}})
                st.success("پروفایل با موفقیت به‌روز شد.")
                new_payload = {"id": user_payload["sub"], "name": name, "email": user_payload.get("email", "")}
                st.session_state.token = create_jwt_token(new_payload)
                time.sleep(0.7)
                st.experimental_rerun()

    # ---------------- Router اصلی ----------------
    async def run(self):
        self._init_session_state()
        token = st.session_state.get("token")
        user_payload = decode_jwt_token(token) if token else None

        if not user_payload:
            await self.render_login_signup()
            return

        # بارگذاری اولیه مکالمات
        if not st.session_state.get("initialized", False):
            try:
                st.session_state.conversations_list = await self.db_get_conversations(user_payload["sub"])
            except Exception as e:
                logger.exception("Error loading conversations")
            st.session_state.initialized = True

        page = st.session_state.get("page", "dashboard")
        if page == "dashboard":
            await self.render_dashboard(user_payload)
        elif page == "profile":
            await self.render_profile(user_payload)
        else:
            st.error("صفحهٔ نامعتبر")

# ---------- اجرای برنامه ----------
if __name__ == "__main__":
    app = JarvisArgus()
    try:
        asyncio.run(app.run())
    except Exception as e:
        logger.exception("Unhandled exception in app")
        st.error(f"خطای داخلی: {e}")
