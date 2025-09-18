import os
import uuid
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, AsyncGenerator

import streamlit as st
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import google.generativeai as genai
import pandas as pd

# --- 1. Page & CSS ---
st.set_page_config(page_title="Jarvis Elite - Gemini UI", page_icon="👑", layout="centered")
load_dotenv()

st.markdown("""
<style>
/* Chat & Media UI */
body {direction: rtl;}
.stButton>button {border-radius: 8px; border: 1px solid #404040; transition: all 0.2s ease;}
.stButton>button:hover {border-color: #3b82f6; color: #3b82f6;}
.stChatMessage {border-radius: 12px; border: 1px solid #222; background-color: #1a1a1a; margin-bottom: 1rem;}
.stChatMessage:has(div[data-testid="stChatMessageContent.user"]) {background-color: #2563eb; color: white;}
.stChatMessage img {border-radius: 8px; max-width: 100%;}
[data-testid="stSidebar"] {background-color: #111; border-right: 1px solid #222; padding: 1rem;}
</style>
""", unsafe_allow_html=True)

# --- 2. Config ---
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user"

# --- Session State Initialization ---
for key, default in [
    ("messages", []),
    ("conversations", []),
    ("current_conv_id", None),
    ("current_title", "مکالمه جدید"),
    ("initialized", False),
    ("media_mode", False),
    ("last_media", None)
]:
    if key not in st.session_state:
        st.session_state[key] = default

# --- 3. DB ---
@st.cache_resource
def get_db_client(): return AsyncIOMotorClient(MONGO_URI)
client = get_db_client()
db = client["jarvis_elite_definitive"]
users_coll = db["users"]

async def db_create_conversation(conv_id: str, title: str):
    conv = {"_id": conv_id, "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
    await users_coll.update_one({"_id": USER_ID}, {"$push": {"conversations": conv}}, upsert=True)

async def db_get_conversations() -> List[Dict]:
    doc = await users_coll.find_one({"_id": USER_ID}, {"conversations.messages": 0})
    if not doc or "conversations" not in doc: return []
    return sorted(doc["conversations"], key=lambda c: c["created_at"], reverse=True)

async def db_get_messages(conv_id: str) -> List[Dict]:
    doc = await users_coll.find_one({"_id": USER_ID, "conversations._id": conv_id}, {"conversations.$": 1})
    return doc["conversations"][0].get("messages", []) if doc and doc.get("conversations") else []

async def db_save_message(conv_id: str, msg: Dict):
    await users_coll.update_one({"_id": USER_ID, "conversations._id": conv_id}, {"$push": {"conversations.$.messages": msg}})

# --- 4. Models ---
MODELS = {
    "چت متنی": {
        "Gemini 2.5 Pro": {"id": "gemini-2.5-pro", "RPM": 5, "RPD": 100, "capabilities": "چت و پاسخ متن"},
        "Gemini 2.5 Flash": {"id": "gemini-2.5-flash", "RPM": 10, "RPD": 250, "capabilities": "چت سریع"},
        "Gemini 2.5 Flash-Lite": {"id": "gemini-2.5-flash-lite", "RPM": 15, "RPD": 1000, "capabilities": "حجم بالا، بهینه"},
        "Gemini 2.0 Pro": {"id": "gemini-2.0-pro", "RPM": 15, "RPD": 200, "capabilities": "پایدار"},
        "Gemini 2.0 Flash": {"id": "gemini-2.0-flash", "RPM": 30, "RPD": 200, "capabilities": "سهمیه بالا"}
    },
    "تولید تصویر": {
        "Gemini 2.5 Flash Image": {"id":"gemini-2.5-flash-image-preview","RPM":10,"RPD":100,"capabilities":"تولید تصویر"},
        "Gemini 2.0 Flash Image": {"id":"gemini-2.0-flash-image","RPM":15,"RPD":200,"capabilities":"تولید تصویر پایدار"}
    },
    "تولید ویدیو": {
        "Veo 3": {"id":"veo-3","RPM":5,"RPD":50,"capabilities":"تولید ویدیو"}
    }
}

async def stream_gemini_response(api_msgs: List[Dict], model: str) -> AsyncGenerator[str, Any]:
    if not GEMINI_API_KEY:
        yield "**خطا: کلید API گوگل تنظیم نشده است.**"
        return
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        model_instance = genai.GenerativeModel(model)
        response_stream = await model_instance.generate_content_async(api_msgs, stream=True)
        async for chunk in response_stream:
            if chunk.text: yield chunk.text
    except Exception as e:
        yield f"**خطای API:** `{e}`"

async def generate_media(prompt: str, model_id: str) -> str:
    import time
    time.sleep(2)
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"

# --- 5. Sidebar ---
with st.sidebar:
    st.title("👑 Jarvis Elite")
    if st.button("➕ مکالمه جدید", use_container_width=True, type="primary"):
        st.session_state.current_conv_id = None
        st.session_state.messages = []
        st.session_state.current_title = "مکالمه جدید"
        st.rerun()
    st.markdown("---")
    st.subheader("تاریخچه مکالمات")
    if not st.session_state.initialized:
        st.session_state.conversations = asyncio.run(db_get_conversations())
        st.session_state.initialized = True
    for conv in st.session_state.conversations:
        is_active = conv['_id'] == st.session_state.current_conv_id
        if st.button(f"{'🔹' if is_active else ''} {conv['title'][:25]}", key=conv['_id'], use_container_width=True, type="secondary"):
            if not is_active:
                st.session_state.current_conv_id = conv['_id']
                st.session_state.current_title = conv['title']
                st.session_state.messages = asyncio.run(db_get_messages(conv['_id']))
                st.rerun()

# --- 6. Header & Model Selection ---
col1, col2 = st.columns([3,2])
col1.header(st.session_state.current_title)
with col2:
    st.session_state.media_mode = st.checkbox("فعال کردن حالت تصویر/ویدیو")
    if st.session_state.media_mode:
        media_type = st.radio("نوع محتوا:", ["تولید تصویر","تولید ویدیو"], horizontal=True)
        selected_model_name = st.selectbox("مدل:", list(MODELS[media_type].keys()))
        technical_model_id = MODELS[media_type][selected_model_name]["id"]
    else:
        active_mode = "چت متنی"
        selected_model_name = st.selectbox("مدل:", list(MODELS[active_mode].keys()))
        technical_model_id = MODELS[active_mode][selected_model_name]["id"]

# --- 7. Chat History ---
chat_container = st.container()
for msg in st.session_state.messages:
    with chat_container.chat_message(msg["role"], avatar="🧑‍💻" if msg["role"]=="user" else "🤖"):
        if msg.get("type")=="image":
            st.image(msg["content"], caption="تصویر تولید شده")
        elif msg.get("type")=="video":
            st.video(msg["content"])
        else:
            st.markdown(msg["content"])

# --- 8. Input Handling ---
if prompt := st.chat_input("پیام خود را بنویسید..."):
    conv_id = st.session_state.current_conv_id
    is_new_conv = not conv_id
    if is_new_conv:
        conv_id = str(uuid.uuid4())
        st.session_state.current_conv_id = conv_id
        st.session_state.current_title = prompt[:40]
        asyncio.run(db_create_conversation(conv_id, st.session_state.current_title))
    
    user_message = {"_id": str(uuid.uuid4()), "role":"user","type":"text","content":prompt}
    st.session_state.messages.append(user_message)
    asyncio.run(db_save_message(conv_id, user_message))
    if is_new_conv:
        st.session_state.conversations = asyncio.run(db_get_conversations())
    st.rerun()

# --- 9. Response Generation ---
if st.session_state.messages and st.session_state.messages[-1]["role"]=="user":
    last_prompt = st.session_state.messages[-1]["content"]
    conv_id = st.session_state.current_conv_id
    with chat_container.chat_message("assistant", avatar="🤖"):
        if st.session_state.media_mode:
            media_url = asyncio.run(generate_media(last_prompt, technical_model_id))
            if media_type=="تولید تصویر":
                st.image(media_url)
                msg_type = "image"
            else:
                st.video(media_url)
                msg_type = "video"
            ai_message = {"_id": str(uuid.uuid4()),"role":"assistant","type":msg_type,"content":media_url}
        else:
            text_history = [{"role": m["role"], "parts":[{"text": m["content"]}]} for m in st.session_state.messages if m["type"]=="text"]
            response_gen = stream_gemini_response(text_history, technical_model_id)
            full_response = st.write_stream(response_gen)
            ai_message = {"_id": str(uuid.uuid4()),"role":"assistant","type":"text","content":full_response}
        st.session_state.messages.append(ai_message)
        asyncio.run(db_save_message(conv_id, ai_message))
        st.rerun()

# --- 10. جمع و جور: جدول مدل‌ها ---
with st.expander("ℹ️ اطلاعات مدل‌ها و قابلیت‌ها"):
    st.markdown("نمایش ویژگی‌ها و محدودیت‌های مدل‌های Gemini. **RPM** و **RPD** فقط اطلاع‌رسانی است، فیلتر اعمال نمی‌شود.")
    model_info = {
        "نام مدل": [name for group in MODELS.values() for name in group.keys()],
        "نوع": [g for group in MODELS.values() for g in [k for k,v in group.items() for _ in range(1)]],
        "قابلیت": [v["capabilities"] for group in MODELS.values() for v in group.values()],
        "حد در دقیقه (RPM)": [v["RPM"] for group in MODELS.values() for v in group.values()],
        "حد در روز (RPD)": [v["RPD"] for group in MODELS.values() for v in group.values()],
    }
    st.dataframe(pd.DataFrame(model_info), use_container_width=True)
