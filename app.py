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
    page_title="Jarvis Elite - Professional Edition",
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 2. CUSTOM CSS INJECTION FOR PROFESSIONAL UI ---
def load_css():
    st.markdown("""
    <style>
        /* General */
        body { font-family: 'Vazirmatn', sans-serif; }
        .stButton>button { border-radius: 10px; border: 1px solid #404040; transition: all 0.2s ease-in-out; }
        .stButton>button:hover { border-color: #3b82f6; color: #3b82f6; }
        
        /* Sidebar */
        [data-testid="stSidebar"] { background-color: #171717; }
        .stRadio [role="radiogroup"] { flex-direction: row; justify-content: space-around; }
        
        /* Chat bubbles */
        .stChatMessage { border-radius: 12px; border: 1px solid #262626; background-color: #262626; }
        .stChatMessage:has(div[data-testid="stChatMessageContent.user"]) { background-color: #3b82f6; color: white; }
        .stChatMessage .stMarkdown { text-align: right; }
        
        /* Scrollbar */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #525252; border-radius: 3px; }
    </style>
    """, unsafe_allow_html=True)

load_css()
load_dotenv()

# --- 3. CONFIGURATION & ENVIRONMENT VARIABLES ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user"

# --- 4. ASYNC DATABASE SETUP ---
@st.cache_resource
def get_db_client():
    return AsyncIOMotorClient(MONGO_URI)

client = get_db_client()
db = client["chat_ai_db_pro"]
users_coll = db["users"]

# Database functions remain the same as the previous correct version...
async def db_create_conversation(conv_id: str, title: str): # ...
async def db_get_conversations() -> List[Dict]: # ...
async def db_get_messages(conv_id: str) -> List[Dict]: # ...
async def db_save_message(conv_id: str, msg: Dict): # ...

# --- 5. AI CORE LOGIC ---
MODELS = {
    "چت متنی": {"Gemma 3": "gemma-2-9b-it", "Gemini 2.5 Flash": "gemini-1.5-flash-latest", "Gemini 2.5 Pro": "gemini-1.5-pro-latest"},
    "تولید تصویر": {"Imagen Pro": "imagen-placeholder"}
}

async def stream_gemini_response(api_msgs: List[Dict], model: str) -> Generator[str, Any, None]: # ...
async def generate_image(prompt: str) -> str: # ...

# --- 6. SESSION STATE INITIALIZATION ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "conversations" not in st.session_state:
    st.session_state.conversations = []
if "current_conversation_id" not in st.session_state:
    st.session_state.current_conversation_id = None
if "current_title" not in st.session_state:
    st.session_state.current_title = "مکالمه جدید"

# --- 7. SIDEBAR UI ---
with st.sidebar:
    st.title("✨ Jarvis Elite Pro")
    if st.button("➕ مکالمه جدید", use_container_width=True):
        st.session_state.current_conversation_id = None
        st.session_state.messages = []
        st.session_state.current_title = "مکالمه جدید"
        st.rerun()

    st.markdown("---")
    st.markdown("**تاریخچه مکالمات**")

    # Load conversations on first run or when they are empty
    if not st.session_state.conversations:
        st.session_state.conversations = asyncio.run(db_get_conversations())
    
    # Use a container for a cleaner look
    conv_container = st.container(height=400)
    for conv in st.session_state.conversations:
        if conv_container.button(conv['title'], key=conv['_id'], use_container_width=True):
            st.session_state.current_conversation_id = conv['_id']
            st.session_state.current_title = conv['title']
            st.session_state.messages = asyncio.run(db_get_messages(conv['_id']))
            st.rerun()

# --- 8. MAIN PAGE UI ---

# Header with mode and model selection
col1, col2 = st.columns([3, 2])
with col1:
    st.header(st.session_state.current_title)
with col2:
    active_mode = st.radio("حالت:", list(MODELS.keys()), horizontal=True, label_visibility="collapsed")
    model_options = MODELS[active_mode]
    selected_model_name = st.selectbox("مدل:", list(model_options.keys()), label_visibility="collapsed")
    selected_model_id = model_options[selected_model_name]

st.markdown("---")

# Chat history container
chat_container = st.container(height=500, border=False)
for msg in st.session_state.messages:
    with chat_container.chat_message("user" if msg['role'] == 'user' else "assistant"):
        if msg.get("type") == "image":
            st.image(msg["content"], caption="Image generated")
        else:
            st.markdown(msg["content"])

# Handle new user input at the bottom
if prompt := st.chat_input("پیام خود را بنویسید..."):
    conv_id = st.session_state.current_conversation_id
    if not conv_id:
        conv_id = str(uuid.uuid4())
        st.session_state.current_conversation_id = conv_id
        st.session_state.current_title = prompt[:50]
        asyncio.run(db_create_conversation(conv_id, prompt[:50]))
        st.session_state.conversations = asyncio.run(db_get_conversations())

    user_message = {"role": "user", "type": "text", "content": prompt}
    st.session_state.messages.append(user_message)
    asyncio.run(db_save_message(conv_id, user_message))
    
    # Rerun to show the user's message immediately
    st.rerun()

# This part runs AFTER the user has submitted a prompt and the page reruns
# Check if the last message was from the user to generate a response
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    last_prompt = st.session_state.messages[-1]["content"]
    conv_id = st.session_state.current_conversation_id

    with chat_container.chat_message("assistant"):
        if active_mode == "چت متنی":
            text_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in st.session_state.messages if m['type'] == 'text']
            
            response_generator = stream_gemini_response(text_history, selected_model_id)
            full_response = st.wr
