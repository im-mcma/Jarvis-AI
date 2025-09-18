#==============================================================================
# ۱. فاز ۱.۳: پیکربندی و استایل (Config & Styling)
#==============================================================================
import os
import uuid
import time
import logging
import nest_asyncio
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import streamlit as st
import pandas as pd
import bcrypt
import jwt
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import google.generativeai as genai
from dotenv import load_dotenv

# apply nest_asyncio early to avoid "Event loop is closed" in Streamlit environments
nest_asyncio.apply()
LOOP = asyncio.get_event_loop()

# ---------- basic logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-senpai")

# load .env for local dev (safe to call even if no .env)
load_dotenv()

#==============================================================================
# ۲. فاز ۱.۲: تنظیمات محیطی و ثابت‌ها (Env & Constants)
#==============================================================================
APP_NICKNAME = "Jarvis: Senpai"  # لقبی فان/انیمه‌طور
APP_TITLE = f"{APP_NICKNAME} — هوش مصنوعی"
PAGE_SIZE_MESSAGES = 50
MIN_SECONDS_BETWEEN_PROMPTS = 0.6

def get_env_var(key: str, required: bool = True) -> Optional[str]:
    v = os.environ.get(key)
    if required and not v:
        raise RuntimeError(f"Environment variable `{key}` is required but not set.")
    return v

MONGO_URI = get_env_var("MONGO_URI")
GEMINI_API_KEY = get_env_var("GEMINI_API_KEY")
JWT_SECRET_KEY = get_env_var("JWT_SECRET_KEY")

# configure genai (best-effort; errors handled later)
try:
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.warning("genai.configure failed: %s", e)

#==============================================================================
# ۳. فاز ۲.۱: MODELS (لیست کامل مدل‌ها)
#==============================================================================
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

#==============================================================================
# ۴. فاز ۲.۲: استایل اپ (Streamlit page config + CSS)
#==============================================================================
st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="collapsed", page_icon="😺")
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;600;700&display=swap');
html, body, [class*="st-"] {{ font-family: 'Vazirmatn', sans-serif; direction: rtl; }}
.header-row {{ display:flex; align-items:center; gap:12px; }}
.app-badge {{ background:#ff6b6b; color:white; padding:6px 10px; border-radius:999px; font-weight:700; }}
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

#==============================================================================
# ۵. فاز ۳.۱: ابزار کمکی (helpers)
#==============================================================================
def run_async(coro):
    """Helper: run coroutine in global loop (nest_asyncio applied)."""
    try:
        return LOOP.run_until_complete(coro)
    except RuntimeError as e:
        # fallback: create temporary loop (shouldn't normally happen because of nest_asyncio)
        new_loop = asyncio.new_event_loop()
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()

def now_ts():
    return time.time()

def safe_str(s) -> str:
    return "" if s is None else str(s)

#==============================================================================
# ۶. فاز ۳.۲: auth helpers (bcrypt + jwt)
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
        st.warning("نشست شما منقضی شده است — لطفاً دوباره وارد شوید.")
        return None
    except Exception as e:
        logger.warning("decode_jwt_token error: %s", e)
        return None

#==============================================================================
# ۷. فاز ۴.۱: DB client (motor) و wrapper های async
#==============================================================================
@st.cache_resource
def get_db_client() -> AsyncIOMotorClient:
    return AsyncIOMotorClient(MONGO_URI)

DB_CLIENT = get_db_client()
DB = DB_CLIENT["jarvis_senpai_db"]
USERS_COLL = DB["users"]
CONV_COLL = DB["conversations"]

async def db_get_user_by_email(email: str) -> Optional[dict]:
    if not email:
        return None
    return await USERS_COLL.find_one({"email": email.lower().strip()})

async def db_create_user(name: str, email: str, password: str) -> str:
    doc = {"name": name, "email": email.lower().strip(), "password": hash_password(password), "created_at": datetime.now(timezone.utc)}
    res = await USERS_COLL.insert_one(doc)
    logger.info("Created user %s", res.inserted_id)
    return str(res.inserted_id)

async def db_create_conversation(user_id: str, title: str) -> str:
    conv = {"user_id": ObjectId(user_id), "title": title or "مکالمه جدید", "created_at": datetime.now(timezone.utc), "messages": []}
    res = await CONV_COLL.insert_one(conv)
    return str(res.inserted_id)

async def db_get_conversations(user_id: str) -> List[dict]:
    cursor = CONV_COLL.find({"user_id": ObjectId(user_id)}).sort("created_at", -1)
    return await cursor.to_list(length=200)

async def db_get_messages(conv_id: str, limit: int = PAGE_SIZE_MESSAGES, offset: int = 0) -> List[dict]:
    doc = await CONV_COLL.find_one({"_id": ObjectId(conv_id)}, {"messages": 1})
    if not doc:
        return []
    msgs = doc.get("messages", []) or []
    start = max(0, len(msgs) - (offset + limit))
    end = len(msgs) - offset
    sliced = msgs[start:end]
    return sliced

async def db_append_message(conv_id: str, msg: dict):
    await CONV_COLL.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": msg}})

#==============================================================================
# ۸. فاز ۴.۲: Gemini interaction (sync-friendly)
#==============================================================================
def call_gemini_sync(api_history: List[Dict[str, Any]], model_id: str) -> str:
    """
    Call genai synchronously (best-effort):
    - prefer sync generate_content if available on SDK
    - otherwise attempt to use async via run_async and collect text
    Returns the aggregated text response.
    """
    try:
        # try sync first (some SDK versions provide .generate_content)
        model = genai.GenerativeModel(model_id)
        if hasattr(model, "generate_content"):
            # build request shape expected by SDK (may vary by SDK version)
            try:
                resp = model.generate_content({"messages": api_history})  # some SDK variants
            except Exception:
                # alternative call signature
                resp = model.generate_content(api_history)
            # parse response: try common fields
            text = ""
            if hasattr(resp, "text") and resp.text:
                text = resp.text
            else:
                # attempt to read structured fields
                try:
                    # fallback: resp.candidates[0].content[0].text or similar shapes
                    if getattr(resp, "candidates", None):
                        cand = resp.candidates[0]
                        if getattr(cand, "content", None):
                            for p in cand.content:
                                if getattr(p, "text", None):
                                    text += p.text
                except Exception:
                    text = str(resp)
            return text or str(resp)
        else:
            # fallback: attempt async generate_content_async and run it (no streaming)
            async def _call():
                model_async = genai.GenerativeModel(model_id)
                stream = await model_async.generate_content_async(api_history, stream=False)
                # stream may be an object with text or candidates
                text_acc = ""
                if getattr(stream, "text", None):
                    text_acc += stream.text
                else:
                    # check candidates
                    if getattr(stream, "candidates", None):
                        for c in stream.candidates:
                            if getattr(c, "content", None):
                                for p in c.content:
                                    if getattr(p, "text", None):
                                        text_acc += p.text
                return text_acc or str(stream)
            return run_async(_call())
    except Exception as e:
        logger.exception("call_gemini_sync error")
        return f"**خطا در فراخوانی Gemini:** {e}"

#==============================================================================
# ۹. فاز ۵.۱: کلاس اصلی اپ (JarvisSenpai)
#==============================================================================
class JarvisSenpai:
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

    # ---------- UI: models info ----------
    def render_models_info_compact(self):
        st.markdown("### 🔎 مدل‌های در دسترس (جمع‌وجور)")
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
            st.write("---")
        st.markdown("### جدول کامل مدل‌ها")
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

    # ---------- render login/signup ----------
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
                            user = run_async(db_get_user_by_email(email))
                            if user and verify_password(pwd, user.get("password","")):
                                payload = {"id": str(user["_id"]), "name": user.get("name","کاربر"), "email": user.get("email","")}
                                st.session_state.token = create_jwt_token(payload)
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
                        elif run_async(db_get_user_by_email(email2)):
                            st.error("این ایمیل قبلاً ثبت شده است.")
                        else:
                            uid = run_async(db_create_user(name, email2, p1))
                            st.success("ثبت‌نام موفق — اکنون وارد شوید.")
                            logger.info("User created %s", uid)
        with col2:
            st.markdown(f"### 👋 خوش آمدید به {APP_NICKNAME}")
            st.markdown("- پیکربندی از متغیرهای محیطی خوانده می‌شود.")
            st.markdown("- مدل‌ها: چت متنی، تولید تصویر و تولید ویدیو (RPM/RPD اطلاع‌رسانی).")

    # ---------- sidebar ----------
    def render_sidebar(self, user_payload: dict):
        with st.sidebar:
            st.header(f"👤 {user_payload.get('name','کاربر')}")
            if st.button("➕ مکالمه جدید", use_container_width=True):
                st.session_state.current_conv_id = None
                st.session_state.messages = []
                st.session_state.messages_offset = 0
                st.experimental_rerun()
            st.markdown("---")
            st.subheader("تاریخچه مکالمات")
            try:
                convs = run_async(db_get_conversations(user_payload["sub"]))
                st.session_state.conversations_list = convs
                for c in convs:
                    cid = str(c["_id"])
                    label = c.get("title","بدون عنوان")[:35]
                    is_active = cid == st.session_state.current_conv_id
                    if st.button(f"{'🔹 ' if is_active else ''}{label}", key=f"conv_{cid}", use_container_width=True):
                        if not is_active:
                            st.session_state.current_conv_id = cid
                            st.session_state.messages_offset = 0
                            st.session_state.messages = run_async(db_get_messages(cid, limit=PAGE_SIZE_MESSAGES, offset=0))
                            st.experimental_rerun()
            except Exception as e:
                logger.exception("Error loading conversations: %s", e)
                st.error("خطا در بارگذاری مکالمات.")
            st.markdown("---")
            if st.button("👤 پروفایل", use_container_width=True):
                st.session_state.page = "profile"
                st.experimental_rerun()
            if st.button("خروج از حساب", use_container_width=True):
                st.session_state.clear()
                st.experimental_rerun()

    # ---------- dashboard ----------
    def render_dashboard(self, user_payload: dict):
        self.render_sidebar(user_payload)
        st.header(f"💬 گفتگوی هوشمند — {APP_NICKNAME}")
        left, right = st.columns([3,1])
        with right:
            st.session_state.media_mode = st.checkbox("فعال کردن حالت تصویر/ویدیو", value=st.session_state.media_mode)
            if st.session_state.media_mode:
                media_type = st.radio("نوع محتوا:", ["تولید تصویر","تولید ویدیو"], horizontal=True)
                model_list = MODELS["تولید تصویر"] if media_type=="تولید تصویر" else MODELS["تولید ویدیو"]
                selected_model_name = st.selectbox("مدل:", list(model_list.keys()))
                technical_model_id = model_list[selected_model_name]["id"]
            else:
                model_cat = st.selectbox("دستهٔ مدل:", list(MODELS.keys()))
                selected_model_name = st.selectbox("انتخاب مدل:", list(MODELS[model_cat].keys()))
                technical_model_id = MODELS[model_cat][selected_model_name]["id"]

        st.markdown("---")

        # load messages on first render for current conv
        if st.session_state.current_conv_id and not st.session_state.messages:
            st.session_state.messages = run_async(db_get_messages(st.session_state.current_conv_id, limit=PAGE_SIZE_MESSAGES, offset=0))

        chat_box = st.container()

        if st.session_state.current_conv_id:
            if st.button("⤴️ بارگذاری پیام‌های قدیمی‌تر", use_container_width=True):
                st.session_state.messages_offset = st.session_state.get("messages_offset", 0) + PAGE_SIZE_MESSAGES
                older = run_async(db_get_messages(st.session_state.current_conv_id, limit=PAGE_SIZE_MESSAGES, offset=st.session_state.messages_offset))
                st.session_state.messages = older + st.session_state.messages
                st.experimental_rerun()

        for m in st.session_state.messages:
            role = m.get("role","user")
            with chat_box.chat_message(role):
                if m.get("type")=="image":
                    st.image(m["content"], caption=m.get("caption","تصویر تولید شده"))
                elif m.get("type")=="video":
                    st.video(m["content"])
                else:
                    st.markdown(m["content"])

        # input form
        with st.form("chat_input_form", clear_on_submit=True):
            user_prompt = st.text_input("پیام خود را بنویسید...", key="user_prompt")
            submit = st.form_submit_button("ارسال")
            if submit and user_prompt:
                now = now_ts()
                last = st.session_state.get("last_prompt_ts", 0.0)
                if now - last < MIN_SECONDS_BETWEEN_PROMPTS:
                    st.warning("لطفاً چند لحظه صبر کنید.")
                else:
                    st.session_state.last_prompt_ts = now
                    # process synchronously by calling async db/genai where needed
                    self.process_chat_input(user_prompt, technical_model_id, user_payload)

        st.markdown("---")
        with st.expander("ℹ️ اطلاعات مدل‌ها و قابلیت‌ها — جمع‌وجور"):
            self.render_models_info_compact()

    # ---------- process input (sync wrapper) ----------
    def process_chat_input(self, prompt: str, model_id: str, user_payload: dict):
        """synchronous wrapper that calls async DB operations and genai via sync helper"""
        try:
            user_id = user_payload["sub"]
            conv_id = st.session_state.current_conv_id
            if not conv_id:
                conv_id = run_async(db_create_conversation(user_id, prompt[:40] or "مکالمه جدید"))
                st.session_state.current_conv_id = conv_id
                st.session_state.messages_offset = 0

            user_msg = {"_id": str(uuid.uuid4()), "role": "user", "type": "text", "content": prompt, "ts": datetime.now(timezone.utc)}
            st.session_state.messages.append(user_msg)
            run_async(db_append_message(conv_id, user_msg))

            with st.spinner("در حال پردازش..."):
                if st.session_state.media_mode:
                    # generate media placeholder
                    media_url = run_async(self._generate_media_async(prompt, model_id))
                    if "image" in model_id.lower() or "imagen" in model_id.lower():
                        ai_msg = {"_id": str(uuid.uuid4()), "role":"assistant", "type":"image", "content":media_url, "ts": datetime.now(timezone.utc)}
                        st.image(media_url)
                    else:
                        ai_msg = {"_id": str(uuid.uuid4()), "role":"assistant", "type":"video", "content":media_url, "ts": datetime.now(timezone.utc)}
                        st.video(media_url)
                    st.session_state.last_media = media_url
                else:
                    # prepare api_history for Gemini
                    text_history = [m for m in st.session_state.messages if m.get("type","text")=="text"]
                    api_history = [{"role": "user" if m["role"]=="user" else "model", "parts":[{"text": m["content"]}]} for m in text_history]
                    # call gemini synchronously
                    full_text = call_gemini_sync(api_history, model_id)
                    # display response
                    st.markdown(full_text)
                    ai_msg = {"_id": str(uuid.uuid4()), "role":"assistant", "type":"text", "content": full_text, "ts": datetime.now(timezone.utc)}

                st.session_state.messages.append(ai_msg)
                run_async(db_append_message(conv_id, ai_msg))

            st.experimental_rerun()
        except Exception as e:
            logger.exception("Error in process_chat_input: %s", e)
            st.error(f"خطا در پردازش پیام: {e}")

    # ---------- media async impl ----------
    async def _generate_media_async(self, prompt: str, model_id: str) -> str:
        # placeholder: replace with real API call if available
        if "image" in model_id.lower() or "imagen" in model_id.lower():
            await asyncio.sleep(2.2)
            return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"
        if "veo" in model_id.lower() or "video" in model_id.lower():
            await asyncio.sleep(4)
            return "https://www.w3schools.com/html/mov_bbb.mp4"
        await asyncio.sleep(2)
        return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"

    # ---------- profile ----------
    def render_profile(self, user_payload: dict):
        self.render_sidebar(user_payload)
        st.title("👤 پروفایل شما")
        with st.form("profile_form"):
            name = st.text_input("نام کامل", value=user_payload.get("name",""))
            st.write(f"**ایمیل:** `{user_payload.get('email','')}`")
            if st.form_submit_button("ذخیره"):
                try:
                    run_async(USERS_COLL.update_one({"_id": ObjectId(user_payload["sub"])}, {"$set": {"name": name}}))
                    st.success("پروفایل با موفقیت به‌روزرسانی شد.")
                    # rebuild token with new name (sub field kept same)
                    new_payload = {"id": user_payload["sub"], "name": name, "email": user_payload.get("email","")}
                    st.session_state.token = create_jwt_token(new_payload)
                    time.sleep(0.6)
                    st.experimental_rerun()
                except Exception as e:
                    logger.exception("Profile update error: %s", e)
                    st.error("خطا در بروزرسانی پروفایل.")

    # ---------- router ----------
    def run(self):
        self._init_session_state()
        token = st.session_state.get("token")
        user_payload = decode_jwt_token(token) if token else None

        if not user_payload:
            self.render_login_signup()
            return

        # load conversations once
        if not st.session_state.get("initialized", False):
            try:
                st.session_state.conversations_list = run_async(db_get_conversations(user_payload["sub"]))
            except Exception as e:
                logger.exception("Error loading convs: %s", e)
            st.session_state.initialized = True

        page = st.session_state.get("page","dashboard")
        if page == "dashboard":
            self.render_dashboard(user_payload)
        elif page == "profile":
            self.render_profile(user_payload)
        else:
            st.error("صفحهٔ نامعتبر")

#==============================================================================
# ۱۰. فاز ۶.۱: اجرای اپ
#==============================================================================
if __name__ == "__main__":
    app = JarvisSenpai()
    try:
        app.run()
    except Exception as e:
        logger.exception("Unhandled exception in main: %s", e)
        st.error(f"خطای داخلی: {e}")
