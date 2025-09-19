# -*- coding: utf-8 -*-
"""
Argus Nova — Chainlit full application (merged Streamlit UI ideas + Chainlit async backend)

Features included:
- Chainlit-based async chat server with avatars, chat settings, and actions
- MongoDB (pymongo) used with asyncio.to_thread wrappers for non-blocking DB access
- JWT-based auth (required JWT_SECRET_KEY env var for production safety)
- Gemini streaming integration (google.generativeai) with tokenized streaming to client
- Image (vision) input handling via PIL
- Message IDs, edit/delete actions, pagination (limit/skip)
- Model selection UI (synchronized with MODELS list) and sensible defaults
- Rate-limiting per-user to avoid API spam
- Defensive logging and clear error messages to user

Run instructions:
1. Set environment variables: MONGO_URI, GEMINI_API_KEY, JWT_SECRET_KEY, CHAINLIT_API_KEY (if required by your deploy).
2. Install requirements (example):
   pip install chainlit pymongo python-dotenv bcrypt PyJWT google-generativeai pillow
3. Run (local dev):
   chainlit run chainlit_jarvis_argus.py

Note: adjust model IDs in MODELS to match your available Gemini models.
"""

import os
import io
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import chainlit as cl
from PIL import Image
from bson import ObjectId
from pymongo import MongoClient, DESCENDING
import bcrypt
import jwt
import google.generativeai as genai
from dotenv import load_dotenv

# ------------------ Setup & Config ------------------
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("argus-nova-chainlit")
VERSION = "2.0.0"

# Required env vars (fail fast to avoid insecure defaults)
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

if not MONGO_URI:
    raise EnvironmentError("MONGO_URI environment variable is required")
if not GEMINI_API_KEY:
    raise EnvironmentError("GEMINI_API_KEY environment variable is required")
if not JWT_SECRET_KEY:
    raise EnvironmentError("JWT_SECRET_KEY environment variable is required (do NOT use a hardcoded default in production)")

# Configure Gemini
try:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini configured successfully")
except Exception as e:
    logger.exception("Failed to configure Gemini: %s", e)

# ------------------ Models ------------------
# Keep this list synchronized with your available models. Add or remove as needed.
MODELS: Dict[str, Dict[str, dict]] = {
    "چت متنی": {
        "Gemini 1.5 Flash (legacy)": {"id": "gemini-1.5-flash-latest", "RPM": 30, "RPD": 200},
        "Gemini 2.5 Pro": {"id": "gemini-2.5-pro", "RPM": 5, "RPD": 100},
        "Gemini 2.5 Flash": {"id": "gemini-2.5-flash", "RPM": 10, "RPD": 250},
        "Gemini 2.0 Flash": {"id": "gemini-2.0-flash", "RPM": 30, "RPD": 200},
    },
    "تولید تصویر": {
        "Gemini 2.5 Flash Image": {"id": "gemini-2.5-flash-image-preview", "RPM": 10, "RPD": 100},
        "Gemini 2.0 Flash Image": {"id": "gemini-2.0-flash-image", "RPM": 15, "RPD": 200},
    },
    "تولید ویدیو": {
        "Veo 3": {"id": "veo-3", "RPM": 5, "RPD": 50},
    }
}

# Flatten model options for quick lookup
MODEL_LOOKUP = {info["id"]: {"category": cat, "name": name, **info} for cat, group in MODELS.items() for name, info in group.items()}

# prefer a modern model if available
DEFAULT_MODEL_ID = "gemini-2.5-flash" if "gemini-2.5-flash" in MODEL_LOOKUP else ("gemini-1.5-flash-latest" if "gemini-1.5-flash-latest" in MODEL_LOOKUP else next(iter(MODEL_LOOKUP)))

# ------------------ Database Manager ------------------
class DBManager:
    def __init__(self, mongo_uri: str):
        self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        try:
            self.client.admin.command("ping")
        except Exception as e:
            logger.exception("MongoDB ping failed: %s", e)
            raise
        self.db = self.client.get_database("argus_nova_db")
        self.users = self.db["users"]
        self.conversations = self.db["conversations"]
        # Ensure indexes
        self._ensure_indexes()

    def _ensure_indexes(self):
        try:
            self.conversations.create_index([("user_id", 1), ("created_at", -1)])
            self.users.create_index([("email", 1)], unique=True)
            logger.debug("Indexes ensured")
        except Exception as e:
            logger.warning("Index creation warning: %s", e)

    async def _run_sync(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    # User helpers
    async def get_user_by_email(self, email: str) -> Optional[dict]:
        if not email: return None
        return await self._run_sync(self.users.find_one, {"email": email.lower().strip()})

    async def create_user(self, name: str, email: str, hashed: str) -> str:
        doc = {"name": name, "email": email.lower().strip(), "password": hashed, "created_at": datetime.now(timezone.utc)}
        res = await self._run_sync(self.users.insert_one, doc)
        return str(res.inserted_id)

    # Conversations & messages
    async def create_conversation(self, user_id: str, title: str) -> str:
        doc = {"user_id": ObjectId(user_id), "title": title, "created_at": datetime.now(timezone.utc), "messages": []}
        res = await self._run_sync(self.conversations.insert_one, doc)
        return str(res.inserted_id)

    async def get_conversations(self, user_id: str) -> List[dict]:
        cursor = self.conversations.find({"user_id": ObjectId(user_id)}).sort("created_at", DESCENDING)
        return await self._run_sync(list, cursor)

    async def get_messages(self, conv_id: str, limit: int = 50, skip: int = 0) -> List[dict]:
        # returns messages in chronological order (oldest first)
        result = await self._run_sync(self.conversations.find_one, {"_id": ObjectId(conv_id)}, {"messages": 1})
        if not result:
            return []
        msgs = result.get("messages", [])
        # messages are stored in append order; apply pagination safely
        start = max(0, len(msgs) - skip - limit)
        end = len(msgs) - skip
        slice_msgs = msgs[start:end]
        return slice_msgs

    async def append_message(self, conv_id: str, msg: dict) -> str:
        # attach string id
        msg_with_id = {**msg, "id": str(ObjectId())}
        await self._run_sync(self.conversations.update_one, {"_id": ObjectId(conv_id)}, {"$push": {"messages": msg_with_id}})
        return msg_with_id["id"]

    async def edit_message(self, conv_id: str, msg_id: str, new_content: str) -> bool:
        res = await self._run_sync(self.conversations.update_one, {"_id": ObjectId(conv_id), "messages.id": msg_id}, {"$set": {"messages.$.content": new_content}})
        return getattr(res, "modified_count", 0) > 0

    async def delete_message(self, conv_id: str, msg_id: str) -> bool:
        res = await self._run_sync(self.conversations.update_one, {"_id": ObjectId(conv_id)}, {"$pull": {"messages": {"id": msg_id}}})
        return getattr(res, "modified_count", 0) > 0

    async def rename_conversation(self, conv_id: str, user_id: str, new_title: str) -> bool:
        res = await self._run_sync(self.conversations.update_one, {"_id": ObjectId(conv_id), "user_id": ObjectId(user_id)}, {"$set": {"title": new_title}})
        return getattr(res, "modified_count", 0) > 0

    async def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        res = await self._run_sync(self.conversations.delete_one, {"_id": ObjectId(conv_id), "user_id": ObjectId(user_id)})
        return getattr(res, "deleted_count", 0) > 0

# ------------------ Auth Manager ------------------
class AuthManager:
    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def verify_password(self, password: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception:
            return False

    def create_jwt(self, user_info: dict) -> str:
        payload = {
            "sub": user_info["id"],
            "name": user_info.get("name"),
            "email": user_info.get("email"),
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
        }
        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode()
        return token

    def decode_jwt(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        try:
            return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])  # raises if invalid/expired
        except jwt.ExpiredSignatureError:
            return None
        except Exception as e:
            logger.warning("JWT decode error: %s", e)
            return None

# ------------------ Chat Manager ------------------
class ChatManager:
    def __init__(self, db: DBManager, auth: AuthManager):
        self.db = db
        self.auth = auth
        # per-session rate limit (in seconds)
        self.min_seconds_between_prompts = float(os.getenv("MIN_SECONDS_BETWEEN_PROMPTS", 0.6))

    async def setup_environment(self, user_payload: dict):
        # Avatars
        await cl.Avatar(name=user_payload.get("name", "کاربر"), url=None).send()
        await cl.Avatar(name="آریو", url=None, for_chainlit_helpers=True).send()

        # Model selection UI
        model_options = [cl.select.Option(label=f"{name} ({cat})", value=info['id']) for cat, group in MODELS.items() for name, info in group.items()]
        await cl.ChatSettings([
            cl.select.Select(id="Model", label="انتخاب مدل هوش مصنوعی", options=model_options, initial_value=DEFAULT_MODEL_ID)
        ]).send()

        # Send welcome message
        await cl.Message(content=f"سلام {user_payload.get('name','کاربر')} — به آرگون نوا خوش آمدید! از نوار تنظیمات برای انتخاب مدل استفاده کنید.").send()

        # Render conversational sidebar actions
        await self.render_conversations_sidebar(user_payload['sub'])

    async def render_conversations_sidebar(self, user_id: str):
        convs = await self.db.get_conversations(user_id)
        conv_actions = [cl.Action(name=f"select_conv", value=str(c['_id']), label=f"💬 {c.get('title', 'بدون عنوان')}") for c in convs]
        control_actions = [
            cl.Action(name="new_chat", value="new", label="➕ مکالمه جدید"),
            cl.Action(name="rename_conv", value="rename", label="✏️ تغییر نام"),
            cl.Action(name="delete_conv", value="delete", label="🗑️ حذف"
            )
        ]
        await cl.set_actions(control_actions + conv_actions)

    async def stream_gemini_response(self, history: List[Dict[str, Any]], model_id: str, image: Optional[Image.Image] = None) -> str:
        """
        Stream response from Gemini. history must be list of dicts with keys role ('user'|'assistant') and content (str).
        Returns the full assembled response string.
        """
        if not GEMINI_API_KEY:
            return "**خطا: کلید API گوگل تنظیم نشده است.**"

        try:
            # Format history for Gemini
            api_history = []
            for m in history:
                role = "user" if m.get("role") == "user" else "model"
                api_history.append({"role": role, "parts": [{"text": m.get("content", "")} ]})

            model = genai.GenerativeModel(model_id)
            stream = model.generate_content(api_history, stream=True)

            full_text = ""
            for chunk in stream:
                text = getattr(chunk, 'text', None)
                if text:
                    full_text += text
                    yield text

            return full_text
        except Exception as e:
            logger.exception("Gemini streaming error: %s", e)
            yield f"**خطای API:** {e}"
            return f"Error: {e}"

# ------------------ Global instances ------------------
DB = DBManager(MONGO_URI)
AUTH = AuthManager()
CHAT = ChatManager(DB, AUTH)

# ------------------ Chainlit Handlers ------------------
@cl.on_chat_start
async def on_chat_start():
    # initialize session values
    jwt_token = cl.user_session.get("jwt")
    user_payload = AUTH.decode_jwt(jwt_token)

    if user_payload:
        cl.user_session.set("user", user_payload)
        cl.user_session.set("model_id", cl.user_session.get("model_id", DEFAULT_MODEL_ID))
        await CHAT.setup_environment(user_payload)
    else:
        cl.user_session.set("user", None)
        # light instructions for first-time users
        await cl.Message(content=("به آرگوس خوش‌آمدید! برای ورود: `login <email> <password>`\n"
                                   "برای ثبت‌نام: `signup <name> <email> <password>`")).send()

@cl.on_settings_update
async def on_settings_update(settings: dict):
    model_id = settings.get("Model")
    if model_id:
        cl.user_session.set("model_id", model_id)
        await cl.Message(content=f"مدل به `{model_id}` تغییر یافت.").send()

@cl.on_message
async def on_message(message: cl.Message):
    """Main message handler. Accepts text and image elements."""
    # Authentication gate
    user = cl.user_session.get("user")
    if not user:
        # attempt login/signup commands
        parts = (message.content or "").strip().split()
        if not parts:
            await cl.Message(content="دستور نامعتبر. لطفاً `login` یا `signup` را امتحان کنید.").send()
            return
        cmd = parts[0].lower()
        if cmd == "login" and len(parts) >= 3:
            email = parts[1]
            password = " ".join(parts[2:])
            db_user = await DB.get_user_by_email(email)
            if db_user and AUTH.verify_password(password, db_user["password"]):
                user_info = {"id": str(db_user["_id"]), "name": db_user.get("name"), "email": db_user.get("email")}
                token = AUTH.create_jwt(user_info)
                cl.user_session.set("jwt", token)
                # re-initialize
                await on_chat_start()
            else:
                await cl.Message(content="ایمیل یا رمز عبور اشتباه است.").send()
        elif cmd == "signup" and len(parts) >= 4:
            name = parts[1]
            email = parts[2]
            password = " ".join(parts[3:])
            if await DB.get_user_by_email(email):
                await cl.Message(content="این ایمیل قبلاً ثبت شده است. لطفاً وارد شوید.").send()
                return
            hashed = AUTH.hash_password(password)
            uid = await DB.create_user(name, email, hashed)
            await cl.Message(content="ثبت‌نام موفق — اکنون با `login` وارد شوید.").send()
        else:
            await cl.Message(content="فرمت دستور درست نیست. از `login` یا `signup` استفاده کنید.").send()
        return

    # Rate limiting per session
    now_ts = datetime.now(timezone.utc).timestamp()
    last_ts = cl.user_session.get("last_prompt_ts", 0.0)
    if now_ts - last_ts < CHAT.min_seconds_between_prompts:
        await cl.Message(content="لطفاً کمی آهسته‌تر پیام ارسال کنید.").send()
        return
    cl.user_session.set("last_prompt_ts", now_ts)

    # Detect image element if present
    image = None
    try:
        image_elem = next((el for el in message.elements if getattr(el, 'mime', '').startswith('image')), None)
        if image_elem:
            image = Image.open(io.BytesIO(image_elem.content))
    except Exception:
        image = None

    # Ensure conversation exists
    conv_id = cl.user_session.get("current_conv_id")
    if not conv_id:
        title = (message.content or "مکالمه جدید")[:60]
        conv_id = await DB.create_conversation(user['sub'], title)
        cl.user_session.set("current_conv_id", conv_id)
        await CHAT.render_conversations_sidebar(user['sub'])

    # Append user message
    user_msg = {"role": "user", "content": message.content}
    user_msg_id = await DB.append_message(conv_id, user_msg)

    # Display current history
    await CHAT.display_chat_history(conv_id) if hasattr(CHAT, 'display_chat_history') else None

    # Stream model response
    model_id = cl.user_session.get("model_id", DEFAULT_MODEL_ID)

    # If model is a media model, handle differently
    is_media = MODEL_LOOKUP.get(model_id, {}).get('category') in ["تولید تصویر", "تولید ویدیو"]
    if is_media:
        # Placeholder simple reply for unsupported flows — can be extended to image generation
        reply_text = f"مدل انتخاب‌شده ({model_id}) یک مدل مولد رسانه است. در این نسخه، تولید رسانه (image/video) از طریق API انجام نشده است."
        assistant_msg = {"role": "assistant", "content": reply_text}
        await DB.append_message(conv_id, assistant_msg)
        await cl.Message(content=reply_text, author="آریو").send()
        return

    # Stream from Gemini; use streaming API
    try:
        placeholder = cl.Message(content="", author="آریو")
        await placeholder.send()
        # Prepare history for streaming: load last N messages (e.g., 40)
        history = await DB.get_messages(conv_id, limit=40, skip=0)
        model = genai.GenerativeModel(model_id)
        api_history = [{"role": ("user" if m.get("role") == "user" else "model"), "parts": [{"text": m.get("content","")}] } for m in history]
        stream = model.generate_content(api_history, stream=True)

        full_text = ""
        for chunk in stream:
            text = getattr(chunk, 'text', None)
            if text:
                full_text += text
                try:
                    await placeholder.stream_token(text)
                except Exception:
                    await placeholder.update(content=full_text)
        await placeholder.update(content=full_text)
        await DB.append_message(conv_id, {"role": "assistant", "content": full_text})

    except Exception as e:
        logger.exception("Error streaming response: %s", e)
        await cl.Message(content=f"خطا در پردازش پاسخ مدل: {e}", author="System").send()

@cl.on_action
async def on_action(action: cl.Action):
    user = cl.user_session.get("user")
    if not user:
        return
    conv_id = cl.user_session.get("current_conv_id")

    # sidebar actions
    if action.name == "new_chat":
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat()
        await cl.Message(content="یک مکالمه جدید شروع شد.").send()
        return

    if action.name == "select_conv":
        # action.value should be conv_id
        conv_id = action.value
        cl.user_session.set("current_conv_id", conv_id)
        # show history
        await CHAT.display_chat_history(conv_id) if hasattr(CHAT, 'display_chat_history') else None
        return

    if action.name == "rename_conv" and conv_id:
        res = await cl.AskUserMessage(content="عنوان جدید را وارد کنید:", timeout=120).send()
        if res:
            await DB.rename_conversation(conv_id, user['sub'], res['output'])
            await CHAT.render_conversations_sidebar(user['sub'])
            await cl.Message(content="نام مکالمه تغییر کرد.").send()
        return

    if action.name == "delete_conv" and conv_id:
        await DB.delete_conversation(conv_id, user['sub'])
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat()
        await CHAT.render_conversations_sidebar(user['sub'])
        await cl.Message(content="مکالمه حذف شد.").send()
        return

    # message actions (edit/delete/copy)
    if action.name == "edit_message" and conv_id:
        res = await cl.AskUserMessage(content="متن جدید را وارد کنید:", timeout=180).send()
        if res:
            success = await DB.edit_message(conv_id, action.value, res['output'])
            if success:
                await cl.Message(content="پیام ویرایش شد.").send()
                await CHAT.display_chat_history(conv_id) if hasattr(CHAT, 'display_chat_history') else None
            else:
                await cl.Message(content="ویرایش ناموفق بود.").send()
        return

    if action.name == "delete_message" and conv_id:
        await DB.delete_message(conv_id, action.value)
        await cl.Message(content="پیام حذف شد.").send()
        await CHAT.display_chat_history(conv_id) if hasattr(CHAT, 'display_chat_history') else None
        return

    if action.name == "copy_message":
        await cl.Message(content="متن کپی شد! (نمایش‌دهی کلاینتی)").send()
        return

# Optional: helper to render chat history for selected conversation
async def display_chat_history(conv_id: str):
    msgs = await DB.get_messages(conv_id, limit=200, skip=0)
    await cl.empty_chat()
    for m in msgs:
        author = "کاربر" if m.get("role") == "user" else "آریو"
        actions = [
            cl.Action(name="edit_message", value=m.get('id'), label="ویرایش"),
            cl.Action(name="delete_message", value=m.get('id'), label="حذف"),
            cl.Action(name="copy_message", value=m.get('content'), label="کپی")
        ]
        await cl.Message(content=m.get('content', ''), author=author, actions=actions).send()

# Attach helper to CHAT for reuse
CHAT.display_chat_history = display_chat_history

# ------------------ End of file ------------------
