import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Generator, Any

import streamlit as st
import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# --- 1. PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Jarvis Elite - Definitive Edition",
    page_icon="👑",
    layout="centered",
    initial_sidebar_state="expanded"
)
load_dotenv()

# --- 2. CUSTOM CSS FOR A PREMIUM UI ---
def load_assets():
    st.markdown("""
    <style>
        .stButton>button { border-radius: 10px; border: 1px solid #404040; transition: all 0.2s ease; }
        .stButton>button:hover { border-color: #3b82f6; color: #3b82f6; }
        [data-testid="stSidebar"] { background-color: #111; border-right: 1px solid #222; }
        .stChatMessage { border-radius: 12px; border: 1px solid #222; background-color: #1a1a1a; }
        .stChatMessage:has(div[data-testid="stChatMessageContent.user"]) { background-color: #2563eb; color: white; }
    </style>
    """, unsafe_allow_html=True)

load_assets()

# --- 3. CONFIGURATION & STATE MANAGEMENT ---
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user"

# Session State Initialization
if "messages" not in st.session_state: st.session_state.messages = []
if "conversations" not in st.session_state: st.session_state.conversations = []
if "current_conv_id" not in st.session_state: st.session_state.current_conv_id = None
if "current_title" not in st.session_state: st.session_state.current_title = "مکالمه جدید"
if "notepad_content" not in st.session_state: st.session_state.notepad_content = "# یادداشت‌های من\n\n"

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

async def db_save_notepad(content: str):
    await users_coll.update_one({"_id": USER_ID}, {"$set": {"notepad": content}}, upsert=True)

async def db_load_notepad() -> str:
    doc = await users_coll.find_one({"_id": USER_ID}, {"notepad": 1})
    return doc.get("notepad", "# یادداشت‌های من\n\n")

# --- 5. AI CORE LOGIC (WITH DEFINITIVE MODEL LIST) ---
MODELS = {
    "چت متنی": {
        "Gemini 2.5 Pro (قدرتمندترین | ۵ درخواست در دقیقه | ۱۰۰ در روز)": "gemini-1.5-pro-latest",
        "Gemini 2.5 Flash (سریع | ۱۰ درخواست در دقیقه | ۲۵۰ در روز)": "gemini-1.5-flash-latest",
        "Gemini 2.5 Flash-Lite (بهینه | ۱۵ درخواست در دقیقه | ۱۰۰۰ در روز)": "gemini-1.5-flash-latest",
        "Gemini 2.0 Flash (پایدار | ۱۵ درخواست در دقیقه | ۲۰۰ در روز)": "gemini-1.0-pro",
        "Gemini 2.0 Flash-Lite (سهمیه بالا | ۳۰ درخواست در دقیقه | ۲۰۰ در روز)": "gemini-1.0-pro-002",
    },
    "تولید تصویر": {
        "Gemini 2.0 Image Generation (۱۰ درخواست در دقیقه | ۱۰۰ در روز)": "imagen-placeholder"
    }
}

async def stream_gemini_response(api_msgs: List[Dict], model: str) -> Generator[str, Any, None]:
    if not GEMINI_API_KEY:
        yield "**خطا: کلید API گوگل تنظیم نشده است.**"
        return
        
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            async with client.stream("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}", json={"contents": api_msgs}) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    for line in chunk.decode('utf-8').splitlines():
                        if '"text":' in line:
                            try:
                                yield json.loads("{" + line.strip().rstrip(',') + "}").get("text", "")
                            except Exception: continue
        except httpx.HTTPStatusError as e:
            yield f"**خطا:** سهمیه رایگان تمام شده یا خطایی در API رخ داده است.\n\n`{e.response.text}`"
        except Exception as e:
            yield f"**خطای اتصال:** `{str(e)}`"

async def generate_image(prompt: str) -> str:
    await asyncio.sleep(3)
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/1024"

# --- 6. SIDEBAR UI ---
def sidebar_ui():
    with st.sidebar:
        st.title("👑 Jarvis Elite Definitive")
        
        if st.button("➕ مکالمه جدید", use_container_width=True):
            st.session_state.current_conv_id = None
            st.session_state.messages = []
            st.session_state.current_title = "مکالمه جدید"
            st.rerun()

        st.markdown("---")

        # دفترچه یادداشت
        with st.expander("📝 دفترچه یادداشت"):
            st.session_state.notepad_content = st.text_area(
                "یادداشت‌ها:",
                value=st.session_state.notepad_content,
                height=200,
                key="notepad",
                on_change=lambda: asyncio.run(db_save_notepad(st.session_state.notepad))
            )

        with st.expander("⚙️ تنظیمات"):
            st.session_state.save_mode = st.toggle("حالت ذخیره انرژی", value=False)
            st.session_state.system_prompt = st.selectbox("شخصیت AI:", list(MODELS["چت متنی"].keys()))

        st.markdown("---")
        st.markdown("**تاریخچه مکالمات**")

        if not st.session_state.conversations:
            st.session_state.conversations = asyncio.run(db_get_conversations())

        for conv in st.session_state.conversations:
            if st.button(conv['title'], key=conv['_id'], use_container_width=True, type="secondary"):
                st.session_state.current_conv_id = conv['_id']
                st.session_state.current_title = conv['title']
                st.session_state.messages = asyncio.run(db_get_messages(conv['_id']))
                st.rerun()

# --- 7. MAIN APP ---
sidebar_ui()

col1, col2 = st.columns([3, 2])
col1.header(st.session_state.current_title)
with col2:
    active_mode = st.radio("حالت:", list(MODELS.keys()), horizontal=True, label_visibility="collapsed")
    model_options = MODELS[active_mode]
    selected_model_name = st.selectbox("مدل:", list(model_options.keys()), label_visibility="collapsed")
    selected_model_id = model_options[selected_model_name]

chat_container = st.container(height=500, border=False)
for msg in st.session_state.messages:
    with chat_container.chat_message(msg["role"], avatar="🧑‍💻" if msg["role"] == "user" else "🤖"):
        if msg.get("type") == "image": st.image(msg["content"])
        else: st.markdown(msg["content"])

# پیام جدید کاربر
if prompt := st.chat_input("پیام خود را بنویسید..."):
    conv_id = st.session_state.current_conv_id
    if not conv_id:
        conv_id = str(uuid.uuid4())
        st.session_state.current_conv_id = conv_id
        st.session_state.current_title = prompt[:50]
        asyncio.run(db_create_conversation(conv_id, prompt[:50]))
        st.session_state.conversations = []  # Force reload

    user_message = {"_id": str(uuid.uuid4()), "role": "user", "type": "text", "content": prompt}
    st.session_state.messages.append(user_message)
    asyncio.run(db_save_message(conv_id, user_message))
    st.rerun()

# پاسخ AI
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    last_prompt = st.session_state.messages[-1]["content"]
    conv_id = st.session_state.current_conv_id
    
    with chat_container.chat_message("assistant", avatar="🤖"):
        if active_mode == "چت متنی":
            text_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in st.session_state.messages if m['type'] == 'text']
            response_generator = stream_gemini_response(text_history, selected_model_id)
            full_response = st.write_stream(response_generator)
            
            if "خطا:" not in full_response:
                ai_message = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "text", "content": full_response}
                st.session_state.messages.append(ai_message)
                asyncio.run(db_save_message(conv_id, ai_message))
                st.rerun()

        elif active_mode == "تولید تصویر":
            with st.spinner("در حال خلق اثر هنری..."):
                image_url = asyncio.run(generate_image(last_prompt))
                st.image(image_url)
                ai_message = {"_id": str(uuid.uuid4()), "role": "assistant", "type": "image", "content": image_url}
                st.session_state.messages.append(ai_message)
                asyncio.run(db_save_message(conv_id, ai_message))
                st.rerun()
