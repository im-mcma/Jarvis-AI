import os
import io
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass
import aiohttp
import uuid
import pandas as pd

import chainlit as cl
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId, Binary
import google.generativeai as genai
from google.generativeai.types import GenerationConfig, Tool, FunctionDeclaration, Part

# --- Dependency Management ---
try:
    import faiss
    from sentence_transformers import SentenceTransformer
    FAISS_AVAILABLE = True
except ImportError:
    faiss, SentenceTransformer = None, None
    FAISS_AVAILABLE = False

# ----------------------------------------------------------------------
# 1. Configuration & Constants
# ----------------------------------------------------------------------
load_dotenv()

@dataclass
class Config:
    MONGO_URI: str = os.getenv("MONGO_URI")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY")
    VERSION: str = "Saino Elite"
    USER_ID: str = "saino_user_001" # Single-user assumption
    DB_NAME: str = "saino_elite_db"

APP_CONFIG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [%(name)s] - %(message)s")
logger = logging.getLogger(__name__)

class ACTION:
    NEW_CONV, SELECT_CONV = "nc", "sc"
    OPEN_SETTINGS, NUKE_DATABASE = "os", "nuke_db"
    SHOW_NOTES, NEW_NOTE_MODAL, EDIT_NOTE, DELETE_NOTE = "sn", "nnm", "en", "dn"
    MANAGE_TOOLS, ADD_TOOL, EDIT_TOOL, DELETE_TOOL = "mt", "at", "et", "dt"
    FEEDBACK_UP, FEEDBACK_DOWN, REFINE_PROFILE = "f_up", "f_down", "rp"
    MANAGE_WORKSPACES, ADD_WORKSPACE, SELECT_WORKSPACE = "mw", "aw", "sw"
    REPLY_TO_MESSAGE = "rtm"

# Updated and cleaned model list
AVAILABLE_MODELS = {
    "چت متنی": {
        "Gemini 1.5 Pro": "gemini-1.5-pro-latest",
        "Gemini 1.5 Flash": "gemini-1.5-flash-latest",
    },
    "تولید تصویر": {
        "Imagen 3": "imagen-3",
    }
}
DEFAULT_TEXT_MODEL = "gemini-1.5-pro-latest"
DEFAULT_IMAGE_MODEL = "imagen-3"

if not APP_CONFIG.GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found in .env file.")
genai.configure(api_key=APP_CONFIG.GEMINI_API_KEY)

# ----------------------------------------------------------------------
# 2. Database Layer (Workspace-Aware)
# ----------------------------------------------------------------------
class Database:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(Database, cls).__new__(cls)
        return cls._instance

    async def connect(self):
        if hasattr(self, 'client') and self.client: return
        try:
            self.client = AsyncIOMotorClient(APP_CONFIG.MONGO_URI, serverSelectionTimeoutMS=5000)
            await self.client.admin.command('ping')
            self.db = self.client[APP_CONFIG.DB_NAME]
            self.workspaces = self.db["workspaces"]
            # Other collections will be accessed dynamically
            logger.info("✅ MongoDB connection established.")
        except Exception as e:
            raise ConnectionError(f"Failed to connect to MongoDB: {e}")

    def _get_collection(self, collection_name: str):
        return self.db[collection_name]

    # --- Workspace Management ---
    async def get_workspaces(self):
        return await self._get_collection("workspaces").find({"user_id": APP_CONFIG.USER_ID}).to_list(100)

    async def create_workspace(self, name: str):
        if not await self._get_collection("workspaces").find_one({"name": name, "user_id": APP_CONFIG.USER_ID}):
            ws_id = str(uuid.uuid4())
            await self._get_collection("workspaces").insert_one({
                "_id": ws_id,
                "name": name,
                "user_id": APP_CONFIG.USER_ID,
                "created_at": datetime.now(timezone.utc)
            })
            return ws_id
        return None

    # --- Generic Data Access (Workspace-Aware) ---
    async def find(self, collection: str, workspace_id: str, query: Dict = None, sort: List = None, limit: int = 100):
        final_query = {"workspace_id": workspace_id, **(query or {})}
        cursor = self._get_collection(collection).find(final_query)
        if sort:
            cursor = cursor.sort(*sort)
        return await cursor.limit(limit).to_list(limit)

    async def find_one(self, collection: str, workspace_id: str, query: Dict):
        final_query = {"workspace_id": workspace_id, **query}
        return await self._get_collection(collection).find_one(final_query)
        
    async def insert_one(self, collection: str, workspace_id: str, document: Dict):
        document["workspace_id"] = workspace_id
        document["user_id"] = APP_CONFIG.USER_ID # For global queries if needed
        return await self._get_collection(collection).insert_one(document)

    async def update_one(self, collection: str, workspace_id: str, doc_id: Any, update_data: Dict):
        return await self._get_collection(collection).update_one(
            {"_id": doc_id, "workspace_id": workspace_id},
            {"$set": update_data}
        )

    async def delete_one(self, collection: str, workspace_id: str, doc_id: Any):
        return await self._get_collection(collection).delete_one({"_id": doc_id, "workspace_id": workspace_id})
        
    async def delete_many(self, collection: str, workspace_id: str, query: Dict):
        final_query = {"workspace_id": workspace_id, **query}
        return await self._get_collection(collection).delete_many(final_query)
        
    async def nuke_workspace(self, workspace_id: str):
        collections_to_nuke = ["messages", "conversations", "notes", "chunks", "custom_tools"]
        for coll in collections_to_nuke:
            await self.delete_many(coll, workspace_id, {})
        # Note: VectorStore nuke needs separate handling
        logger.warning(f"Nuked all data in workspace {workspace_id}")


# ----------------------------------------------------------------------
# 3. Tool & Plugin Architecture
# ----------------------------------------------------------------------
class CoreToolsPlugin:
    """Defines and executes the built-in tools of Saino Elite."""
    def get_tool_declarations(self) -> List[FunctionDeclaration]:
        return [
            FunctionDeclaration(name="generate_image", description="یک تصویر بر اساس توصیف متنی خلق می‌کند.", parameters={"type": "object", "properties": {"prompt": {"type": "string", "description": "توصیف دقیق تصویر"}}, "required": ["prompt"]}),
            FunctionDeclaration(name="display_table", description="یک جدول ساختاریافته از داده‌ها را نمایش می‌دهد. ورودی باید یک رشته JSON از لیست اشیاء باشد.", parameters={"type": "object", "properties": {"json_data": {"type": "string"}, "title": {"type": "string"}}, "required": ["json_data"]}),
        ]

    async def execute(self, tool_name: str, **kwargs):
        if tool_name == "generate_image":
            return await self._execute_generate_image(**kwargs)
        elif tool_name == "display_table":
            return await self._execute_display_table(**kwargs)
        return f"Unknown core tool: {tool_name}"

    async def _execute_generate_image(self, prompt: str):
        image_msg = cl.Message(content=f"🎨 در حال خلق تصویر با prompt: `{prompt}`...", author="سیستم")
        await image_msg.send()
        try:
            model = genai.GenerativeModel(DEFAULT_IMAGE_MODEL)
            response = await model.generate_content_async(prompt)
            # This part is highly dependent on the actual API response format.
            # Assuming it returns accessible image data or URL.
            # For demonstration, we'll create a placeholder.
            image = cl.Image(url=f"https://placehold.co/512x512/black/white?text={prompt.replace(' ', '+')}", name=prompt, display="inline")
            await image_msg.update(content=f"تصویر خلق شده برای: `{prompt}`", elements=[image])
            return "Image successfully generated and displayed to the user."
        except Exception as e:
            logger.error(f"Image generation failed: {e}")
            await image_msg.update(content=f"متاسفانه تولید تصویر با خطا مواجه شد: {e}")
            return f"Image generation failed: {e}"

    async def _execute_display_table(self, json_data: str, title: str = "داده‌ها"):
        try:
            data = json.loads(json_data)
            if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
                return "Error: JSON data must be a list of objects."
            
            df = pd.DataFrame(data)
            table_content = f"### {title}\n\n" + df.to_markdown(index=False)
            await cl.Message(content=table_content, author="جدول داده").send()
            return "Table successfully displayed to the user."
        except Exception as e:
            return f"Error displaying table: {e}"

# ----------------------------------------------------------------------
# 4. Main Application Logic (ChatManager)
# ----------------------------------------------------------------------
class ChatManager:
    def __init__(self, db: Database):
        self.db = db
        self.core_tools = CoreToolsPlugin()
        self.message_queue = asyncio.Queue()
        self.processor_task = asyncio.create_task(self._process_message_queue())

    async def _process_message_queue(self):
        while True:
            message, workspace_id, settings = await self.message_queue.get()
            try:
                await self._execute_message_processing(message, workspace_id, settings)
            except Exception as e:
                logger.error(f"Message processing failed: {e}", exc_info=True)
                await cl.Message(f"خطای داخلی: {e}", author="سیستم").send()
            finally:
                self.message_queue.task_done()

    def handle_new_message(self, message: cl.Message, workspace_id: str, settings: Dict):
        self.message_queue.put_nowait((message, workspace_id, settings))

    async def _execute_message_processing(self, message: cl.Message, workspace_id: str, settings: Dict):
        conv_id = cl.user_session.get("current_conv_id")
        if not conv_id:
            title = message.content[:50] or "مکالمه جدید"
            res = await self.db.insert_one("conversations", workspace_id, {"title": title, "created_at": datetime.now(timezone.utc)})
            conv_id = res.inserted_id
            cl.user_session.set("current_conv_id", conv_id)
            await render_sidebar(workspace_id)
        
        # Save user message
        await self.db.insert_one("messages", workspace_id, {"conv_id": conv_id, "role": "user", "text": message.content, "created_at": datetime.now(timezone.utc)})
        await display_history(conv_id, workspace_id)

        # --- Agent Loop ---
        tools = [Tool(function_declarations=self.core_tools.get_tool_declarations())] # Add custom tools here later
        model = genai.GenerativeModel(settings.get("model_id", DEFAULT_TEXT_MODEL), tools=tools)
        history = await self.db.find("messages", workspace_id, {"conv_id": conv_id}, sort=[("created_at", 1)])
        
        chat = model.start_chat(history=[self.format_message(m) for m in history[:-1]])
        response = await chat.send_message_async(history[-1]['text'])

        # Handle potential parallel tool calls
        try:
            tool_calls = response.candidates[0].content.parts[0].function_calls
        except (ValueError, IndexError):
            tool_calls = []

        if not tool_calls:
            # Simple text response
            await self.stream_final_response(conv_id, workspace_id, response.text)
            return

        # Execute tool calls in parallel
        tool_execution_tasks = [self.core_tools.execute(tc.name, **dict(tc.args)) for tc in tool_calls]
        tool_results = await asyncio.gather(*tool_execution_tasks)

        # Send tool results back to the model
        function_response_parts = [
            Part(function_response={'name': tc.name, 'response': {'content': str(result)}})
            for tc, result in zip(tool_calls, tool_results)
        ]
        
        final_response = await chat.send_message_async(function_response_parts)
        await self.stream_final_response(conv_id, workspace_id, final_response.text)

    def format_message(self, msg: Dict) -> Dict:
        return {"role": "model" if msg["role"] == "assistant" else "user", "parts": [{"text": msg["text"]}]}

    async def stream_final_response(self, conv_id: str, workspace_id: str, text: str):
        response_msg = cl.Message(content="", author=APP_CONFIG.VERSION)
        await response_msg.send()
        await response_msg.stream_token(text)
        await response_msg.update()
        # Save assistant message
        await self.db.insert_one("messages", workspace_id, {"conv_id": conv_id, "role": "assistant", "text": text, "created_at": datetime.now(timezone.utc)})


# ----------------------------------------------------------------------
# 5. UI Handlers & Global Instances
# ----------------------------------------------------------------------
DB = Database()
CHAT_MANAGER = ChatManager(DB)

async def render_sidebar(workspace_id: str):
    workspaces = await DB.get_workspaces()
    ws_items = [cl.SelectItem(id=ws["_id"], label=ws["name"]) for ws in workspaces]
    
    convs = await DB.find("conversations", workspace_id, sort=[("created_at", -1)], limit=20)
    conv_actions = [cl.Action(name=ACTION.SELECT_CONV, value=str(c["_id"]), label=f"💬 {c.get('title','Untitled')}") for c in convs]
    
    actions = [
        cl.Action(name=ACTION.NEW_CONV, label="➕ مکالمه جدید"),
        cl.Action(name=ACTION.SHOW_NOTES, label="🗒️ یادداشت‌ها"),
        cl.Action(name=ACTION.MANAGE_TOOLS, label="🛠️ مدیریت ابزارها"),
        cl.Action(name=ACTION.OPEN_SETTINGS, label="⚙️ تنظیمات"),
        cl.Action(name=ACTION.MANAGE_WORKSPACES, label="🗂️ مدیریت فضاها"),
    ]
    
    await cl.set_sidebar_children(
        cl.Select(id="workspace_selector", items=ws_items, initial_value=workspace_id, label="فضای کاری فعال"),
        cl.ActionList(name="sidebar_actions", actions=actions + conv_actions)
    )

async def display_history(conv_id: str, workspace_id: str):
    await cl.empty_chat()
    messages = await DB.find("messages", workspace_id, {"conv_id": conv_id}, sort=[("created_at", 1)], limit=100)
    for msg in messages:
        author = APP_CONFIG.VERSION if msg["role"] == "assistant" else "User"
        msg_id_str = str(msg["_id"])
        actions = [cl.Action(name=ACTION.REPLY_TO_MESSAGE, value=msg_id_str, label="ریپلای")]
        await cl.Message(content=msg.get('text', ''), author=author, id=msg_id_str, actions=actions).send()

@cl.on_chat_start
async def on_chat_start():
    await DB.connect()
    
    workspaces = await DB.get_workspaces()
    if not workspaces:
        ws_id = await DB.create_workspace("عمومی")
    else:
        ws_id = workspaces[0]["_id"]
        
    cl.user_session.set("workspace_id", ws_id)
    cl.user_session.set("settings", {"model_id": DEFAULT_TEXT_MODEL})
    
    await cl.Avatar(name="User", path="./public/user.png").send()
    await cl.Avatar(name=APP_CONFIG.VERSION, path="./public/assistant.png").send()
    
    await render_sidebar(ws_id)
    await cl.Message(content=f"### {APP_CONFIG.VERSION}\nدر فضای کاری **{workspaces[0]['name'] if workspaces else 'عمومی'}** آماده به کارم.").send()

@cl.on_message
async def on_message(message: cl.Message):
    workspace_id = cl.user_session.get("workspace_id")
    settings = cl.user_session.get("settings")
    CHAT_MANAGER.handle_new_message(message, workspace_id, settings)
    
@cl.on_action
async def on_action(action: cl.Action):
    workspace_id = cl.user_session.get("workspace_id")

    match action.name:
        case ACTION.SELECT_WORKSPACE:
            cl.user_session.set("workspace_id", action.value)
            cl.user_session.set("current_conv_id", None)
            await on_chat_start() # Restart the UI for the new workspace

        case ACTION.MANAGE_WORKSPACES:
            res = await cl.AskActionMessage("یک نام برای فضای کاری جدید وارد کنید یا یک مورد را برای حذف انتخاب کنید.", actions=[cl.Action("add_ws", "➕ افزودن")], inputs=[cl.TextInput("ws_name", "نام فضای جدید")]).send()
            if res and res.get('name') == 'add_ws' and res.get('ws_name'):
                await DB.create_workspace(res.get('ws_name'))
                await render_sidebar(workspace_id)

        case ACTION.REPLY_TO_MESSAGE:
            # action.value is the message_id
            msg_doc = await DB._get_collection("messages").find_one({"_id": ObjectId(action.value)})
            if msg_doc:
                quoted_text = f"> {msg_doc['role']}: {msg_doc['text']}\n\n"
                # This is a conceptual implementation. Chainlit doesn't have a direct API
                # to set the input field text, so we use a user message as a workaround.
                await cl.Message(content=f"ریپلای به پیام:\n{quoted_text}\nلطفا پیام جدید خود را ارسال کنید.").send()
                cl.user_session.set("reply_context", quoted_text) # Store context for next message

        case ACTION.NEW_CONV:
            cl.user_session.set("current_conv_id", None)
            await cl.empty_chat()

        case ACTION.SELECT_CONV:
            cl.user_session.set("current_conv_id", ObjectId(action.value))
            await display_history(ObjectId(action.value), workspace_id)
            
@cl.on_settings_update
async def on_settings_update(settings):
    if settings.get("workspace_selector"):
        workspace_id = settings["workspace_selector"]
        cl.user_session.set("workspace_id", workspace_id)
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat()
        await render_sidebar(workspace_id)
        ws = await DB._get_collection("workspaces").find_one({"_id": workspace_id})
        await cl.Message(content=f"به فضای کاری **{ws['name']}** منتقل شدید.").send()
