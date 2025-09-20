# --- فایل: main.py (نسخه نهایی و کامل - اصلاح شده توسط Gemini) ---

import os
import sys
import json
import asyncio
import logging
import importlib.util
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Type

# --- کتابخانه‌های شخص ثالث ---
from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pydantic import BaseModel, Field as PydanticField, ValidationError
import aiofiles

# Import از مسیرهای صحیح Chainlit v2.8.0
from chainlit.components import Select, SelectItem, Action, ActionList
from chainlit import Message, File, Image, Audio, Text, Slider # Slider در ریشه chainlit قرار دارد
import chainlit as cl

import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool, HarmCategory
from pypdf import PdfReader
import docx
import backoff

# Import ابزارهای سفارشی و پیکربندی مدل
from tools.base import BaseTool
from model_config import MODEL_INFO

# ----------------------------------------------------------------------
# بخش ۱: پیکربندی و راه‌اندازی
# ----------------------------------------------------------------------
load_dotenv()
Path("tools").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("saino_elite_v5")

@dataclass
class Config:
    MONGO_URI: str = os.getenv("MONGO_URI", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    OAUTH_GITHUB_CLIENT_ID: str = os.getenv("OAUTH_GITHUB_CLIENT_ID", "")
    OAUTH_GITHUB_CLIENT_SECRET: str = os.getenv("OAUTH_GITHUB_CLIENT_SECRET", "")
    VERSION: str = "Saino Elite 5.0"
    DB_NAME: str = "saino_elite_v5_db"
    MAX_MODEL_CONCURRENCY: int = 5
    CHUNK_SIZE: int = 2000
    CHUNK_OVERLAP: int = 200

    def __post_init__(self):
        if not self.MONGO_URI or not self.GEMINI_API_KEY:
            raise RuntimeError("❌ متغیرهای MONGO_URI و GEMINI_API_KEY باید در فایل .env تنظیم شوند.")

try:
    CFG = Config()
    genai.configure(api_key=CFG.GEMINI_API_KEY)
except RuntimeError as e:
    logger.error(e)
    sys.exit(1)
except Exception as e:
    logger.error(f"❌ خطای پیکربندی Google Generative AI: {e}", exc_info=True)
    sys.exit(1)

MODEL_SEMAPHORE = asyncio.Semaphore(CFG.MAX_MODEL_CONCURRENCY)

class ACTION:
    NEW_CONV = "nc"; SELECT_CONV = "sc"; OPEN_SETTINGS = "os"; MANAGE_WORKSPACES = "mw"
    ADD_WORKSPACE = "aw"; DELETE_WORKSPACE = "dw"; CONFIRM_DELETE_WORKSPACE = "cdw"
    SELECT_WORKSPACE = "sw"; SAVE_SETTINGS = "ss"; SHOW_MEMORY = "sm"; ADD_MEMORY = "am"
    DELETE_MEMORY = "dm"; CONFIRM_DELETE_MEMORY = "cdm"

# ----------------------------------------------------------------------
# بخش ۲: مدل‌های داده Pydantic
# ----------------------------------------------------------------------
class BaseDBModel(BaseModel):
    id: str = PydanticField(default_factory=lambda: str(ObjectId()), alias="_id")
    created_at: datetime = PydanticField(default_factory=lambda: datetime.now(timezone.utc))
    user_id: str
    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}

class Workspace(BaseDBModel): name: str = PydanticField(min_length=1, max_length=50)
class Conversation(BaseDBModel): workspace_id: str; title: str = PydanticField(max_length=50)

# [✨ بهبود ۱]: تغییر نام مدل برای جلوگیری از تداخل با cl.Message
class DBMessage(BaseDBModel):
    workspace_id: str
    conv_id: str
    role: str
    content: str

class UserSettings(BaseDBModel): default_model: str = "gemini-1.5-flash-latest"; temperature: float = 0.7
class Memory(BaseDBModel): workspace_id: str; content: str
class DocumentChunk(BaseDBModel): workspace_id: str; file_name: str; content: str

# ----------------------------------------------------------------------
# بخش ۳: معماری ابزارهای پویا
# ----------------------------------------------------------------------
class ToolLoader:
    def __init__(self, tool_dir: str = "tools"):
        self.tool_dir = Path(tool_dir)
        self.loaded_modules = {}

    def load_tools(self) -> List[BaseTool]:
        tools = []
        for file in self.tool_dir.glob("*.py"):
            if file.stem == "__init__": continue
            module_name = f"{self.tool_dir.name}.{file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, file)
                if not spec or not spec.loader: continue

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                for item in dir(module):
                    obj = getattr(module, item)
                    if isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool:
                        tools.append(obj())
                        logger.info(f"✅ ابزار '{obj.name}' از {file.name} بارگذاری شد.")
            except Exception as e:
                logger.error(f"❌ خطا در بارگذاری ابزار از {file.name}: {e}", exc_info=True)
        return tools

class ToolManager:
    def __init__(self):
        self.loader = ToolLoader()
        self.tools: Dict[str, BaseTool] = {}
        self.reload_tools()

    def reload_tools(self):
        self.tools = {tool.name: tool for tool in self.loader.load_tools()}

    def get_all_declarations(self) -> List[FunctionDeclaration]:
        return [t.get_declaration() for t in self.tools.values()]

    async def execute_tool(self, name: str, **kwargs) -> Dict:
        if name not in self.tools:
            return {"status": "error", "error": f"ابزار '{name}' یافت نشد."}
        try:
            return await self.tools[name].execute(**kwargs)
        except Exception as e:
            logger.exception(f"❌ خطای بحرانی در ابزار {name}")
            return {"status": "error", "error": str(e)}

TOOLS = ToolManager()

# ----------------------------------------------------------------------
# بخش ۴: مدیریت پایگاه داده
# ----------------------------------------------------------------------
class DatabaseManager:
    _client: Optional[AsyncIOMotorClient] = None
    
    @backoff.on_exception(backoff.expo, Exception, max_tries=5)
    async def connect(self):
        if self._client: return
        self._client = AsyncIOMotorClient(CFG.MONGO_URI, serverSelectionTimeoutMS=5000)
        await self._client.admin.command('ping')
        self.db = self._client[CFG.DB_NAME]
        logger.info("✅ اتصال به MongoDB برقرار شد.")

    def _get_collection(self, name: str):
        if not self._client:
            raise RuntimeError("ابتدا باید به پایگاه داده متصل شوید.")
        return self.db[name]

    async def find(self, coll: str, q: Dict, m: Type, s: Optional[Tuple[str, int]] = None, l: int = 100) -> List[Any]:
        c = self._get_collection(coll).find(q)
        if s: c = c.sort(s[0], s[1])
        docs = await c.limit(l).to_list(length=l)
        return [m.model_validate(doc) for doc in docs]

    async def find_one(self, coll: str, q: Dict, m: Type) -> Optional[Any]:
        doc = await self._get_collection(coll).find_one(q)
        return m.model_validate(doc) if doc else None

    async def insert_one(self, coll: str, doc: BaseModel):
        data = doc.model_dump(by_alias=True)
        # ObjectId از روی id استرینگ ساخته می‌شود
        data['_id'] = ObjectId(data['_id'])
        return await self._get_collection(coll).insert_one(data)

    async def find_one_and_update(self, coll: str, q: Dict, u: Dict, m: Type, upsert=False):
        doc = await self._get_collection(coll).find_one_and_update(q, {"$set": u}, upsert=upsert, return_document=True)
        return m.model_validate(doc) if doc else None

    async def delete_many(self, coll: str, q: Dict):
        return await self._get_collection(coll).delete_many(q)

    async def delete_one(self, coll: str, q: Dict):
        return await self._get_collection(coll).delete_one(q)

    async def delete_workspace_cascade(self, workspace_id: str, user_id: str):
        q = {"workspace_id": workspace_id, "user_id": user_id}
        tasks = [
            self.delete_many("conversations", q),
            self.delete_many("messages", q),
            self.delete_many("memories", q),
            self.delete_many("documents", q),
            self._get_collection("workspaces").delete_one({"_id": ObjectId(workspace_id), "user_id": user_id})
        ]
        await asyncio.gather(*tasks)

DB = DatabaseManager()

# ----------------------------------------------------------------------
# بخش ۵: مدیریت مدل‌های هوش مصنوعی
# ----------------------------------------------------------------------
class ModelManager:
    def __init__(self):
        self._models: Dict[str, genai.GenerativeModel] = {}
        self.safety_settings = [{"category": hc, "threshold": "BLOCK_NONE"} for hc in HarmCategory]
        self._load_models()

    def _load_models(self):
        model_names = list(MODEL_INFO.keys())
        for name in model_names:
            try:
                self._models[name] = genai.GenerativeModel(model_name=name, safety_settings=self.safety_settings)
            except Exception as e:
                logger.warning(f"⚠️ مدل {name} بارگذاری نشد: {e}")

    def get_model(self, model_name: str) -> Optional[genai.GenerativeModel]:
        return self._models.get(model_name)

    def get_available_models(self) -> List[str]:
        return list(self._models.keys())

MODELS = ModelManager()

# ----------------------------------------------------------------------
# بخش ۶: هسته پردازشگر Agent
# ----------------------------------------------------------------------
class ChatProcessor:
    def __init__(self, db: DatabaseManager, tools: ToolManager, models: ModelManager):
        self.db = db
        self.tools = tools
        self.models = models

    async def _process_file_task(self, element: cl.File, workspace_id: str, user_id: str) -> bool:
        content = ""
        try:
            if element.path:
                async with aiofiles.open(element.path, mode="rb") as f:
                    if "pdf" in element.mime:
                        reader = PdfReader(f)
                        content = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
                    elif "word" in element.mime:
                        # [✅ رفع خطا]: جداسازی دستورات و اصلاح گرامر join
                        doc = docx.Document(f)
                        content = "\n".join([p.text for p in doc.paragraphs])
                    else: # برای فایل‌های متنی ساده
                        file_content = await f.read()
                        content = file_content.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"❌ خطای پردازش فایل {element.name}: {e}", exc_info=True)
            await cl.ErrorMessage(f"خطا در پردازش فایل {element.name}: {e}").send()
            return False

        if not content.strip():
            logger.warning(f"⚠️ محتوایی برای پردازش در فایل {element.name} یافت نشد.")
            return False

        chunks_to_insert = []
        for i in range(0, len(content), CFG.CHUNK_SIZE - CFG.CHUNK_OVERLAP):
            chunk_text = content[i:i + CFG.CHUNK_SIZE]
            chunk_doc = DocumentChunk(
                workspace_id=workspace_id, user_id=user_id,
                file_name=element.name, content=chunk_text
            )
            chunks_to_insert.append(chunk_doc.model_dump(by_alias=True))
        
        if chunks_to_insert:
            await self.db._get_collection("documents").insert_many(chunks_to_insert)
        return True

    async def _process_files(self, message: cl.Message, workspace_id: str, user_id: str):
        text_elements = [el for el in message.elements if isinstance(el, (File, Text))]
        if not text_elements: return
        
        msg = cl.Message(content="⏳ در حال پردازش فایل‌های آپلود شده...", author="System")
        await msg.send()
        
        tasks = [self._process_file_task(el, workspace_id, user_id) for el in text_elements]
        results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r)
        await msg.update(content=f"✅ {success_count} فایل با موفقیت پردازش و به پایگاه دانش اضافه شد.")

    async def _get_or_create_conversation(self, message: cl.Message, workspace_id: str, user_id: str) -> str:
        conv_id = cl.user_session.get("current_conv_id")
        if not conv_id:
            title = (message.content or "مکالمه جدید")[:50]
            conv = Conversation(workspace_id=workspace_id, title=title, user_id=user_id)
            await self.db.insert_one("conversations", conv)
            conv_id = conv.id
            cl.user_session.set("current_conv_id", conv_id)
        return conv_id

    async def _prepare_model_history(self, conv_id: str) -> List[Dict[str, Any]]:
        messages = await self.db.find("messages", {"conv_id": conv_id}, DBMessage, s=("created_at", 1), l=100)
        history = []
        for m in messages:
            # نقش 'assistant' به 'model' برای Gemini API تبدیل می‌شود
            role = "model" if m.role == "assistant" else m.role
            history.append({"role": role, "parts": [{"text": m.content}]})
        return history

    async def process_message(self, message: cl.Message):
        user = cl.user_session.get("user")
        if not user:
            await cl.Message("خطا: اطلاعات کاربر پیدا نشد. لطفاً مجدداً وارد شوید.").send()
            return
        
        try:
            workspace_id = cl.user_session.get("workspace_id")
            settings: UserSettings = cl.user_session.get("settings")
            user_id = user.identifier

            if message.elements:
                await self._process_files(message, workspace_id, user_id)

            if not message.content:
                logger.info("پیام خالی دریافت شد (فقط فایل ضمیمه). پردازش متوقف شد.")
                return

            conv_id = await self._get_or_create_conversation(message, workspace_id, user_id)
            await self.db.insert_one(
                "messages",
                DBMessage(workspace_id=workspace_id, conv_id=conv_id, role="user", content=message.content, user_id=user_id)
            )

            history = await self._prepare_model_history(conv_id)
            model = self.models.get_model(settings.default_model)
            if not model:
                await cl.ErrorMessage(f"مدل '{settings.default_model}' یافت نشد.").send()
                return
            
            @backoff.on_exception(backoff.expo, (genai.types.StopCandidateException, genai.types.BlockedPromptException), max_tries=3)
            async def generate_content_with_retry():
                return await model.generate_content_async(
                    history,
                    stream=True,
                    tools=[Tool(function_declarations=self.tools.get_all_declarations())],
                    generation_config=genai.types.GenerationConfig(temperature=settings.temperature)
                )

            response_stream = await generate_content_with_retry()
            await self._handle_stream_and_tools(response_stream, history, model, workspace_id, conv_id, user_id)
        except Exception as e:
            logger.exception("❌ خطای جدی در پردازش پیام.")
            await cl.ErrorMessage(f"یک خطای داخلی رخ داد: {e}").send()

    async def _handle_stream_and_tools(self, stream, history, model, workspace_id, conv_id, user_id):
        tool_calls = []
        text_response = ""
        ui_message = cl.Message(content="", author=CFG.VERSION)

        async for chunk in stream:
            try:
                if (parts := chunk.parts):
                    for part in parts:
                        if part.text:
                            if not ui_message.id: await ui_message.send()
                            text_response += part.text
                            await ui_message.stream_token(part.text)
                        if (function_call := getattr(part, 'function_call', None)):
                            tool_calls.append(function_call)
            except Exception as e:
                logger.error(f"خطا در پردازش چانک استریم: {e}")

        if ui_message.id: await ui_message.update()

        if tool_calls:
            tool_info = ", ".join([f"`{tc.name}`" for tc in tool_calls])
            await cl.Message(content=f"🛠️ در حال اجرای ابزار(ها): {tool_info}", author="System", parent_id=ui_message.id).send()

            tool_results = await asyncio.gather(*[
                self.tools.execute_tool(tc.name, **dict(tc.args))
                for tc in tool_calls
            ], return_exceptions=True)

            tool_response_parts = []
            for tc, res in zip(tool_calls, tool_results):
                if isinstance(res, Exception):
                    logger.exception(f"❌ خطای اجرایی در ابزار {tc.name}")
                    tool_response_parts.append({"tool_response": {"name": tc.name, "response": {"status": "error", "error": str(res)}}})
                else:
                    tool_response_parts.append({"tool_response": {"name": tc.name, "response": res}})

            # کل فراخوانی‌های ابزار در یک turn به تاریخچه اضافه می‌شوند
            history.append({"role": "model", "parts": [{"function_call": tc} for tc in tool_calls]})
            history.append({"role": "tool", "parts": tool_response_parts})

            final_stream = await model.generate_content_async(history, stream=True)
            # فراخوانی بازگشتی برای پردازش پاسخ نهایی مدل
            await self._handle_stream_and_tools(final_stream, history, model, workspace_id, conv_id, user_id)
        
        elif text_response.strip():
            await self.db.insert_one(
                "messages",
                DBMessage(workspace_id=workspace_id, conv_id=conv_id, role="assistant", content=text_response, user_id=user_id)
            )

PROCESSOR = ChatProcessor(DB, TOOLS, MODELS)

# ----------------------------------------------------------------------
# بخش ۷: رابط کاربری و مدیریت رویدادها
# ----------------------------------------------------------------------
@cl.on_chat_start
async def on_chat_start():
    user = cl.user_session.get("user")
    if not user:
        # این پیام در صورتی نمایش داده می‌شود که احراز هویت فعال باشد ولی کاربر وارد نشده باشد
        await cl.Message("لطفاً ابتدا با حساب خود وارد شوید.").send()
        return

    try:
        await DB.connect()
    except Exception as e:
        logger.error("❌ اتصال به پایگاه داده MongoDB ناموفق بود.", exc_info=True)
        await cl.ErrorMessage(f"خطا: اتصال به پایگاه داده برقرار نشد. لطفاً از صحت MONGO_URI مطمئن شوید.").send()
        return

    user_id = user.identifier
    ws = await DB.find_one("workspaces", {"user_id": user_id}, Workspace)
    if not ws:
        ws = Workspace(user_id=user_id, name="عمومی")
        await DB.insert_one("workspaces", ws)

    settings = await DB.find_one("settings", {"user_id": user_id}, UserSettings)
    if not settings:
        settings = UserSettings(user_id=user_id)
        await DB.insert_one("settings", settings)

    cl.user_session.set("workspace_id", ws.id)
    cl.user_session.set("settings", settings)
    cl.user_session.set("current_conv_id", None)
    
    await render_sidebar(user_id, ws.id)
    await cl.Message(content=f"### سلام {user.username}!\nبه {CFG.VERSION} خوش آمدید.").send()

async def render_sidebar(user_id: str, active_ws_id: str):
    workspaces = await DB.find("workspaces", {"user_id": user_id}, Workspace)
    ws_items = [SelectItem(id=ws.id, label=ws.name) for ws in workspaces]
    convs = await DB.find("conversations", {"workspace_id": active_ws_id}, Conversation, s=("created_at", -1), l=20)
    conv_actions = [Action(name=ACTION.SELECT_CONV, value=c.id, label=f"💬 {c.title}") for c in convs]
    main_actions = [
        Action(name=ACTION.NEW_CONV, label="➕ مکالمه جدید"),
        Action(name=ACTION.MANAGE_WORKSPACES, label="🗂️ مدیریت فضاها"),
        Action(name=ACTION.SHOW_MEMORY, label="🧠 مدیریت حافظه"),
        Action(name=ACTION.OPEN_SETTINGS, label="⚙️ تنظیمات")
    ]
    await cl.set_sidebar_children([
        Select(id=ACTION.SELECT_WORKSPACE, items=ws_items, initial_value=active_ws_id, label="فضای کاری فعال"),
        ActionList(name="sidebar_actions", actions=main_actions + conv_actions)
    ])

async def display_chat_history(conv_id: str):
    await cl.empty_chat()
    messages = await DB.find("messages", {"conv_id": conv_id}, DBMessage, s=("created_at", 1))
    for msg in messages:
        author = CFG.VERSION if msg.role == "assistant" else user.username if (user := cl.user_session.get("user")) else "User"
        await cl.Message(content=msg.content, author=author).send()

@cl.on_message
async def on_message(message: cl.Message):
    if not cl.user_session.get("user"):
        await cl.Message("لطفا ابتدا با حساب GitHub خود وارد شوید.").send()
        return
    await PROCESSOR.process_message(message)

@cl.on_action
async def on_action(action: cl.Action):
    user = cl.user_session.get("user")
    if not user:
        await cl.Message("خطا: اطلاعات کاربر پیدا نشد.").send()
        return
    
    user_id = user.identifier
    ws_id = cl.user_session.get("workspace_id")

    action_map = {
        ACTION.SELECT_WORKSPACE: handle_select_workspace,
        ACTION.NEW_CONV: handle_new_conv,
        ACTION.SELECT_CONV: handle_select_conv,
        ACTION.OPEN_SETTINGS: handle_open_settings,
        ACTION.SAVE_SETTINGS: handle_save_settings,
        ACTION.MANAGE_WORKSPACES: handle_manage_workspaces,
        ACTION.ADD_WORKSPACE: handle_add_workspace,
        ACTION.DELETE_WORKSPACE: handle_delete_workspace,
        ACTION.CONFIRM_DELETE_WORKSPACE: handle_confirm_delete_workspace,
        ACTION.SHOW_MEMORY: handle_show_memory,
        ACTION.ADD_MEMORY: handle_add_memory,
        ACTION.DELETE_MEMORY: handle_delete_memory,
        ACTION.CONFIRM_DELETE_MEMORY: handle_confirm_delete_memory,
    }

    handler = action_map.get(action.name)
    if handler:
        await handler(action, user_id, ws_id)
    else:
        logger.warning(f"Handler for action '{action.name}' not found.")

# --- Action Handlers ---

async def handle_select_workspace(action: cl.Action, user_id: str, ws_id: str):
    if action.value and action.value != ws_id:
        cl.user_session.set("workspace_id", action.value)
        cl.user_session.set("current_conv_id", None)
        await on_chat_start() # این تابع صفحه را کامل بازسازی می‌کند

async def handle_new_conv(action: cl.Action, user_id: str, ws_id: str):
    cl.user_session.set("current_conv_id", None)
    await cl.empty_chat()
    await cl.Message(content="مکالمه جدید آغاز شد. می‌توانید پیام خود را ارسال کنید.").send()

async def handle_select_conv(action: cl.Action, user_id: str, ws_id: str):
    if action.value:
        cl.user_session.set("current_conv_id", action.value)
        await display_chat_history(action.value)

async def handle_open_settings(action: cl.Action, user_id: str, ws_id: str):
    settings: UserSettings = cl.user_session.get("settings")
    model_items = [SelectItem(id=m, label=m) for m in MODELS.get_available_models()]
    await cl.AskActionMessage(
        "تنظیمات را ویرایش کنید:",
        actions=[Action(name=ACTION.SAVE_SETTINGS, label="ذخیره")],
        inputs=[
            Select(id="model", label="مدل پیش‌فرض", items=model_items, initial_value=settings.default_model),
            Slider(id="temp", label="Temperature", min=0, max=1, step=0.1, initial=settings.temperature)
        ]
    ).send()

async def handle_save_settings(action: cl.Action, user_id: str, ws_id: str):
    if not action.inputs: return
    try:
        new_settings_data = {"default_model": action.inputs['model'], "temperature": float(action.inputs['temp'])}
        updated = await DB.find_one_and_update(
            "settings", {"user_id": user_id}, new_settings_data, UserSettings, upsert=True
        )
        if updated:
            cl.user_session.set("settings", updated)
            await cl.Message("✅ تنظیمات با موفقیت ذخیره شد.").send()
    except Exception as e:
        logger.exception("❌ خطای ذخیره تنظیمات")
        await cl.ErrorMessage("خطا در ذخیره تنظیمات. لطفاً مجدداً تلاش کنید.").send()

async def handle_manage_workspaces(action: cl.Action, user_id: str, ws_id: str):
    workspaces = await DB.find("workspaces", {"user_id": user_id}, Workspace)
    actions = [Action(name=ACTION.ADD_WORKSPACE, label="➕ ایجاد فضای جدید")]
    # فقط اگر بیش از یک فضای کاری وجود داشته باشد، امکان حذف را نمایش بده
    if len(workspaces) > 1:
        actions.extend([Action(name=ACTION.DELETE_WORKSPACE, value=ws.id, label=f"🗑️ حذف '{ws.name}'") for ws in workspaces])
    await cl.AskActionMessage("مدیریت فضاها", actions=actions).send()

async def handle_add_workspace(action: cl.Action, user_id: str, ws_id: str):
    res = await cl.AskUserMessage("نام فضای کاری جدید:").send()
    if res and res.get("content"):
        name = res["content"].strip()
        if not name:
            await cl.ErrorMessage("نام فضای کاری نمی‌تواند خالی باشد.").send()
            return
        
        try:
            # اعتبارسنجی با Pydantic قبل از ذخیره
            new_ws = Workspace(user_id=user_id, name=name)
        except ValidationError as e:
            await cl.ErrorMessage(f"خطا در نام‌گذاری: {e}").send()
            return

        if not await DB.find_one("workspaces", {"user_id": user_id, "name": name}, Workspace):
            await DB.insert_one("workspaces", new_ws)
            await render_sidebar(user_id, ws_id)
            await cl.Message(f"✅ فضای کاری '{name}' ایجاد شد.").send()
        else:
            await cl.ErrorMessage(f"فضای کاری با نام '{name}' از قبل وجود دارد.").send()

async def handle_delete_workspace(action: cl.Action, user_id: str, ws_id: str):
    await cl.AskActionMessage(
        f"آیا از حذف این فضای کاری مطمئن هستید؟ تمام مکالمات و داده‌های مرتبط با آن **برای همیشه** پاک خواهند شد.",
        actions=[Action(name=ACTION.CONFIRM_DELETE_WORKSPACE, value=action.value, label="⚠️ بله، حذف کن")]
    ).send()

async def handle_confirm_delete_workspace(action: cl.Action, user_id: str, ws_id: str):
    if not action.value: return
    try:
        await DB.delete_workspace_cascade(action.value, user_id)
        # اگر فضای کاری فعال حذف شد، باید کاربر را به یک فضای کاری دیگر منتقل کنیم
        if action.value == ws_id:
            await cl.Message("فضای کاری فعال حذف شد. در حال بارگذاری مجدد...").send()
            await on_chat_start() # برنامه را از نو راه‌اندازی می‌کند و اولین فضای کاری را انتخاب می‌کند
        else:
            await render_sidebar(user_id, ws_id)
            await cl.Message("✅ فضای کاری حذف شد.").send()
    except InvalidId:
        await cl.ErrorMessage("شناسه فضای کاری نامعتبر است.").send()
    except Exception as e:
        logger.exception("❌ خطای حذف فضای کاری")
        await cl.ErrorMessage("خطا در حذف فضای کاری. لطفاً مجدداً تلاش کنید.").send()

async def handle_show_memory(action: cl.Action, user_id: str, ws_id: str):
    memories = await DB.find("memories", {"user_id": user_id, "workspace_id": ws_id}, Memory)
    msg_actions = [Action(name=ACTION.ADD_MEMORY, label="➕ افزودن به حافظه")]
    content = f"### 🧠 حافظه بلندمدت (فضای کاری فعلی)\n\n"
    if memories:
        for i, mem in enumerate(memories):
            content += f"{i+1}. {mem.content}\n"
            msg_actions.append(Action(name=ACTION.DELETE_MEMORY, value=mem.id, label=f"🗑️ حذف خاطره شماره {i+1}"))
        await cl.Message(content=content, actions=msg_actions).send()
    else:
        await cl.AskActionMessage("حافظه این فضای کاری خالی است.", actions=[Action(name=ACTION.ADD_MEMORY, label="➕ افزودن به حافظه")]).send()

async def handle_add_memory(action: cl.Action, user_id: str, ws_id: str):
    res = await cl.AskUserMessage("چه چیزی را به خاطر بسپارم؟").send()
    if res and res.get("content"):
        mem = Memory(user_id=user_id, workspace_id=ws_id, content=res['content'])
        await DB.insert_one("memories", mem)
        await cl.Message("✅ به حافظه اضافه شد.").send()

async def handle_delete_memory(action: cl.Action, user_id: str, ws_id: str):
    await cl.AskActionMessage(
        "آیا از حذف این خاطره مطمئن هستید؟",
        actions=[Action(name=ACTION.CONFIRM_DELETE_MEMORY, value=action.value, label="⚠️ بله، حذف کن")]
    ).send()

async def handle_confirm_delete_memory(action: cl.Action, user_id: str, ws_id: str):
    if not action.value: return
    try:
        await DB.delete_one("memories", {"_id": ObjectId(action.value), "user_id": user_id})
        await cl.Message("✅ خاطره حذف شد.").send()
    except InvalidId:
        await cl.ErrorMessage("شناسه خاطره نامعتبر است.").send()
    except Exception as e:
        logger.exception("❌ خطای حذف خاطره")
        await cl.ErrorMessage("خطا در حذف خاطره. لطفاً مجدداً تلاش کنید.").send()
