# --------------------------------------------------------------------------
# Jarvis Elite V3 - ارتقا یافته توسط Gemini
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
    page_title="Jarvis Elite V3",
    page_icon="👑",
    layout="centered",
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
        self.user_id = "main_user" # در آینده می‌توان این را داینامیک کرد

        if not self.mongo_uri or not self.gemini_api_key:
            st.error("لطفاً کلیدهای API را در فایل .env تنظیم کنید. (MONGO_URI, GEMINI_API_KEY)")
            st.stop()
        
        # پیکربندی SDK گوگل
        genai.configure(api_key=self.gemini_api_key)

# --- 4. کلاس مدیریت پایگاه داده (MongoDB) ---
class DatabaseManager:
    """کلاسی برای تمام تعاملات با پایگاه داده MongoDB."""
    def __init__(self, mongo_uri: str, user_id: str):
        self._client = AsyncIOMotorClient(mongo_uri)
        self._db = self._client["jarvis_elite_v3"]
        self._users_coll = self._db["users"]
        self.user_id = user_id

    async def get_conversations(self) -> List[Dict]:
        """بازیابی لیست تمام مکالمات بدون پیام‌ها."""
        doc = await self._users_coll.find_one(
            {"_id": self.user_id},
            {"conversations.messages": 0}
        )
        if not doc or "conversations" not in doc:
            return []
        return sorted(doc["conversations"], key=lambda c: c["created_at"], reverse=True)

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
        conv = {
            "_id": conv_id,
            "title": title,
            "created_at": datetime.now(timezone.utc),
            "messages": []
        }
        await self._users_coll.update_one(
            {"_id": self.user_id},
            {"$push": {"conversations": conv}},
            upsert=True
        )
        logger.info(f"مکالمه جدید با ID: {conv_id} ایجاد شد.")
        return conv_id

    async def delete_conversation(self, conv_id: str):
        """حذف یک مکالمه."""
        await self._users_coll.update_one(
            {"_id": self.user_id},
            {"$pull": {"conversations": {"_id": conv_id}}}
        )
        logger.info(f"مکالمه با ID: {conv_id} حذف شد.")


# --- 5. کلاس مدیریت مدل هوش مصنوعی (Gemini) ---
class GeminiClient:
    """کلاسی برای تعامل با Gemini API."""
    MODELS = {
        "چت متنی 💬": {
            "Gemini 1.5 Pro (قدرتمند)": "gemini-1.5-pro-latest",
            "Gemini 1.5 Flash (سریع)": "gemini-1.5-flash-latest",
        },
        "تولید تصویر 🖼️": {
            "Imagen 2 (به زودی)": "imagen-placeholder"
        }
    }

    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt

    def _prepare_api_messages(self, history: List[Dict]) -> List[Dict]:
        """تبدیل تاریخچه چت به فرمت مورد نیاز API."""
        api_messages = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            api_messages.append({"role": role, "parts": [{"text": msg["content"]}]})
        return api_messages

    async def stream_chat_response(
        self,
        model_id: str,
        history: List[Dict],
        generation_config: Dict
    ) -> AsyncGenerator[str, Any]:
        """ارسال درخواست به Gemini و دریافت پاسخ به صورت استریم."""
        try:
            model = genai.GenerativeModel(
                model_name=model_id,
                system_instruction=self.system_prompt if self.system_prompt else None
            )
            api_messages = self._prepare_api_messages(history)
            
            response_stream = await model.generate_content_async(
                api_messages,
                stream=True,
                generation_config=generation_config
            )
            async for chunk in response_stream:
                if chunk.text:
                    yield chunk.text

        except Exception as e:
            logger.error(f"خطا در ارتباط با Gemini API: {e}")
            yield f"**خطای API:** `{str(e)}`"

    async def generate_image(self, prompt: str) -> str:
        """تولید تصویر (در حال حاضر یک Placeholder است)."""
        logger.info(f"درخواست تولید تصویر برای: {prompt}")
        await asyncio.sleep(2) # شبیه‌سازی تاخیر شبکه
        # این یک placeholder است. در آینده با API واقعی جایگزین شود.
        # 
        return f"https://picsum.photos/seed/{uuid.uuid4().hex[:10]}/1024/1024"


# --- 6. کلاس مدیریت رابط کاربری (Streamlit) ---
class UIManager:
    """کلاسی برای رندر کردن تمام اجزای UI و مدیریت تعاملات کاربر."""
    def __init__(self, config: Config, db_manager: DatabaseManager):
        self.config = config
        self.db = db_manager
        self._initialize_session_state()

    def _initialize_session_state(self):
        """مقداردهی اولیه تمام متغیرهای مورد نیاز در session state."""
        defaults = {
            "messages": [],
            "conversations": [],
            "current_conv_id": None,
            "current_title": "مکالمه جدید",
            "system_prompt": "You are Jarvis, a powerful and helpful AI assistant.",
            "temperature": 0.7,
            "top_p": 1.0,
        }
        for key, value in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = value

    def _render_css(self):
        """اعمال استایل‌های سفارشی CSS."""
        st.markdown("""
        <style>
            .stButton>button { border-radius: 8px; font-weight: bold; transition: all 0.2s ease-in-out; }
            .stButton>button:hover { transform: scale(1.05); }
            .stChatMessage { border-radius: 12px; border: 1px solid rgba(255, 255, 255, 0.1); }
            [data-testid="stSidebar"] { background-color: #09090b; border-right: 1px solid rgba(255, 255, 255, 0.1); }
            .sidebar-conv-button {
                display: block; width: 100%; text-align: left;
                background-color: transparent; border: none; padding: 8px;
                border-radius: 8px; margin-bottom: 5px; color: #FAFAFA;
                transition: background-color 0.2s;
            }
            .sidebar-conv-button:hover { background-color: rgba(255, 255, 255, 0.1); }
            .active-conv { background-color: #2563eb; }
        </style>
        """, unsafe_allow_html=True)
    
    async def _render_sidebar(self):
        """رندر کردن سایدبار، شامل لیست مکالمات و تنظیمات."""
        with st.sidebar:
            st.title("👑 Jarvis Elite V3")
            st.markdown("---")
            
            if st.button("➕ مکالمه جدید", use_container_width=True):
                st.session_state.current_conv_id = None
                st.session_state.messages = []
                st.session_state.current_title = "مکالمه جدید"
                st.rerun()

            st.markdown("---")
            st.subheader("تاریخچه مکالمات")

            # بارگذاری مکالمات یک بار و کش کردن آن
            if not st.session_state.conversations:
                st.session_state.conversations = await self.db.get_conversations()

            if not st.session_state.conversations:
                st.info("هنوز مکالمه‌ای شروع نشده است.")

            for conv in st.session_state.conversations:
                col1, col2 = st.columns([4, 1])
                conv_title = conv['title'][:30] + "..." if len(conv['title']) > 30 else conv['title']
                
                with col1:
                    is_active = conv['_id'] == st.session_state.current_conv_id
                    if st.button(f"📜 {conv_title}", key=f"load_{conv['_id']}", use_container_width=True):
                        st.session_state.current_conv_id = conv['_id']
                        st.session_state.current_title = conv['title']
                        st.session_state.messages = await self.db.get_messages(conv['_id'])
                        st.rerun()
                
                with col2:
                    if st.button("🗑️", key=f"del_{conv['_id']}", help="حذف مکالمه"):
                        await self.db.delete_conversation(conv['_id'])
                        st.session_state.conversations = [] # برای بارگذاری مجدد
                        if st.session_state.current_conv_id == conv['_id']:
                            st.session_state.current_conv_id = None
                            st.session_state.messages = []
                        st.toast(f"مکالمه '{conv['title']}' حذف شد.")
                        await asyncio.sleep(1) # زمان برای دیدن toast
                        st.rerun()
            
            st.markdown("---")
            with st.expander("⚙️ تنظیمات پیشرفته"):
                st.text_area("دستور سیستمی (System Prompt)", key="system_prompt", height=150)
                st.slider("دما (Temperature)", min_value=0.0, max_value=2.0, step=0.1, key="temperature",
                          help="مقادیر بالاتر خلاقیت بیشتر و مقادیر پایین‌تر دقت بیشتری به همراه دارند.")
                st.slider("Top-P", min_value=0.0, max_value=1.0, step=0.05, key="top_p")


    def _render_header(self):
        """رندر کردن هدر اصلی صفحه."""
        col1, col2 = st.columns([3, 2])
        with col1:
            st.header(st.session_state.current_title)
        with col2:
            active_mode = st.radio("حالت:", list(GeminiClient.MODELS.keys()), horizontal=True, label_visibility="collapsed")
            model_options = GeminiClient.MODELS[active_mode]
            selected_model_name = st.selectbox("مدل:", list(model_options.keys()), label_visibility="collapsed")
            return active_mode, model_options[selected_model_name]

    def _render_chat_history(self):
        """رندر کردن پیام‌های تاریخچه چت."""
        chat_container = st.container(height=500, border=False)
        for msg in st.session_state.messages:
            avatar = "🧑‍💻" if msg["role"] == "user" else "🤖"
            with chat_container.chat_message(msg["role"], avatar=avatar):
                if msg.get("type") == "image":
                    st.image(msg["content"], caption=msg.get("prompt", ""))
                else:
                    st.markdown(msg["content"])
                    
    async def _handle_user_input(self, active_mode: str, selected_model_id: str):
        """پردازش ورودی کاربر و تولید پاسخ."""
        if prompt := st.chat_input("پیام خود را بنویسید..."):
            # 1. اگر مکالمه جدید است، آن را در دیتابیس ایجاد کن
            if not st.session_state.current_conv_id:
                title = prompt[:50]
                with st.spinner("ایجاد مکالمه جدید..."):
                    conv_id = await self.db.create_conversation(title)
                st.session_state.current_conv_id = conv_id
                st.session_state.current_title = title
                st.session_state.conversations = [] # برای بارگذاری مجدد لیست در سایدبار
                st.rerun()

            conv_id = st.session_state.current_conv_id

            # 2. پیام کاربر را به UI و دیتابیس اضافه کن
            user_msg = {"role": "user", "content": prompt}
            st.session_state.messages.append(user_msg)
            await self.db.save_message(conv_id, user_msg)
            
            # 3. نمایش پیام کاربر و آماده‌سازی برای پاسخ AI
            st.rerun()

    async def _process_response(self, active_mode: str, selected_model_id: str):
        """بررسی آخرین پیام و در صورت نیاز، تولید و نمایش پاسخ AI."""
        # تنها زمانی پاسخ تولید کن که آخرین پیام از طرف کاربر باشد
        if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
            conv_id = st.session_state.current_conv_id
            
            with st.chat_message("assistant", avatar="🤖"):
                gemini_client = GeminiClient(st.session_state.system_prompt)
                
                if active_mode == "تولید تصویر 🖼️":
                    with st.spinner("درحال تولید تصویر... 🎨"):
                        prompt = st.session_state.messages[-1]["content"]
                        image_url = await gemini_client.generate_image(prompt)
                        st.image(image_url, caption=prompt)
                        ai_msg = {"role": "assistant", "type": "image", "content": image_url, "prompt": prompt}
                else: # حالت چت متنی
                    placeholder = st.empty()
                    full_response = ""
                    generation_config = {
                        "temperature": st.session_state.temperature,
                        "top_p": st.session_state.top_p
                    }
                    history = st.session_state.messages
                    
                    try:
                        async for chunk in gemini_client.stream_chat_response(selected_model_id, history, generation_config):
                            full_response += chunk
                            placeholder.markdown(full_response + "▌")
                        placeholder.markdown(full_response)
                        ai_msg = {"role": "assistant", "content": full_response}
                    except Exception as e:
                        full_response = f"متاسفانه خطایی رخ داد: {e}"
                        placeholder.error(full_response)
                        ai_msg = {"role": "assistant", "content": full_response}
                
                # 4. ذخیره پاسخ AI در دیتابیس و session state
                st.session_state.messages.append(ai_msg)
                await self.db.save_message(conv_id, ai_msg)
    
    async def render(self):
        """متد اصلی برای رندر کردن کل صفحه."""
        self._render_css()
        await self._render_sidebar()
        active_mode, selected_model_id = self._render_header()
        self._render_chat_history()
        
        # این بخش‌ها به صورت جداگانه اجرا می‌شوند تا UI سریع‌تر پاسخ دهد
        await self._handle_user_input(active_mode, selected_model_id)
        await self._process_response(active_mode, selected_model_id)

# --- 7. نقطه شروع برنامه ---
async def main():
    """تابع اصلی برنامه که به صورت async اجرا می‌شود."""
    st.title(" ") # برای ایجاد فضای خالی در بالای صفحه
    try:
        config = Config()
        
        # @st.cache_resource برای جلوگیری از ساخت مجدد در هر بار rerun
        @st.cache_resource
        def get_db_manager():
            return DatabaseManager(config.mongo_uri, config.user_id)
            
        db_manager = get_db_manager()
        ui_manager = UIManager(config, db_manager)
        await ui_manager.render()

    except Exception as e:
        logger.error(f"یک خطای بحرانی در برنامه رخ داد: {e}")
        st.error(f"یک خطای غیرمنتظره رخ داد. لطفاً صفحه را رفرش کنید. جزئیات: {e}")


if __name__ == "__main__":
    # اجرای برنامه در یک event loop برای مدیریت صحیح async/await
    asyncio.run(main())
