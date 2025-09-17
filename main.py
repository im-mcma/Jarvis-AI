import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Generator

import streamlit as st
import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# --- Initial Setup ---
# This command should be at the top and run only once.
st.set_page_config(
    page_title="Jarvis Elite Pro",
    page_icon="🤖",
    layout="wide"
)
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Environment Variables from Render Dashboard (NOT .env file) ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_ID = "main_user"

# --- Database Connection (Async support with asyncio) ---
@st.cache_resource
def get_db_client():
    logger.info("Initializing MongoDB client...")
    return AsyncIOMotorClient(MONGO_URI)

client = get_db_client()
db = client["chat_ai_db_streamlit"]
users_coll = db["users"]

# --- Database Functions (converted to async) ---
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

# --- AI Core Logic ---
MODELS = {
    "چت متنی": {
        "Gemma 3 (بیشترین سهمیه)": "gemma-2-9b-it",
        "Gemini 2.0 Flash-Lite (سهمیه بالا)": "gemini-1.0-pro-002",
        "Gemini 2.0 Flash (پایدار)": "gemini-1.0-pro",
        "Gemini 2.5 Flash / Lite (سریع و جدید)": "gemini-1.5-flash-latest",
        "Gemini 2.5 Pro (قدرتمندترین)": "gemini-1.5-pro-latest"
    },
    "تولید تصویر": {
        "Gemini Image Generation": "imagen-placeholder"
    }
}

async def stream_gemini_response(api_msgs: List[Dict], model: str) -> Generator[str, Any, None]:
    if not GEMINI_API_KEY:
        yield "کلید API گوگل تنظیم نشده است."
        return
        
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            async with client.stream("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={GEMINI_API_KEY}", json={"contents": api_msgs}) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    for line in chunk.decode('utf-8').splitlines():
                        if '"text":' in line:
                            try:
                                text = json.loads("{" + line.strip().rstrip(',') + "}").get("text", "")
                                yield text
                            except Exception: continue
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                yield f"**خطا:** سهمیه رایگان مدل «{model}» تمام شده. لطفاً از سایدبار یک مدل دیگر را انتخاب کنید."
            else:
                yield f"**خطای API گوگل:** {e.response.text}"
        except Exception as e:
            yield f"**خطای اتصال:** {e}"

async def generate_image(prompt: str) -> str:
    """Placeholder for a real image generation API."""
    await asyncio.sleep(3) # Simulate processing
    return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/1024"

# --- Streamlit UI Application ---

# Initialize session state to store data across reruns
if "messages" not in st.session_state:
    st.session_state.messages = []
if "conversations" not in st.session_state:
    st.session_state.conversations = []
if "current_conversation_id" not in st.session_state:
    st.session_state.current_conversation_id = None

async def load_conversations():
    st.session_state.conversations = await db_get_conversations()
    if not st.session_state.current_conversation_id and st.session_state.conversations:
        st.session_state.current_conversation_id = st.session_state.conversations[0]['_id']

# --- Sidebar UI ---
with st.sidebar:
    st.title("🤖 Jarvis Elite Pro")
    
    if st.button("مکالمه جدید", use_container_width=True):
        st.session_state.current_conversation_id = None
        st.session_state.messages = []
        st.rerun()

    st.markdown("---")
    
    # Load conversations on first run
    if not st.session_state.conversations:
        asyncio.run(load_conversations())

    for conv in st.session_state.conversations:
        if st.button(conv['title'], key=conv['_id'], use_container_width=True):
            st.session_state.current_conversation_id = conv['_id']
            st.session_state.messages = asyncio.run(db_get_messages(conv['_id']))
            st.rerun()

    st.markdown("---")
    active_mode = st.radio("حالت را انتخاب کنید:", list(MODELS.keys()), horizontal=True)
    
    model_options = MODELS[active_mode]
    selected_model_name = st.selectbox("مدل را انتخاب کنید:", list(model_options.keys()))
    selected_model_id = model_options[selected_model_name]

# --- Main Chat UI ---
st.header(f"مکالمه با Jarvis")

# Display previous messages from session state
for msg in st.session_state.messages:
    with st.chat_message("user" if msg['role'] == 'user' else "assistant"):
        if msg.get("type") == "image":
            st.image(msg["content"], caption="تصویر تولید شده")
        else:
            st.markdown(msg["content"])

# Handle new user input
if prompt := st.chat_input("پیام خود را اینجا وارد کنید..."):
    # Create conversation if it doesn't exist
    if not st.session_state.current_conversation_id:
        st.session_state.current_conversation_id = str(uuid.uuid4())
        asyncio.run(db_create_conversation(st.session_state.current_conversation_id, prompt))
        # Refresh sidebar to show the new conversation
        asyncio.run(load_conversations())

    # Save and display user message
    user_message = {"role": "user", "type": "text", "content": prompt}
    st.session_state.messages.append(user_message)
    asyncio.run(db_save_message(st.session_state.current_conversation_id, user_message))
    
    with st.chat_message("user"):
        st.markdown(prompt)

    # Process and display AI response
    with st.chat_message("assistant"):
        if active_mode == "چت متنی":
            # Get only text messages for chat history
            text_history = [{"role": m["role"], "parts": [{"text": m["content"]}]} for m in st.session_state.messages if m['type'] == 'text']
            
            # Use write_stream for a beautiful typing effect
            response_generator = stream_gemini_response(text_history, selected_model_id)
            full_response = st.write_stream(response_generator)
            
            ai_message = {"role": "assistant", "type": "text", "content": full_response}
            st.session_state.messages.append(ai_message)
            asyncio.run(db_save_message(st.session_state.current_conversation_id, ai_message))
            
        elif active_mode == "تولید تصویر":
            with st.spinner("در حال تولید تصویر..."):
                image_url = asyncio.run(generate_image(prompt))
                st.image(image_url, caption="تصویر تولید شده")
                
                ai_message = {"role": "assistant", "type": "image", "content": image_url}
                st.session_state.messages.append(ai_message)
                asyncio.run(db_save_message(st.session_state.current_conversation_id, ai_message))
    
    # Rerun to ensure everything is consistent
    st.rerun()
