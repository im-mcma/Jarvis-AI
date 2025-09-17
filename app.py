# --------------------------------------------------------------------------
# Jarvis Elite V4 - ارتقا یافته توسط Gemini
# تاریخ ارتقا: ۱۷ سپتامبر ۲۰۲۵
# --------------------------------------------------------------------------

import os
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, AsyncGenerator

import streamlit as st
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import google.generativeai as genai

# --- 1. تنظیمات اولیه و پیکربندی ---
st.set_page_config(
    page_title="Jarvis Elite V4",
    page_icon="👑",
    layout="wide",  # تغییر به حالت عریض برای فضای بیشتر
    initial_sidebar_state="expanded"
)
load_dotenv()

# --- 2. لاگ‌گیری پیشرفته ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- 3. کلاس مدیریت پیکربندی و کلیدها ---
class Config:
    """کلاسی برای نگهداری متمرکز تنظیمات و کلیدهای API."""
    def __init__(self):
        self.mongo_uri = os.getenv("MONGO_URI")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.user_id = "main_user"

        if not self.mongo_uri or not self.gemini_api_key:
            st.error("لطفاً کلیدهای API را در فایل .env تنظیم کنید. (MONGO_URI, GEMINI_API_KEY)")
            st.stop()
        
        genai.configure(api_key=self.gemini_api_key)

# --- 4. کلاس مدیریت پایگاه داده (MongoDB) ---
class DatabaseManager:
    """کلاسی برای تمام تعاملات با پایگاه داده MongoDB."""
    def __init__(self, mongo_uri: str, user_id: str):
        self._client = AsyncIOMotorClient(mongo_uri)
        self._db = self._client["jarvis_elite_v4"]
        self._users_coll = self._db["users"]
        self.user_id = user_id

    async def get_conversations(self) -> List[Dict]:
        """بازیابی لیست تمام مکالمات بدون پیام‌ها."""
        doc = await self._users_coll.find_one({"_id": self.user_id}, {"conversations.messages": 0})
        return sorted(doc.get("conversations", []), key=lambda c: c["created_at"], reverse=True) if doc else []

    async def get_messages(self, conv_id: str) -> List[Dict]:
        """بازیابی پیام‌های یک مکالمه خاص."""
        doc = await self._users_coll.find_one(
            {"_id": self.user_id, "conversations._id": conv_id},
            {"conversations.$": 1}
        )
        return doc["conversations"][0].get("messages", []) if doc and doc.get("conversations") else []

    async def save_message(self, conv_id: str, msg: Dict):
        """ذخیره یک پیام جدید در یک مکالمه."""
        await self._users_coll.update_one(
            {"_id": self.user_id, "conversations._id": conv_id},
            {"$push": {"conversations.$.messages": msg}}
        )

    async def create_conversation(self, title: str) -> str:
        """ایجاد یک مکالمه جدید و برگرداندن ID آن."""
        conv_id = str(uuid.uuid4())
        conv = {"_id": conv_id, "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
        await self._users_coll.update_one({"_id": self.user_id}, {"$push": {"conversations": conv}}, upsert=True)
        logger.info(f"مکالمه جدید با ID: {conv_id} ایجاد شد.")
        return conv_id

    async def delete_conversation(self, conv_id: str):
        """حذف یک مکالمه."""
        await self._users_coll.update_one({"_id": self.user_id}, {"$pull": {"conversations": {"_id": conv_id}}})
        logger.info(f"مکالمه با ID: {conv_id} حذف شد.")

    async def pop_last_message(self, conv_id: str):
        """حذف آخرین پیام از مکالمه (برای قابلیت Regenerate)."""
        await self._users_coll.update_one(
            {"_id": self.user_id, "conversations._id": conv_id},
            {"$pop": {"conversations.$.messages": 1}}
        )

# --- 5. کلاس مدیریت مدل هوش مصنوعی (Gemini) ---
class GeminiClient:
    """کلاسی برای تعامل با Gemini API با لیست مدل‌های به‌روز شده."""
    MODELS = {
        "چت متنی 💬": {
            "Gemini 2.5 Pro (RPM: 5 | روزانه: ۱۰۰)": "gemini-1.5-pro-latest",
            "Gemini 2.5 Flash (RPM: 10 | روزانه: ۲۵۰)": "gemini-1.5-flash-latest",
            "Gemini 2.5 Flash-Lite (RPM: 15 | روزانه: ۱,۰۰۰)": "gemini-1.5-flash-latest",
            "Gemini 2.0 Flash (RPM: 15 | روزانه: ۲۰۰)": "gemini-1.0-pro",
            "Gemini 2.0 Flash-Lite (RPM: 30 | روزانه: ۲۰۰)": "gemini-1.0-pro-001", # شناسه متفاوت برای تمایز
        },
        "تولید تصویر 🖼️": {
            "Gemini 2.0 Flash Preview (RPM: 10 | روزانه: ۱۰۰)": "imagen-preview-placeholder"
        }
    }

    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt

    def _prepare_api_messages(self, history: List[Dict]) -> List[Dict]:
        return [{"role": ("user" if msg["role"] == "user" else "model"), "parts": [{"text": msg["content"]}]} for msg in history if msg.get("type") != "image"]

    async def stream_chat_response(self, model_id: str, history: List[Dict], generation_config: Dict) -> AsyncGenerator[str, Any]:
        try:
            model = genai.GenerativeModel(model_name=model_id, system_instruction=self.system_prompt or None)
            api_messages = self._prepare_api_messages(history)
            response_stream = await model.generate_content_async(api_messages, stream=True, generation_config=generation_config)
            async for chunk in response_stream:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            logger.error(f"خطا در ارتباط با Gemini API: {e}")
            yield f"**خطای API:** `{str(e)}`"

    async def generate_image(self, prompt: str) -> str:
        logger.info(f"درخواست تولید تصویر برای: {prompt}")
        await asyncio.sleep(2)
        # نکته مهم: این یک Placeholder است. برای تولید تصویر واقعی، باید از APIهایی
        # مانند DALL-E 3, Stable Diffusion یا Vertex AI Imagen استفاده کرده
        # و کد زیر را با فراخوانی آن API جایگزین کنید.
        return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/768"


# --- 6. کلاس مدیریت رابط کاربری (Streamlit) ---
class UIManager:
    def __init__(self, config: Config, db_manager: DatabaseManager):
        self.config = config
        self.db = db_manager
        self._initialize_session_state()

    def _initialize_session_state(self):
        defaults = {
            "messages": [], "conversations": [], "current_conv_id": None,
            "current_title": "مکالمه جدید",
            "system_prompt": "You are Jarvis, a powerful and helpful AI assistant.",
            "temperature": 0.7, "top_p": 1.0, "processing": False,
        }
        for key, value in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = value

    def _render_css(self):
        st.markdown("""
        <style>
            .stButton>button { border-radius: 8px; }
            [data-testid="stSidebar"] { background-color: #09090b; border-right: 1px solid rgba(255, 255, 255, 0.1); }
            .stChatMessage { border-radius: 12px; border: 1px solid rgba(255, 255, 255, 0.1); }
            /* استایل دکمه حذف در سایدبار */
            div[data-testid*="stHorizontalBlock"] > div:nth-child(2) > div > button {
                border-color: #ff4b4b; color: #ff4b4b;
            }
            div[data-testid*="stHorizontalBlock"] > div:nth-child(2) > div > button:hover {
                border-color: white; background-color: #ff4b4b; color: white;
            }
        </style>
        """, unsafe_allow_html=True)

    async def _render_sidebar(self):
        with st.sidebar:
            st.title("👑 Jarvis Elite V4")
            if st.button("➕ مکالمه جدید", use_container_width=True, type="primary"):
                st.session_state.update({"current_conv_id": None, "messages": [], "current_title": "مکالمه جدید"})
                st.rerun()

            st.markdown("---")
            st.subheader("تاریخچه مکالمات")

            if not st.session_state.conversations:
                st.session_state.conversations = await self.db.get_conversations()

            if not st.session_state.conversations:
                st.info("هنوز مکالمه‌ای شروع نشده است.")

            for conv in st.session_state.conversations:
                col1, col2 = st.columns([4, 1])
                is_active = conv['_id'] == st.session_state.current_conv_id
                btn_type = "primary" if is_active else "secondary"
                
                if col1.button(f"📜 {conv['title'][:25]}...", key=f"load_{conv['_id']}", use_container_width=True, type=btn_type):
                    st.session_state.update({
                        "current_conv_id": conv['_id'], "current_title": conv['title'],
                        "messages": await self.db.get_messages(conv['_id'])
                    })
                    st.rerun()

                if col2.button("🗑️", key=f"del_{conv['_id']}", help="حذف مکالمه"):
                    await self.db.delete_conversation(conv['_id'])
                    st.session_state.conversations = [] # Force reload
                    if st.session_state.current_conv_id == conv['_id']:
                        st.session_state.update({"current_conv_id": None, "messages": []})
                    st.toast(f"مکالمه '{conv['title']}' حذف شد.")
                    await asyncio.sleep(1)
                    st.rerun()
            
            st.markdown("---")
            with st.expander("⚙️ تنظیمات پیشرفته"):
                st.text_area("دستور سیستمی (System Prompt)", key="system_prompt", height=150)
                st.slider("دما (Temperature)", 0.0, 2.0, key="temperature", help="بالاتر = خلاق‌تر، پایین‌تر = دقیق‌تر")
                st.slider("Top-P", 0.0, 1.0, key="top_p")

    def _render_header(self):
        col1, col2 = st.columns([3, 2])
        col1.header(st.session_state.current_title)
        with col2:
            active_mode = st.radio("حالت:", GeminiClient.MODELS.keys(), horizontal=True, label_visibility="collapsed")
            model_options = GeminiClient.MODELS[active_mode]
            selected_model_name = st.selectbox("مدل:", model_options.keys(), label_visibility="collapsed")
            return active_mode, model_options[selected_model_name]

    async def _render_chat_history(self):
        for i, msg in enumerate(st.session_state.messages):
            avatar = "🧑‍💻" if msg["role"] == "user" else "🤖"
            with st.chat_message(msg["role"], avatar=avatar):
                if msg.get("type") == "image":
                    st.image(msg["content"], caption=msg.get("prompt", ""))
                else:
                    st.markdown(msg["content"])

                # قابلیت تولید مجدد فقط برای آخرین پیام دستیار
                if msg["role"] == "assistant" and i == len(st.session_state.messages) - 1:
                    if st.button("🔄 تولید مجدد", key=f"regen_{i}"):
                        st.session_state.processing = True
                        # حذف پیام کاربر قبلی و پاسخ دستیار از دیتابیس
                        await self.db.pop_last_message(st.session_state.current_conv_id)
                        await self.db.pop_last_message(st.session_state.current_conv_id)
                        # حذف از state و rerun برای تولید مجدد
                        st.session_state.messages = st.session_state.messages[:-2]
                        st.rerun()

    async def _handle_user_input(self):
        if prompt := st.chat_input("پیام خود را بنویسید...", disabled=st.session_state.processing):
            st.session_state.processing = True
            
            if not st.session_state.current_conv_id:
                title = prompt[:50]
                conv_id = await self.db.create_conversation(title)
                st.session_state.update({"current_conv_id": conv_id, "current_title": title, "conversations": []})
            
            user_msg = {"role": "user", "content": prompt}
            st.session_state.messages.append(user_msg)
            await self.db.save_message(st.session_state.current_conv_id, user_msg)
            st.rerun()

    async def _process_response(self, active_mode: str, selected_model_id: str):
        if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
            st.session_state.processing = True
            with st.chat_message("assistant", avatar="🤖"):
                gemini_client = GeminiClient(st.session_state.system_prompt)
                ai_msg = {}
                
                if active_mode == "تولید تصویر 🖼️":
                    with st.spinner("درحال تولید تصویر... 🎨"):
                        prompt = st.session_state.messages[-1]["content"]
                        image_url = await gemini_client.generate_image(prompt)
                        st.image(image_url, caption=prompt)
                        ai_msg = {"role": "assistant", "type": "image", "content": image_url, "prompt": prompt}
                else: # حالت چت متنی
                    placeholder = st.empty()
                    full_response = ""
                    gen_config = {"temperature": st.session_state.temperature, "top_p": st.session_state.top_p}
                    history = st.session_state.messages
                    
                    try:
                        async for chunk in gemini_client.stream_chat_response(selected_model_id, history, gen_config):
                            full_response += chunk
                            placeholder.markdown(full_response + "▌")
                        placeholder.markdown(full_response)
                        ai_msg = {"role": "assistant", "content": full_response}
                    except Exception as e:
                        full_response = f"متاسفانه خطایی رخ داد: {e}"
                        placeholder.error(full_response)
                        ai_msg = {"role": "assistant", "content": full_response}

                st.session_state.messages.append(ai_msg)
                await self.db.save_message(st.session_state.current_conv_id, ai_msg)
                st.session_state.processing = False
                st.rerun() # برای نمایش دکمه "تولید مجدد"
        elif st.session_state.processing and not (st.session_state.messages and st.session_state.messages[-1]["role"] == "user"):
            # اگر پردازش تمام شده بود، state را false کن
            st.session_state.processing = False


    async def render(self):
        self._render_css()
        await self._render_sidebar()
        
        main_container = st.container()
        with main_container:
            active_mode, selected_model_id = self._render_header()
            await self._render_chat_history()
            await self._handle_user_input()
            # این تابع تنها زمانی اجرا می‌شود که آخرین پیام از طرف کاربر باشد
            await self._process_response(active_mode, selected_model_id)


# --- 7. نقطه شروع برنامه با مدیریت صحیح Event Loop ---
def get_or_create_eventloop():
    """رفع مشکل Event loop is closed در Streamlit."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

async def main():
    try:
        config = Config()
        @st.cache_resource
        def get_db_manager():
            return DatabaseManager(config.mongo_uri, config.user_id)
            
        db_manager = get_db_manager()
        ui_manager = UIManager(config, db_manager)
        await ui_manager.render()
    except Exception as e:
        logger.error(f"یک خطای بحرانی در برنامه رخ داد: {e}")
        st.error(f"یک خطای غیرمنتظره رخ داد: {e}")

if __name__ == "__main__":
    loop = get_or_create_eventloop()
    loop.run_until_complete(main())
