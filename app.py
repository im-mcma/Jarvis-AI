import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Generator, Any
from io import BytesIO

import streamlit as st
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import google.generativeai as genai
from gtts import gTTS

# --- 1. PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Jarvis Elite - Definitive Edition",
    page_icon="👑",
    layout="centered",
    initial_sidebar_state="expanded"
)
load_dotenv()

# --- 2. CUSTOM CSS FOR A PREMIUM UI ---
st.markdown("""
<style>
    /* Professional look and feel */
    .stButton>button { border-radius: 8px; border: 1px solid #404040; transition: all 0.2s ease; }
    .stButton>button:hover { border-color: #3b82f6; color: #3b82f6; }
    [data-testid="stSidebar"] { background-color: #111; border-right: 1px solid #222; padding: 1rem; }
    .stChatMessage { border-radius: 12px; border: 1px solid #222; background-color: #1a1a1a; margin-bottom: 1rem; }
    .stChatMessage:has(div[data-testid="stChatMessageContent.user"]) { background-color: #2563eb; color: white; }
    [data-testid="stChatInput"] { background-color: #111; }
    .stSelectbox div[data-baseweb="select"] > div {background-color: #222;}
    .stRadio div[role="radiogroup"] > label { background-color: #222; padding: 5px 10px; border-radius: 8px; margin: 0 5px; }
</style>
""", unsafe_allow_html=True)

# --- 3. CONFIGURATION & STATE MANAGEMENT ---
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user"

# Initialize Session State
if "messages" not in st.session_state: st.session_state.messages = []
if "conversations" not in st.session_state: st.session_state.conversations = []
if "current_conv_id" not in st.session_state: st.session_state.current_conv_id = None
if "current_title" not in st.session_state: st.session_state.current_title = "مکالمه جدید"
if "initialized" not in st.session_state: st.session_state.initialized = False

# --- 4. ASYNC DATABASE SETUP ---
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

# --- 5. AI CORE LOGIC (WITH YOUR DEFINITIVE MODEL LIST) ---
MODELS = {
    "چت متنی": {
        "Gemini 2.5 Pro (قدرتمندترین | RPM: 5, RPD: 100)": "gemini-1.5-pro-latest",
        "Gemini 2.5 Flash (سریع و جدید | RPM: 10, RPD: 250)": "gemini-1.5-flash-latest",
        "Gemini 2.5 Flash-Lite (بسیار بهینه | RPM: 15, RPD: 1,000)": "gemini-1.5-flash-latest",
        "Gemini 2.0 Flash (پایدار | RPM: 15, RPD: 200)": "gemini-1.0-pro",
        "Gemini 2.0 Flash-Lite (سهمیه بالا | RPM: 30, RPD: 200)": "gemini-1.0-pro-002",
    },
    "تولید تصویر": {
        "Gemini 2.0 Image Generation (RPM: 10, RPD: 100)": "imagen-placeholder"
    }
}

async def stream_gemini_response(api_msgs: List[Dict], model: str) -> AsyncGenerator[str, Any]:
    if not GEMINI_API_KEY:
        yield "**خطا: کلید API گوگل (GEMINI_API_KEY) در متغیرهای محیطی Render تنظیم نشده است.**"
        return
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        model_instance = genai.GenerativeModel(model)
        response_stream = await model_instance.generate_content_async(api_msgs, stream=True)
        async for chunk in response_stream:
            if chunk.text: yield chunk.text
    except Exception as e:
        yield f"**خطای API:** `{e}`"

async def generate_image(prompt: str) -> str:
    await asyncio.sleep(2)
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"

# --- 6. MAIN APPLICATION ---
# Sidebar UI
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

# Header UI
col1, col2 = st.columns([2, 3])
col1.header(st.session_state.current_title)
with col2:
    active_mode = st.radio("حالت:", list(MODELS.keys()), horizontal=True, label_visibility="collapsed")
    model_options = MODELS[active_mode]
    selected_model_name = st.selectbox("مدل:", list(model_options.keys()), label_visibility="collapsed")
    # Handle potential aliasing (e.g., Flash vs Flash-Lite pointing to same tech ID)
    technical_model_id = model_options[selected_model_name]

# Chat History UI
chat_container = st.container(height=500, border=False)
for msg in st.session_state.messages:
    with chat_container.chat_message(msg["role"], avatar="🧑‍💻" if msg["role"] == "user" else "🤖"):
        if msg.get("type") == "image":
            st.image(msg["content"], caption="تصویر تولید شده توسط هوش مصنوعی")
        else:
            st.markdown(msg["content"])

# --- Main Logic: Handle new input and generate response ---
if prompt := st.chat_input("پیام خود را بنویسید..."):
    conv_id = st.session_state.current_conv_id
    is_new_conv = not conv_id
    if is_new_conv:
        conv_id = str(uuid.uuid4())
        st.session_state.current_conv_id = conv_id
        st.session_state.current_title = prompt[:40]
        asyncio.run(db_create_conversation(conv_id, st.session_state.current_title))
    
    user_message = {"_id": str(uuid.uuid4()), "role": "user", "type": "text", "content": prompt}
    st.session_state.messages.append(user_message)
    asyncio.run(db_save_message(conv_id, user_message))
    
    if is_new_conv: # Reload conversations list in sidebar if new chat started
        st.session_state.conversations = asyncio.run(db_get_conversations())

    st.rerun()

# This part runs AFTER the user message has been displayed
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    last_prompt = st.session_state.messages[-1]["content"]
    conv_id = st.session_state.current_conv_id
    
    with chat_container.chat_message("assistant", avatar="🤖"):
        if active_mode == "چت متنی":
            # Filter for text messages for chat history
            text_history = [{"role": m["role"], "parts": [{"text": m["content"]}]} for m in st.session_state.messages if m['type'] == 'text']
            
            with st.spinner("در حال فکر کردن..."):
                response_generator = stream_gemini_response(text_history, technical_model_id)
                full_response = st.write_stream(response_generator)
            
            if full_response and "**خطا:**" not in full_response:
                ai_message = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "text", "content": full_response}
                st.session_state.messages.append(ai_message)
                asyncio.run(db_save_message(conv_id, ai_message))
                st.rerun()

        elif active_mode == "تولید تصویر":
            with st.spinner("در حال خلق اثر هنری شما..."):
                image_url = asyncio.run(generate_image(last_prompt))
                st.image(image_url)
                ai_message = {"_id": str(uuid.uuid4()),"role": "assistant", "type": "image", "content": image_url}
                st.session_state.messages.append(ai_message)
                asyncio.run(db_save_message(conv_id, ai_message))
                st.rerun()
