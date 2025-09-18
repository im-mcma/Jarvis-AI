# app.py
import streamlit as st
import asyncio
import bcrypt
import logging
import time
import pandas as pd
import uuid
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import google.generativeai as genai
from google.api_core.exceptions import GoogleAPICallError

# ==============================================================================
# ۱. تنظیمات اولیه و پیکربندی
# ==============================================================================

# --- تنظیمات صفحه ---
st.set_page_config(page_title="Jarvis Elite", layout="wide", initial_sidebar_state="collapsed")

# --- بارگذاری استایل سفارشی ---
def load_css(file_path):
    try:
        with open(file_path) as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning(f"فایل CSS در مسیر '{file_path}' یافت نشد.")

load_css("assets/style.css")

# --- تنظیمات لاگ‌گیری ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ۱.۱. پیکربندی هوش مصنوعی ---
try:
    MONGO_URI = st.secrets["mongo"]["uri"]
    GEMINI_API_KEY = st.secrets["google_ai"]["api_key"]
    genai.configure(api_key=GEMINI_API_KEY)
except KeyError as e:
    st.error(f"خطا: مقدار '{e.args[0]}' در فایل secrets.toml تعریف نشده است.")
    st.stop()


# ==============================================================================
# ۲. ماژول پایگاه داده (بازسازی شده)
# ==============================================================================

@st.cache_resource
def get_db_client():
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        logging.info("MongoDB client connected successfully.")
        return client
    except Exception as e:
        logging.error(f"Failed to connect to MongoDB: {e}")
        st.error("اتصال به پایگاه داده برقرار نشد.")
        st.stop()

client = get_db_client()
db = client["jarvis_elite_app"]
users_coll = db["users"]
conversations_coll = db["conversations"] # کالکشن مجزا برای مکالمات

# --- توابع مرتبط با کاربر ---
async def get_user_by_email(email: str):
    return await users_coll.find_one({"email": email})
async def create_user(name: str, email: str, hashed_password: str):
    await users_coll.insert_one({"name": name, "email": email, "password": hashed_password})
async def update_user_profile(user_id: str, updates: dict):
    await users_coll.update_one({"_id": ObjectId(user_id)}, {"$set": updates})

# --- توابع مرتبط با مکالمات (بازسازی شده) ---
async def db_create_conversation(user_id: str, title: str):
    conv_data = {
        "user_id": ObjectId(user_id),
        "title": title,
        "created_at": datetime.now(timezone.utc),
        "messages": []
    }
    result = await conversations_coll.insert_one(conv_data)
    return str(result.inserted_id)

async def db_get_conversations(user_id: str):
    cursor = conversations_coll.find({"user_id": ObjectId(user_id)}).sort("created_at", -1)
    return await cursor.to_list(length=100)

async def db_get_messages(conv_id: str):
    conv = await conversations_coll.find_one({"_id": ObjectId(conv_id)})
    return conv.get("messages", []) if conv else []

async def db_save_message(conv_id: str, msg: dict):
    await conversations_coll.update_one(
        {"_id": ObjectId(conv_id)},
        {"$push": {"messages": msg}}
    )


# ==============================================================================
# ۳. ماژول احراز هویت (بدون تغییر)
# ==============================================================================
def hash_password(password: str): return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
def verify_password(password: str, hashed: str): return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def initialize_session():
    defaults = {
        'authenticated': False, 'user_info': None, 'page': 'login',
        'messages': [], 'conversations': [], 'current_conv_id': None
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

def login_user(user_info):
    st.session_state.authenticated = True
    st.session_state.user_info = {"id": str(user_info["_id"]), "name": user_info["name"], "email": user_info["email"]}
    st.session_state.page = 'dashboard'

def logout_user():
    initialize_session() # Reset all session state keys to default
    st.toast("با موفقیت خارج شدید!", icon="👋")


# ==============================================================================
# ۴. ماژول مدل‌های هوش مصنوعی (جدید)
# ==============================================================================
@st.cache_data
def get_models_config():
    return {
        "چت متنی": {
            "Gemini 1.5 Flash": {"id": "gemini-1.5-flash-latest", "capabilities": "سرعت بالا و کارایی بهینه برای چت"},
            "Gemini 1.5 Pro": {"id": "gemini-1.5-pro-latest", "capabilities": "پیشرفته‌ترین مدل برای تحلیل‌های پیچیده"},
        },
        "تولید تصویر (شبیه‌سازی شده)": {
            "Imagen 3": {"id": "imagen-3-placeholder", "capabilities": "تولید تصاویر با کیفیت بالا"},
        }
    }
MODELS = get_models_config()

async def stream_gemini_response(api_msgs, model_id):
    try:
        model = genai.GenerativeModel(model_id)
        response_stream = await model.generate_content_async(api_msgs, stream=True)
        async for chunk in response_stream:
            if chunk.text:
                yield chunk.text
    except GoogleAPICallError as e:
        yield f"**خطای API:** `{e.message}`. لطفاً کلید API خود را بررسی کنید."
    except Exception as e:
        logging.error(f"Gemini streaming error: {e}", exc_info=True)
        yield "**خطای ناشناخته:** مشکلی در پردازش درخواست رخ داد."

async def generate_media(prompt, model_id):
    # این تابع شبیه‌سازی شده است. در یک پروژه واقعی، کد اتصال به API تولید تصویر در اینجا قرار می‌گیرد.
    await asyncio.sleep(2) # شبیه‌سازی تاخیر شبکه
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"


# ==============================================================================
# ۵. تعریف صفحات برنامه
# ==============================================================================

def render_login_page():
    st.set_page_config(page_title="ورود", page_icon="🔐")
    st.title("به پلتفرم هوشمند Jarvis Elite خوش آمدید 👑")
    tab1, tab2 = st.tabs(["**ورود**", "**ثبت‌نام**"])
    with tab1:
        with st.form("login"):
            email, password = st.text_input("ایمیل"), st.text_input("رمز", type="password")
            if st.form_submit_button("ورود", type="primary", use_container_width=True):
                user = asyncio.run(get_user_by_email(email))
                if user and verify_password(password, user["password"]): login_user(user); st.rerun()
                else: st.error("ایمیل یا رمز عبور اشتباه است.")
    with tab2:
        with st.form("signup"):
            name, email = st.text_input("نام"), st.text_input("ایمیل", key="signup_email")
            p1, p2 = st.text_input("رمز", type="password"), st.text_input("تکرار رمز", type="password")
            if st.form_submit_button("ثبت‌نام", use_container_width=True):
                if p1 != p2: st.error("رمزها مطابقت ندارند."); return
                if asyncio.run(get_user_by_email(email)): st.error("این ایمیل قبلاً ثبت شده."); return
                asyncio.run(create_user(name, email, hash_password(p1)))
                st.success("ثبت‌نام موفق بود! اکنون وارد شوید."); time.sleep(2); st.rerun()

async def render_dashboard_page():
    st.set_page_config(page_title="چت‌باکس هوشمند", page_icon="💬", initial_sidebar_state="auto")
    user_id = st.session_state.user_info["id"]

    # --- سایدبار و مدیریت مکالمات ---
    with st.sidebar:
        st.header(f"کاربر: {st.session_state.user_info['name']}")
        if st.button("➕ مکالمه جدید", use_container_width=True, type="primary"):
            st.session_state.current_conv_id = None
            st.session_state.messages = []
            st.rerun()

        st.markdown("---")
        st.subheader("تاریخچه مکالمات")
        st.session_state.conversations = await db_get_conversations(user_id)
        for conv in st.session_state.conversations:
            conv_id_str = str(conv['_id'])
            if st.button(conv['title'][:30], key=conv_id_str, use_container_width=True,
                          type="secondary" if conv_id_str != st.session_state.current_conv_id else "primary"):
                st.session_state.current_conv_id = conv_id_str
                st.session_state.messages = await db_get_messages(conv_id_str)
                st.rerun()
        
        st.markdown("---")
        if st.button("👤 پروفایل", use_container_width=True): st.session_state.page = 'profile'; st.rerun()
        if st.button("خروج از حساب", use_container_width=True): logout_user(); st.rerun()

    # --- هدر و انتخاب مدل ---
    current_title = next((c['title'] for c in st.session_state.conversations if str(c['_id']) == st.session_state.current_conv_id), "مکالمه جدید")
    st.header(f"💬 {current_title}")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        model_category = st.selectbox("نوع مدل:", list(MODELS.keys()))
    with col2:
        selected_model_name = st.selectbox("انتخاب مدل:", list(MODELS[model_category].keys()))
        technical_model_id = MODELS[model_category][selected_model_name]["id"]

    # --- نمایش تاریخچه چت ---
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg.get("type") == "image":
                st.image(msg["content"], caption="تصویر تولید شده")
            else:
                st.markdown(msg["content"])

    # --- ورودی کاربر و مدیریت پیام ---
    if prompt := st.chat_input("پیام خود را بنویسید..."):
        # اگر مکالمه جدید است، آن را در دیتابیس ایجاد کن
        conv_id = st.session_state.current_conv_id
        if not conv_id:
            conv_id = await db_create_conversation(user_id, prompt[:40])
            st.session_state.current_conv_id = conv_id
        
        # ذخیره پیام کاربر
        user_message = {"role": "user", "content": prompt, "type": "text"}
        st.session_state.messages.append(user_message)
        await db_save_message(conv_id, user_message)
        
        # تولید و ذخیره پاسخ مدل
        with st.chat_message("assistant"):
            if model_category == "چت متنی":
                api_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in st.session_state.messages if m["type"]=="text"]
                response_gen = stream_gemini_response(api_history, technical_model_id)
                full_response = st.write_stream(response_gen)
                ai_message = {"role": "assistant", "content": full_response, "type": "text"}
            else: # تولید تصویر
                with st.spinner("در حال تولید تصویر..."):
                    media_url = await generate_media(prompt, technical_model_id)
                    st.image(media_url)
                    ai_message = {"role": "assistant", "content": media_url, "type": "image"}
        
        st.session_state.messages.append(ai_message)
        await db_save_message(conv_id, ai_message)
        st.rerun()

    # --- جدول اطلاعات مدل‌ها ---
    with st.expander("ℹ️ اطلاعات مدل‌ها"):
        model_info = [{"نام مدل": name, "نوع": cat, **data} for cat, group in MODELS.items() for name, data in group.items()]
        st.dataframe(pd.DataFrame(model_info).drop(columns=['id']), use_container_width=True, hide_index=True)


async def render_profile_page():
    st.set_page_config(page_title="پروفایل", page_icon="👤", initial_sidebar_state="auto")
    st.sidebar.button("بازگشت به چت", on_click=lambda: st.session_state.update(page='dashboard'), use_container_width=True, type="primary")

    st.title("👤 پروفایل کاربری")
    user_info = st.session_state.user_info
    with st.form("profile_form"):
        name = st.text_input("نام کامل", value=user_info["name"])
        st.text_input("ایمیل", value=user_info["email"], disabled=True)
        new_password = st.text_input("رمز عبور جدید", type="password", placeholder="برای تغییر، وارد کنید")
        if st.form_submit_button("ذخیره تغییرات", use_container_width=True, type="primary"):
            updates = {"name": name}
            if new_password: updates["password"] = hash_password(new_password)
            await update_user_profile(user_info["id"], updates)
            st.session_state.user_info["name"] = name
            st.success("پروفایل شما با موفقیت به‌روز شد!"); st.balloons()


# ==============================================================================
# ۶. روتر اصلی برنامه
# ==============================================================================
async def main():
    initialize_session()
    if not st.session_state.authenticated:
        render_login_page()
    elif st.session_state.page == 'dashboard':
        await render_dashboard_page()
    elif st.session_state.page == 'profile':
        await render_profile_page()

if __name__ == "__main__":
    asyncio.run(main())
