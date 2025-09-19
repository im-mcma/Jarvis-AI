import os
import io
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import chainlit as cl
import bcrypt
import jwt
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
import google.generativeai as genai
from dotenv import load_dotenv
from PIL import Image

# ------------------------------------------------------------
# 1. Standard Setup & Configuration
# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("jarvis-argus-pro")
load_dotenv()

# --- Environment Variables ---
MONGO_URI = os.environ.get("MONGO_URI")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "a-secure-default-secret-for-development")
APP_TITLE = "Jarvis Argus — نسخه حرفه‌ای"

if not MONGO_URI or not GEMINI_API_KEY:
    raise ConnectionError("MONGO_URI and GEMINI_API_KEY must be set in your .env file.")

# --- Gemini API Configuration ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini API configured successfully.")
except Exception as e:
    logger.critical(f"Failed to configure Gemini API: {e}")

# --- Models Dictionary ---
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


# ------------------------------------------------------------
# 2. Core Logic Classes
# ------------------------------------------------------------
class AuthManager:
    """Handles user authentication, session management, and JWT."""
    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def verify_password(self, password: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except (ValueError, TypeError):
            return False

    def create_jwt(self, user_info: dict) -> str:
        payload = {
            "sub": user_info["id"], "name": user_info.get("name"), "email": user_info.get("email"),
            "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(days=1),
        }
        return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

    def decode_jwt(self, token: Optional[str]) -> Optional[dict]:
        if not token: return None
        try:
            return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None

class DBManager:
    """Manages all interactions with the MongoDB database asynchronously."""
    def __init__(self, mongo_uri: str):
        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            self.db = self.client["jarvis_argus_db"]
            self.users = self.db["users"]
            self.conversations = self.db["conversations"]
            logger.info("MongoDB connection successful.")
        except Exception as e:
            logger.critical(f"MongoDB connection failed: {e}")
            raise

    async def _run_sync(self, func, *args, **kwargs):
        """Runs a synchronous pymongo call in a non-blocking thread."""
        return await asyncio.to_thread(func, *args, **kwargs)

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        return await self._run_sync(self.users.find_one, {"email": email.lower().strip()})

    async def create_user(self, name: str, email: str, hashed_pass: str) -> str:
        doc = {"name": name, "email": email.lower().strip(), "password": hashed_pass, "created_at": datetime.now(timezone.utc)}
        result = await self._run_sync(self.users.insert_one, doc)
        return str(result.inserted_id)

    async def get_conversations(self, user_id: str) -> List[dict]:
        cursor = self.conversations.find({"user_id": ObjectId(user_id)}).sort("created_at", DESCENDING)
        return await self._run_sync(list, cursor)

    async def create_conversation(self, user_id: str, title: str) -> str:
        doc = {"user_id": ObjectId(user_id), "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
        result = await self._run_sync(self.conversations.insert_one, doc)
        return str(result.inserted_id)
        
    async def get_conversation(self, conv_id: str, user_id: str) -> Optional[Dict]:
        return await self._run_sync(self.conversations.find_one, {"_id": ObjectId(conv_id), "user_id": ObjectId(user_id)})

    async def rename_conversation(self, conv_id: str, user_id: str, new_title: str) -> bool:
        result = await self._run_sync(self.conversations.update_one,
            {"_id": ObjectId(conv_id), "user_id": ObjectId(user_id)}, {"$set": {"title": new_title}})
        return result.modified_count > 0
        
    async def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        result = await self._run_sync(self.conversations.delete_one,
            {"_id": ObjectId(conv_id), "user_id": ObjectId(user_id)})
        return result.deleted_count > 0

    async def get_messages(self, conv_id: str) -> List[dict]:
        result = await self._run_sync(self.conversations.find_one, {"_id": ObjectId(conv_id)}, {"messages": 1})
        return result.get('messages', []) if result else []

    async def append_message(self, conv_id: str, msg: dict) -> str:
        msg_with_id = {**msg, "id": str(ObjectId())}
        await self._run_sync(self.conversations.update_one,
            {"_id": ObjectId(conv_id)}, {"$push": {"messages": msg_with_id}})
        return msg_with_id["id"]

    async def edit_message(self, conv_id: str, msg_id: str, new_content: str) -> bool:
        result = await self._run_sync(self.conversations.update_one,
            {"messages.id": msg_id}, {"$set": {"messages.$.content": new_content}})
        return result.modified_count > 0

    async def delete_message(self, conv_id: str, msg_id: str) -> bool:
        result = await self._run_sync(self.conversations.update_one,
            {"_id": ObjectId(conv_id)}, {"$pull": {"messages": {"id": msg_id}}})
        return result.modified_count > 0

class ChatManager:
    """Orchestrates the chat UI, state, streaming, and user interactions."""
    def __init__(self, auth: AuthManager, db: DBManager):
        self.auth = auth
        self.db = db

    async def setup_chat_environment(self, user: Dict):
        """Configures avatars, chat settings, and the conversation sidebar."""
        await cl.Avatar(name="کاربر", url="/public/user.svg").send()
        await cl.Avatar(name="آرگوس", url="/public/logo.svg", for_chainlit_helpers=True).send()
        
        # --- Chat Settings for Model Selection ---
        model_options = [
            cl.select.Option(label=f"{name} ({cat})", value=info['id'])
            for cat, group in MODELS.items()
            for name, info in group.items()
        ]
        await cl.ChatSettings([
            cl.select.Select(id="Model", label="انتخاب مدل هوش مصنوعی", options=model_options, initial_value="gemini-1.5-flash-latest")
        ]).send()
        
        await self.render_conversations_sidebar(user['sub'])

    async def stream_gemini_response(self, history: List[Dict], model_id: str, image: Optional[Image.Image] = None) -> str:
        """Streams Gemini response to the UI, supporting multimodal input."""
        message_placeholder = cl.Message(content="", author="آرگوس")
        try:
            model = genai.GenerativeModel(model_id)
            # Format history for API
            api_history = [{"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]} for m in history[:-1]]
            
            # Prepare the last message with optional image
            last_user_message_parts = [history[-1]["content"]]
            if image and "vision" in model_id or "1.5" in model_id: # Simple check for vision models
                 last_user_message_parts.append(image)
            
            # Start streaming
            stream = model.generate_content(api_history + [{"role": "user", "parts": last_user_message_parts}], stream=True)
            
            full_response = ""
            for chunk in stream:
                if chunk.text:
                    await message_placeholder.stream_token(chunk.text)
                    full_response += chunk.text
            
            # Finalize the message content
            if not full_response:
                await message_placeholder.update(content="پاسخی دریافت نشد. ممکن است به دلیل فیلترهای ایمنی باشد.")
                return "[No Response]"

            message_placeholder.content = full_response
            await message_placeholder.update()
            return full_response

        except Exception as e:
            logger.error(f"Gemini streaming error: {e}")
            error_content = f"**خطا در ارتباط با API:**\n`{str(e)}`"
            await message_placeholder.update(content=error_content)
            return f"Error: {e}"

    async def render_conversations_sidebar(self, user_id: str):
        """Renders the dynamic conversation history in the sidebar."""
        convs = await self.db.get_conversations(user_id)
        conv_actions = [cl.Action(name=f"select_conv_{str(c['_id'])}", value=str(c['_id']), label=f"💬 {c.get('title', 'بدون عنوان')}") for c in convs]
        
        control_actions = [
            cl.Action(name="new_chat", value="new", label="➕ مکالمه جدید", description="شروع یک چت جدید"),
            cl.Action(name="rename_conv", value="rename", label="✏️ تغییر نام مکالمه فعلی", description="تغییر نام چت انتخاب شده"),
            cl.Action(name="delete_conv", value="delete", label="🗑️ حذف مکالمه فعلی", description="حذف چت انتخاب شده")
        ]
        
        await cl.set_actions(control_actions + conv_actions)
    
    async def display_chat_history(self, conv_id: str):
        """Clears the UI and renders all messages for the selected conversation with actions."""
        await cl.empty_chat()
        messages = await self.db.get_messages(conv_id)
        for msg in messages:
            await cl.Message(
                content=msg['content'],
                author="کاربر" if msg['role'] == 'user' else "آرگوس",
                actions=[
                    cl.Action(name="edit_message", value=msg['id'], label="ویرایش"),
                    cl.Action(name="delete_message", value=msg['id'], label="حذف"),
                    cl.Action(name="copy_message", value=msg['content'], label="کپی")
                ]
            ).send()

# ------------------------------------------------------------
# 3. Global Instances & Chainlit Handlers
# ------------------------------------------------------------
auth = AuthManager()
db = DBManager(MONGO_URI)
chat = ChatManager(auth, db)

@cl.on_chat_start
async def on_chat_start():
    """Handle the start of a new chat session."""
    cl.user_session.set("model_id", "gemini-1.5-flash-latest") # Default model
    token = cl.user_session.get("jwt")
    user_payload = auth.decode_jwt(token)
    
    if user_payload:
        cl.user_session.set("user", user_payload)
        await chat.setup_chat_environment(user_payload)
        await cl.Message(content=f"**{user_payload['name']}، خوش آمدید!**\nیک مکالمه جدید شروع کنید یا از نوار کناری یکی را انتخاب نمایید.").send()
    else:
        # Block UI until login is successful
        cl.user_session.set("user", None)
        await cl.Message(content=f"به **{APP_TITLE}** خوش آمدید.\nبرای ورود: `login <email> <password>`\nبرای ثبت‌نام: `signup <name> <email> <password>`").send()

@cl.on_settings_update
async def on_settings_update(settings):
    model_id = settings.get("Model")
    if model_id:
        cl.user_session.set("model_id", model_id)
        await cl.Message(content=f"مدل به `{model_id}` تغییر یافت.").send()

@cl.on_message
async def on_message(message: cl.Message):
    """Handle incoming user messages."""
    user = cl.user_session.get("user")
    
    # --- Authentication Gate ---
    if not user:
        # Handle login and signup attempts
        parts = message.content.strip().split()
        cmd = parts[0].lower() if parts else ""

        if cmd == 'login' and len(parts) >= 3:
            email, password = parts[1], " ".join(parts[2:])
            db_user = await db.get_user_by_email(email)
            if db_user and auth.verify_password(password, db_user["password"]):
                user_info = {"id": str(db_user["_id"]), "name": db_user.get("name"), "email": db_user["email"]}
                token = auth.create_jwt(user_info)
                cl.user_session.set("jwt", token)
                await on_chat_start() # Re-initialize the session
            else:
                await cl.Message(content="ایمیل یا رمز عبور اشتباه است.").send()
        elif cmd == 'signup' and len(parts) >= 4:
            name, email, password = parts[1], parts[2], " ".join(parts[3:])
            if await db.get_user_by_email(email):
                await cl.Message(content="این ایمیل قبلا ثبت شده است. لطفا وارد شوید.").send()
                return
            hashed_pass = auth.hash_password(password)
            await db.create_user(name, email, hashed_pass)
            await cl.Message(content="ثبت‌نام موفق! اکنون با دستور `login` وارد شوید.").send()
        else:
            await cl.Message(content="دستور نامعتبر است. فرمت صحیح را بررسی کنید.").send()
        return

    # --- Main Chat Logic ---
    conv_id = cl.user_session.get("current_conv_id")
    image_element = next((el for el in message.elements if "image" in el.mime), None)
    image = Image.open(io.BytesIO(image_element.content)) if image_element else None

    if not conv_id:
        title = message.content[:30] or ("مکالمه تصویری" if image else "مکالمه جدید")
        conv_id = await db.create_conversation(user['sub'], title)
        cl.user_session.set("current_conv_id", conv_id)
        await chat.render_conversations_sidebar(user['sub'])

    await db.append_message(conv_id, {"role": "user", "content": message.content})
    
    # Display user message with actions immediately
    await chat.display_chat_history(conv_id)
    
    history = await db.get_messages(conv_id)
    model_response = await chat.stream_gemini_response(history, cl.user_session.get("model_id"), image)
    
    if "Error:" not in model_response:
        await db.append_message(conv_id, {"role": "assistant", "content": model_response})

@cl.on_action
async def on_action(action: cl.Action):
    """Handle button clicks from the UI."""
    user = cl.user_session.get("user")
    if not user: return
    
    conv_id = cl.user_session.get("current_conv_id")

    # --- Sidebar Actions ---
    if action.name == "new_chat":
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat()
        await cl.Message(content="یک مکالمه جدید شروع شد.").send()

    elif action.name.startswith("select_conv_"):
        cl.user_session.set("current_conv_id", action.value)
        await chat.display_chat_history(action.value)

    elif action.name == "rename_conv" and conv_id:
        res = await cl.AskUserMessage(content="عنوان جدید مکالمه را وارد کنید:", timeout=120).send()
        if res:
            await db.rename_conversation(conv_id, user['sub'], res['output'])
            await chat.render_conversations_sidebar(user['sub'])
            await cl.Message(content="نام مکالمه تغییر کرد.").send()

    elif action.name == "delete_conv" and conv_id:
        await db.delete_conversation(conv_id, user['sub'])
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat()
        await chat.render_conversations_sidebar(user['sub'])
        await cl.Message(content="مکالمه حذف شد.").send()

    # --- Message Actions ---
    elif action.name == "edit_message" and conv_id:
        res = await cl.AskUserMessage(content="متن جدید را وارد کنید:", timeout=180).send()
        if res:
            await db.edit_message(conv_id, action.value, res['output'])
            await chat.display_chat_history(conv_id)

    elif action.name == "delete_message" and conv_id:
        await db.delete_message(conv_id, action.value)
        await chat.display_chat_history(conv_id)

    elif action.name == "copy_message":
        # This is a client-side action, we just give feedback.
        await cl.Message(content="متن کپی شد! (این پیام فقط یک اعلان است)").send()
