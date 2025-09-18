# app.py
import streamlit as st
import asyncio
import bcrypt
import logging
import time
import pandas as pd
import uuid
import jwt
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import google.generativeai as genai
from google.api_core.exceptions import GoogleAPICallError

# ==============================================================================
# ۱. فاز ۱.۳: پیکربندی و استایل (Config & Styling)
# ==============================================================================

# st.set_page_config فقط یک بار در ابتدای اسکریپت فراخوانی می‌شود
st.set_page_config(page_title="Jarvis Elite", layout="wide", initial_sidebar_state="collapsed")

# تمام CSS به صورت داخلی برای حذف وابستگی به فایل خارجی
APP_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;700&display=swap');

    /* General Styling */
    html, body, [class*="st-"], .st-emotion-cache-1yycg8b, .st-emotion-cache-1av2t4g {
        font-family: 'Vazirmatn', sans-serif;
        direction: rtl;
    }

    /* Responsive Design for smaller screens */
    @media (max-width: 768px) {
        .st-emotion-cache-1cypcdb {
            flex-direction: column;
        }
    }

    /* Custom Buttons */
    .stButton>button[kind="primary"] {
        background-color: #3b82f6;
        color: white;
        border-radius: 12px;
        border: none;
        padding: 12px 24px;
        transition: background-color 0.2s ease-in-out;
    }
    .stButton>button[kind="primary"]:hover {
        background-color: #2563eb;
    }

    /* Chat UI enhancements */
    .stChatMessage {
        border-radius: 12px;
        border: 1px solid #374151;
        background-color: #1f2937;
        margin-bottom: 1rem;
    }
    .stChatMessage:has(div[data-testid="stChatMessageContent.user"]) {
        background-color: #2563eb;
        color: white;
    }
</style>
"""
st.markdown(APP_CSS, unsafe_allow_html=True)

# --- بارگذاری اطلاعات حساس ---
try:
    MONGO_URI = st.secrets["mongo"]["uri"]
    GEMINI_API_KEY = st.secrets["google_ai"]["api_key"]
    JWT_SECRET_KEY = st.secrets["auth"]["jwt_secret_key"]
    genai.configure(api_key=GEMINI_API_KEY)
except KeyError as e:
    st.error(f"خطا در فایل secrets.toml: مقدار '{e.args[0]}' یافت نشد.")
    st.stop()

# ==============================================================================
# ۲. ماژول پایگاه داده (Database Module)
# ==============================================================================

@st.cache_resource
def get_db_client():
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        return client
    except Exception as e:
        st.error(f"اتصال به پایگاه داده ناموفق بود: {e}")
        st.stop()

db = get_db_client().jarvis_elite_final
users_coll = db["users"]
conversations_coll = db["conversations"]

async def get_user_by_email(email: str):
    return await users_coll.find_one({"email": email})

# ... (سایر توابع پایگاه داده در ادامه استفاده می‌شوند)

# ==============================================================================
# ۳. فاز ۱.۲: ماژول احراز هویت با JWT (Auth Module)
# ==============================================================================

def hash_password(password: str): return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
def verify_password(p, h): return bcrypt.checkpw(p.encode(), h.encode())

def create_jwt_token(user_info: dict):
    payload = {
        "exp": datetime.now(timezone.utc) + timedelta(days=1),
        "iat": datetime.now(timezone.utc),
        "sub": user_info["id"],
        "name": user_info["name"],
        "email": user_info["email"]
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

def decode_jwt_token(token: str):
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

def initialize_session():
    if 'token' not in st.session_state: st.session_state.token = None
    if 'page' not in st.session_state: st.session_state.page = 'login'
    # ... سایر مقادیر اولیه

def logout_user():
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.toast("با موفقیت خارج شدید!", icon="👋")

# ==============================================================================
# ۴. فاز ۲.۱: ماژول مدل‌های هوش مصنوعی (AI Models Module)
# ==============================================================================

MODELS = {
    "چت متنی": {
        "Gemini 1.5 Pro": {"id": "gemini-1.5-pro-latest", "RPM": 5, "RPD": 100, "capabilities": "چت و پاسخ متن"},
        "Gemini 1.5 Flash": {"id": "gemini-1.5-flash-latest", "RPM": 10, "RPD": 250, "capabilities": "چت سریع"},
    },
    "تولید تصویر": {
        "Imagen 3 (Placeholder)": {"id": "imagen-3", "RPM": 10, "RPD": 100, "capabilities": "تولید تصویر"},
    },
    "تولید ویدیو": {
        "Veo (Placeholder)": {"id": "veo-3", "RPM": 5, "RPD": 50, "capabilities": "تولید ویدیو"},
    }
}

async def stream_gemini_response(history, model_id):
    try:
        model = genai.GenerativeModel(model_id)
        api_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in history if m["type"]=="text"]
        response_stream = await model.generate_content_async(api_history, stream=True)
        async for chunk in response_stream:
            if chunk.text: yield chunk.text
    except Exception as e:
        yield f"**خطا در ارتباط با API:** `{str(e)}`"

async def generate_media(prompt: str, model_id: str):
    await asyncio.sleep(2) # شبیه‌سازی تاخیر API
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"

# ==============================================================================
# ۵. رندر صفحات برنامه (Page Renderers)
# ==============================================================================

async def render_login_page():
    st.title("به پلتفرم هوشمند Jarvis Elite خوش آمدید 👑")
    tab1, tab2 = st.tabs(["**ورود**", "**ثبت‌نام**"])
    with tab1:
        with st.form("login"):
            email = st.text_input("ایمیل")
            password = st.text_input("رمز عبور", type="password")
            if st.form_submit_button("ورود", type="primary", use_container_width=True):
                user = await get_user_by_email(email)
                if user and verify_password(password, user["password"]):
                    user_info = {"id": str(user["_id"]), "name": user["name"], "email": user["email"]}
                    st.session_state.token = create_jwt_token(user_info)
                    st.session_state.page = 'dashboard'
                    st.rerun()
                else:
                    st.error("ایمیل یا رمز عبور اشتباه است.")
    with tab2:
        with st.form("signup"):
            name = st.text_input("نام کامل")
            email = st.text_input("ایمیل", key="s_email")
            p1 = st.text_input("رمز عبور", type="password", key="s_p1")
            p2 = st.text_input("تکرار رمز عبور", type="password", key="s_p2")
            if st.form_submit_button("ثبت‌نام", use_container_width=True):
                if p1 != p2: st.error("رمزهای عبور مطابقت ندارند."); return
                if await get_user_by_email(email): st.error("این ایمیل قبلاً ثبت شده."); return
                await users_coll.insert_one({"name": name, "email": email, "password": hash_password(p1)})
                st.success("ثبت‌نام موفق بود! اکنون وارد شوید."); time.sleep(1)

async def render_dashboard_page(user_info: dict):
    user_id = user_info["sub"]

    # --- مقداردهی اولیه وضعیت چت ---
    if "current_conv_id" not in st.session_state: st.session_state.current_conv_id = None
    if "messages" not in st.session_state: st.session_state.messages = []

    # --- سایدبار و مدیریت مکالمات ---
    with st.sidebar:
        st.header(f"کاربر: {user_info['name']}")
        if st.button("➕ مکالمه جدید", use_container_width=True, type="primary"):
            st.session_state.current_conv_id = None
            st.session_state.messages = []
            st.rerun()
        st.markdown("---")
        st.subheader("تاریخچه مکالمات")
        
        # فاز ۲.۳: کش کردن لیست مکالمات
        @st.cache_data(ttl=60)
        async def get_conversations(uid):
            cursor = conversations_coll.find({"user_id": ObjectId(uid)}).sort("created_at", -1)
            return await cursor.to_list(length=100)

        conversations = await get_conversations(user_id)
        for conv in conversations:
            conv_id_str = str(conv['_id'])
            if st.button(conv['title'][:30], key=conv_id_str, use_container_width=True,
                          type="secondary" if conv_id_str != st.session_state.current_conv_id else "primary"):
                st.session_state.current_conv_id = conv_id_str
                conv_data = await conversations_coll.find_one({"_id": ObjectId(conv_id_str)})
                st.session_state.messages = conv_data.get("messages", [])
                st.rerun()
        
        st.markdown("---")
        if st.button("👤 پروفایل", use_container_width=True): st.session_state.page = 'profile'; st.rerun()
        if st.button("خروج از حساب", use_container_width=True): logout_user(); st.rerun()

    # --- هدر و انتخاب مدل ---
    title = next((c['title'] for c in conversations if str(c['_id']) == st.session_state.current_conv_id), "مکالمه جدید")
    st.header(f"💬 {title}")
    
    col1, col2 = st.columns([2, 1])
    with col1: model_cat = st.selectbox("نوع مدل:", list(MODELS.keys()))
    with col2: model_name = st.selectbox("انتخاب مدل:", list(MODELS[model_cat].keys()))
    model_id = MODELS[model_cat][model_name]["id"]

    # --- نمایش تاریخچه چت ---
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg.get("type") == "image": st.image(msg["content"])
            else: st.markdown(msg["content"])

    # --- ورودی کاربر ---
    if prompt := st.chat_input("پیام خود را بنویسید..."):
        conv_id = st.session_state.current_conv_id
        if not conv_id:
            new_conv = {"user_id": ObjectId(user_id), "title": prompt[:40], "created_at": datetime.now(timezone.utc), "messages": []}
            result = await conversations_coll.insert_one(new_conv)
            conv_id = str(result.inserted_id)
            st.session_state.current_conv_id = conv_id
            st.cache_data.clear() # پاک کردن کش لیست مکالمات

        user_msg = {"role": "user", "content": prompt, "type": "text"}
        st.session_state.messages.append(user_msg)
        await conversations_coll.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": user_msg}})
        
        with st.chat_message("assistant"):
            if "چت" in model_cat:
                with st.spinner("دستیار در حال فکر کردن است..."):
                    res_gen = stream_gemini_response(st.session_state.messages, model_id)
                    full_res = st.write_stream(res_gen)
                    ai_msg = {"role": "assistant", "content": full_res, "type": "text"}
            else:
                with st.spinner("در حال تولید رسانه..."):
                    url = await generate_media(prompt, model_id)
                    st.image(url)
                    ai_msg = {"role": "assistant", "content": url, "type": "image"}
        
        st.session_state.messages.append(ai_msg)
        await conversations_coll.update_one({"_id": ObjectId(conv_id)}, {"$push": {"messages": ai_msg}})
        st.rerun()

async def render_profile_page(user_info: dict):
    st.sidebar.button("بازگشت به چت", on_click=lambda: st.session_state.update(page='dashboard'), use_container_width=True)
    st.title("👤 پروفایل کاربری")
    with st.form("profile"):
        name = st.text_input("نام کامل", value=user_info["name"])
        st.text_input("ایمیل", value=user_info["email"], disabled=True)
        if st.form_submit_button("ذخیره تغییرات", type="primary", use_container_width=True):
            await users_coll.update_one({"_id": ObjectId(user_info["sub"])}, {"$set": {"name": name}})
            st.success("پروفایل با موفقیت به‌روز شد!")
            st.balloons()
            # فاز ۳.۱: بازسازی توکن برای نمایش نام جدید
            user_info["name"] = name
            st.session_state.token = create_jwt_token(user_info)
            time.sleep(1); st.rerun()

# ==============================================================================
# ۶. فاز ۱.۱: روتر اصلی برنامه (Main Application Router)
# ==============================================================================
async def main():
    initialize_session()
    user_info = decode_jwt_token(st.session_state.token)

    if not user_info:
        await render_login_page()
    elif st.session_state.page == 'dashboard':
        await render_dashboard_page(user_info)
    elif st.session_state.page == 'profile':
        await render_profile_page(user_info)

if __name__ == "__main__":
    asyncio.run(main())
