# -*- coding: utf-8 -*-
# Saino Elite - نسخه 5.0 (کامل، ماژولار و نهایی) - [اصلاح شده توسط سونیا]

# ----------------------------------------------------------------------
# بخش ۱: وارد کردن کتابخانه‌ها
# ----------------------------------------------------------------------
import os
import sys
import json
import asyncio
import logging
import importlib
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Literal, Type

# --- کتابخانه‌های شخص ثالث ---
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pydantic import BaseModel, Field as PydanticField

import chainlit as cl
import google.generativeai as genai
# [تغییر ۱]: SafetySetting از این خط حذف شد زیرا در نسخه‌های جدید منسوخ شده است
from google.generativeai.types import FunctionDeclaration, Tool, HarmCategory
import pandas as pd
from pypdf import PdfReader
import docx
from tavily import TavilyClient

# ----------------------------------------------------------------------
# بخش ۲: پیکربندی و راه‌اندازی
# ----------------------------------------------------------------------
load_dotenv()
Path("tools").mkdir(exist_ok=True) # اطمینان از وجود پوشه tools

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
CFG = Config()

if not all([CFG.MONGO_URI, CFG.GEMINI_API_KEY]):
    raise RuntimeError("❌ متغیرهای MONGO_URI و GEMINI_API_KEY باید تنظیم شوند.")
genai.configure(api_key=CFG.GEMINI_API_KEY)

MODEL_SEMAPHORE = asyncio.Semaphore(CFG.MAX_MODEL_CONCURRENCY)

class ACTION:
    NEW_CONV="nc"; SELECT_CONV="sc"; OPEN_SETTINGS="os"; MANAGE_WORKSPACES="mw"
    ADD_WORKSPACE="aw"; DELETE_WORKSPACE="dw"; CONFIRM_DELETE_WORKSPACE="cdw"
    SELECT_WORKSPACE="sw"; SAVE_SETTINGS="ss"; SHOW_MEMORY="sm"; ADD_MEMORY="am"
    DELETE_MEMORY="dm"; CONFIRM_DELETE_MEMORY="cdm"

# ----------------------------------------------------------------------
# بخش ۳: مدل‌های داده Pydantic
# ----------------------------------------------------------------------
class BaseDBModel(BaseModel):
    id: str = PydanticField(default_factory=lambda: str(ObjectId()), alias="_id")
    created_at: datetime = PydanticField(default_factory=lambda: datetime.now(timezone.utc))
    user_id: str
    class Config: populate_by_name = True; json_encoders = {ObjectId: str}

class Workspace(BaseDBModel): name: str
class Conversation(BaseDBModel): workspace_id: str; title: str
class Message(BaseDBModel): workspace_id: str; conv_id: str; role: str; content: str
class UserSettings(BaseDBModel): default_model: str = "gemini-1.5-flash-latest"; temperature: float = 0.7
class Memory(BaseDBModel): workspace_id: str; content: str
class DocumentChunk(BaseDBModel): workspace_id: str; file_name: str; content: str

# ----------------------------------------------------------------------
# بخش ۴: معماری ابزارهای پویا
# ----------------------------------------------------------------------
class BaseTool(ABC):
    name: str; description: str; parameters: Dict
    @abstractmethod
    async def execute(self, **kwargs) -> Dict: pass
    def get_declaration(self) -> FunctionDeclaration: return FunctionDeclaration(name=self.name, description=self.description, parameters=self.parameters)

class ToolLoader:
    def __init__(self, tool_dir: str = "tools"): self.tool_dir = Path(tool_dir)
    def load_tools(self) -> List[BaseTool]:
        tools = []
        for file in self.tool_dir.glob("*.py"):
            module_name = f"{self.tool_dir.name}.{file.stem}"
            try:
                if module_name in sys.modules: module = importlib.reload(sys.modules[module_name])
                else:
                    spec = importlib.util.spec_from_file_location(module_name, file)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)

                for item in dir(module):
                    obj = getattr(module, item)
                    if isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool:
                        tools.append(obj())
                        logger.info(f"✅ ابزار '{obj.name}' از {file.name} بارگذاری شد.")
            except Exception as e: logger.error(f"❌ خطا در بارگذاری ابزار از {file.name}: {e}")
        return tools

class ToolManager:
    def __init__(self):
        self.loader = ToolLoader()
        self.tools: Dict[str, BaseTool] = {}
        self.reload_tools()
    def reload_tools(self): self.tools = {tool.name: tool for tool in self.loader.load_tools()}
    def get_all_declarations(self) -> List[FunctionDeclaration]: return [t.get_declaration() for t in self.tools.values()]
    async def execute_tool(self, name: str, **kwargs) -> Dict:
        if name not in self.tools: return {"status": "error", "error": f"ابزار '{name}' یافت نشد."}
        try: return await self.tools[name].execute(**kwargs)
        except Exception as e: logger.exception(f"❌ خطای بحرانی در ابزار {name}"); return {"status": "error", "error": str(e)}
TOOLS = ToolManager()

# ----------------------------------------------------------------------
# بخش ۵: مدیریت پایگاه داده
# ----------------------------------------------------------------------
class DatabaseManager:
    _client = None
    async def connect(self):
        if self._client: return
        self._client = AsyncIOMotorClient(CFG.MONGO_URI, serverSelectionTimeoutMS=5000)
        self.db = self._client[CFG.DB_NAME]; logger.info("✅ اتصال به MongoDB برقرار شد.")
    def _get_collection(self, name: str): return self.db[name]
    async def find(self, coll: str, q: Dict, m: Type, s: Optional[Tuple[str, int]] = None, l: int=100) -> List[Any]:
        c = self._get_collection(coll).find(q);
        if s: c = c.sort(s[0], s[1])
        docs = await c.limit(l).to_list(length=l); return [m.model_validate(doc) for doc in docs]
    async def find_one(self, coll: str, q: Dict, m: Type) -> Optional[Any]:
        doc = await self._get_collection(coll).find_one(q); return m.model_validate(doc) if doc else None
    async def insert_one(self, coll: str, doc: BaseModel): return await self._get_collection(coll).insert_one(doc.model_dump(by_alias=True))
    async def find_one_and_update(self, coll: str, q: Dict, u: Dict, m: Type, upsert=False):
        doc = await self._get_collection(coll).find_one_and_update(q, {"$set": u}, upsert=upsert, return_document=True)
        return m.model_validate(doc) if doc else None
    async def delete_many(self, coll: str, q: Dict): return await self._get_collection(coll).delete_many(q)
    async def delete_one(self, coll: str, q: Dict): return await self._get_collection(coll).delete_one(q)
    async def delete_workspace_cascade(self, workspace_id: str, user_id: str):
        q = {"workspace_id": workspace_id, "user_id": user_id}
        await self.delete_many("conversations", q); await self.delete_many("messages", q)
        await self.delete_many("memories", q); await self.delete_many("documents", q)
        await self._get_collection("workspaces").delete_one({"_id": workspace_id, "user_id": user_id})
DB = DatabaseManager()

# ----------------------------------------------------------------------
# بخش ۶: مدیریت مدل‌های هوش مصنوعی
# ----------------------------------------------------------------------
class ModelManager:
    def __init__(self):
        self._models = {}
        model_names = ["gemini-1.5-pro-latest", "gemini-1.5-flash-latest", "gemini-pro"]
        # [تغییر ۲]: ساختار safety_settings به فرمت دیکشنری که در نسخه‌های جدید لازم است، تغییر کرد
        self.safety_settings = [{"category": hc, "threshold": "BLOCK_NONE"} for hc in HarmCategory]
        for name in model_names:
            try:
                self._models[name] = genai.GenerativeModel(model_name=name, safety_settings=self.safety_settings)
            except Exception as e:
                logger.warning(f"⚠️ مدل {name} بارگذاری نشد: {e}")
    def get_model(self, model_name: str) -> Optional[genai.GenerativeModel]: return self._models.get(model_name)
    def get_available_models(self) -> List[str]: return list(self._models.keys())
MODELS = ModelManager()

# ----------------------------------------------------------------------
# بخش ۷: هسته پردازشگر Agent (منطق اصلی)
# ----------------------------------------------------------------------
class ChatProcessor:
    def __init__(self, db: DatabaseManager, tools: ToolManager, models: ModelManager):
        self.db = db; self.tools = tools; self.models = models

    async def _process_files(self, elements: List[cl.Element], workspace_id: str, user_id: str):
        text_elements = [el for el in elements if "text" in el.mime or "pdf" in el.mime or "word" in el.mime]
        if not text_elements: return

        msg = cl.Message(content="در حال پردازش فایل‌های آپلود شده...", author="System"); await msg.send()
        all_chunks = []
        for el in text_elements:
            content = ""
            if el.path:
                with open(el.path, "rb") as f:
                    if "pdf" in el.mime: reader = PdfReader(f); content = "\n".join([page.extract_text() for page in reader.pages])
                    elif "word" in el.mime: doc = docx.Document(f); content = "\n".join([p.text for p in doc.paragraphs])
                    else: content = f.read().decode("utf-8")
            
            if not content: continue
            
            # Text chunking
            for i in range(0, len(content), CFG.CHUNK_SIZE - CFG.CHUNK_OVERLAP):
                chunk_text = content[i:i + CFG.CHUNK_SIZE]
                chunk = DocumentChunk(workspace_id=workspace_id, user_id=user_id, file_name=el.name, content=chunk_text)
                await self.db.insert_one("documents", chunk)

        await msg.update(content=f"✅ {len(text_elements)} فایل با موفقیت پردازش و به پایگاه دانش اضافه شد.")

    async def _get_or_create_conversation(self, message: cl.Message, workspace_id: str, user_id: str) -> str:
        conv_id = cl.user_session.get("current_conv_id")
        if not conv_id:
            title = (message.content or "مکالمه جدید")[:50]
            conv = Conversation(workspace_id=workspace_id, title=title, user_id=user_id)
            await self.db.insert_one("conversations", conv)
            conv_id = conv.id; cl.user_session.set("current_conv_id", conv_id)
        return conv_id

    async def _prepare_model_history(self, conv_id: str) -> List[Dict[str, Any]]:
        messages = await self.db.find("messages", {"conv_id": conv_id}, Message, sort=("created_at", -1), limit=10)
        history = []
        for m in reversed(messages): history.append({"role": m.role, "parts": [{"text": m.content}]})
        return history

    async def process_message(self, message: cl.Message):
        try:
            user = cl.user_session.get("user"); workspace_id = cl.user_session.get("workspace_id")
            settings: UserSettings = cl.user_session.get("settings"); user_id = user.identifier

            if message.elements: await self._process_files(message.elements, workspace_id, user_id)

            conv_id = await self._get_or_create_conversation(message, workspace_id, user_id)
            await self.db.insert_one("messages", Message(workspace_id=workspace_id, conv_id=conv_id, role="user", content=message.content, user_id=user_id))

            history = await self._prepare_model_history(conv_id)
            model = self.models.get_model(settings.default_model)
            if not model: await cl.Message(f"مدل '{settings.default_model}' یافت نشد.").send(); return
            
            response_stream = await model.generate_content_async(
                history, stream=True,
                tools=[Tool(function_declarations=self.tools.get_all_declarations())],
                generation_config=genai.types.GenerationConfig(temperature=settings.temperature)
            )

            await self._handle_stream_and_tools(response_stream, history, model, workspace_id, conv_id, user_id)

        except Exception as e: logger.exception("❌ خطای جدی در پردازش پیام."); await cl.ErrorMessage(f"یک خطای داخلی رخ داد: {e}").send()

    async def _handle_stream_and_tools(self, stream, history, model, workspace_id, conv_id, user_id):
        tool_calls = []; text_response = ""
        ui_message = cl.Message(content="", author=CFG.VERSION)

        async for chunk in stream:
            try:
                if (parts := chunk.parts):
                    for part in parts:
                        if part.text:
                            if not ui_message.id: await ui_message.send()
                            text_response += part.text; await ui_message.stream_token(part.text)
                        if (function_call := part.function_call): tool_calls.append(function_call)
            except Exception as e: logger.error(f"خطا در پردازش چانک: {e}")

        if ui_message.id: await ui_message.update()
        
        if tool_calls:
            model_response_with_tools = f"[فراخوانی ابزار: {', '.join([tc.name for tc in tool_calls])}]"
            await self.db.insert_one("messages", Message(workspace_id=workspace_id, conv_id=conv_id, role="model", content=model_response_with_tools, user_id=user_id))
            
            tasks = [self.tools.execute_tool(tc.name, **dict(tc.args)) for tc in tool_calls]
            tool_results = await asyncio.gather(*tasks)

            tool_response_parts = [{"tool_response": {"name": tc.name, "response": res}} for tc, res in zip(tool_calls, tool_results)]
            
            history.append({"role": "model", "parts": [{"function_call": tc} for tc in tool_calls]})
            history.append({"role": "tool", "parts": tool_response_parts})
            
            final_stream = await model.generate_content_async(history, stream=True)
            await self._handle_stream_and_tools(final_stream, history, model, workspace_id, conv_id, user_id)
        
        elif text_response:
            await self.db.insert_one("messages", Message(workspace_id=workspace_id, conv_id=conv_id, role="assistant", content=text_response, user_id=user_id))
PROCESSOR = ChatProcessor(DB, TOOLS, MODELS)

# ----------------------------------------------------------------------
# بخش ۸: رابط کاربری و مدیریت رویدادها
# ----------------------------------------------------------------------
# (بدون تغییر در این بخش)
@cl.on_chat_start
async def on_chat_start():
    await DB.connect()
    user = cl.user_session.get("user")
    if not user:
        # Chainlit should handle redirecting to login
        await cl.Message("لطفاً ابتدا با حساب خود وارد شوید.").send()
        return

    user_id = user.identifier
    ws = await DB.find_one("workspaces", {"user_id": user_id}, Workspace)
    if not ws: ws = Workspace(user_id=user_id, name="عمومی"); await DB.insert_one("workspaces", ws)

    settings = await DB.find_one("settings", {"user_id": user_id}, UserSettings)
    if not settings: settings = UserSettings(user_id=user_id); await DB.insert_one("settings", settings)

    cl.user_session.set("workspace_id", ws.id); cl.user_session.set("settings", settings)
    cl.user_session.set("current_conv_id", None)
    
    await render_sidebar(user_id, ws.id)
    await cl.Message(content=f"### سلام {user.username}!\nبه {CFG.VERSION} خوش آمدید.").send()

async def render_sidebar(user_id: str, active_ws_id: str):
    workspaces = await DB.find("workspaces", {"user_id": user_id}, Workspace)
    ws_items = [cl.SelectItem(id=ws.id, label=ws.name) for ws in workspaces]
    convs = await DB.find("conversations", {"workspace_id": active_ws_id}, Conversation, sort=("created_at", -1), limit=20)
    conv_actions = [cl.Action(name=ACTION.SELECT_CONV, value=c.id, label=f"💬 {c.title}") for c in convs]
    main_actions = [
        cl.Action(name=ACTION.NEW_CONV, label="➕ مکالمه جدید"),
        cl.Action(name=ACTION.MANAGE_WORKSPACES, label="🗂️ مدیریت فضاها"),
        cl.Action(name=ACTION.SHOW_MEMORY, label="🧠 مدیریت حافظه"),
        cl.Action(name=ACTION.OPEN_SETTINGS, label="⚙️ تنظیمات")
    ]
    await cl.set_sidebar_children([
        cl.Select(id=ACTION.SELECT_WORKSPACE, items=ws_items, initial_value=active_ws_id, label="فضای کاری فعال"),
        cl.ActionList(name="sidebar_actions", actions=main_actions + conv_actions)
    ])

async def display_chat_history(conv_id: str):
    await cl.empty_chat()
    messages = await DB.find("messages", {"conv_id": conv_id}, Message, sort=("created_at", 1))
    for msg in messages:
        author = CFG.VERSION if msg.role in ["assistant", "model"] else "User"
        await cl.Message(content=msg.content, author=author).send()


@cl.on_message
async def on_message(message: cl.Message):
    if not cl.user_session.get("user"): await cl.Message("لطفا ابتدا با حساب GitHub خود وارد شوید.").send(); return
    await PROCESSOR.process_message(message)

@cl.on_action
async def on_action(action: cl.Action):
    user = cl.user_session.get("user"); user_id = user.identifier
    ws_id = cl.user_session.get("workspace_id")

    if action.name == ACTION.SELECT_WORKSPACE:
        if action.value != ws_id:
            cl.user_session.set("workspace_id", action.value); cl.user_session.set("current_conv_id", None)
            # Re-initialize the chat view for the new workspace
            await on_chat_start() 

    elif action.name == ACTION.NEW_CONV:
        cl.user_session.set("current_conv_id", None); await cl.empty_chat()
        await cl.Message(content="مکالمه جدید آغاز شد.").send()

    elif action.name == ACTION.SELECT_CONV:
        cl.user_session.set("current_conv_id", action.value); await display_chat_history(action.value)

    elif action.name == ACTION.OPEN_SETTINGS:
        settings: UserSettings = cl.user_session.get("settings")
        res = await cl.AskActionMessage("تنظیمات را ویرایش کنید:", actions=[cl.Action(name=ACTION.SAVE_SETTINGS, label="ذخیره")], inputs=[
                cl.Select("model", label="مدل پیش‌فرض", items=[cl.SelectItem(id=m, label=m) for m in MODELS.get_available_models()], initial_value=settings.default_model),
                cl.Slider("temp", label="Temperature", min=0, max=1, step=0.1, initial=settings.temperature)
            ]).send()
        if res and res.get("name") == ACTION.SAVE_SETTINGS:
            new_settings = {"default_model": res['inputs']['model'], "temperature": float(res['inputs']['temp'])}
            updated = await DB.find_one_and_update("settings", {"user_id": user_id}, new_settings, UserSettings, upsert=True)
            cl.user_session.set("settings", updated); await cl.Message("تنظیمات ذخیره شد.").send()

    elif action.name == ACTION.MANAGE_WORKSPACES:
        workspaces = await DB.find("workspaces", {"user_id": user_id}, Workspace)
        actions = [cl.Action(name=ACTION.ADD_WORKSPACE, label="➕ ایجاد فضای جدید")]
        actions.extend([cl.Action(name=ACTION.DELETE_WORKSPACE, value=ws.id, label=f"🗑️ حذف '{ws.name}'") for ws in workspaces])
        await cl.AskActionMessage("مدیریت فضاها", actions=actions).send()

    elif action.name == ACTION.ADD_WORKSPACE:
        res = await cl.AskUserMessage("نام فضای کاری جدید:").send()
        if res and res.get("content"):
            name = res["content"].strip()
            if not await DB.find_one("workspaces", {"user_id": user_id, "name": name}, Workspace):
                ws = Workspace(user_id=user_id, name=name); await DB.insert_one("workspaces", ws)
                await render_sidebar(user_id, ws_id)
            else: await cl.Message(f"فضای کاری '{name}' از قبل وجود دارد.").send()

    elif action.name == ACTION.DELETE_WORKSPACE:
        await cl.AskActionMessage(f"آیا از حذف این فضای کاری مطمئن هستید؟ این عمل غیرقابل بازگشت است.", actions=[
                cl.Action(name=ACTION.CONFIRM_DELETE_WORKSPACE, value=action.value, label="⚠️ بله، حذف کن")
            ]).send()

    elif action.name == ACTION.CONFIRM_DELETE_WORKSPACE:
        await DB.delete_workspace_cascade(action.value, user_id)
        if action.value == ws_id:
            await on_chat_start()
        else:
            await render_sidebar(user_id, ws_id)
        await cl.Message("فضای کاری حذف شد.").send()

    elif action.name == ACTION.SHOW_MEMORY:
        memories = await DB.find("memories", {"user_id": user_id, "workspace_id": ws_id}, Memory)
        msg_actions = [cl.Action(name=ACTION.ADD_MEMORY, label="➕ افزودن به حافظه")]
        content = "### حافظه بلندمدت Agent\n\n"
        if memories:
            for mem in memories:
                content += f"- {mem.content} \n"
                msg_actions.append(cl.Action(name=ACTION.DELETE_MEMORY, value=mem.id, label=f"حذف خاطره {mem.id[:4]}..."))
            await cl.Message(content=content, actions=msg_actions).send()
        else:
            await cl.AskActionMessage("حافظه خالی است.", actions=[cl.Action(name=ACTION.ADD_MEMORY, label="➕ افزودن به حافظه")]).send()

    elif action.name == ACTION.ADD_MEMORY:
        res = await cl.AskUserMessage("چه چیزی را به خاطر بسپارم؟").send()
        if res and res.get("content"):
            mem = Memory(user_id=user_id, workspace_id=ws_id, content=res['content'])
            await DB.insert_one("memories", mem); await cl.Message("به حافظه اضافه شد.").send()

    elif action.name == ACTION.DELETE_MEMORY:
        mem_id = action.value
        await cl.AskActionMessage("آیا از حذف این خاطره مطمئن هستید؟", actions=[
                cl.Action(name=ACTION.CONFIRM_DELETE_MEMORY, value=mem_id, label="⚠️ بله، حذف کن")
            ]).send()

    elif action.name == ACTION.CONFIRM_DELETE_MEMORY:
        await DB.delete_one("memories", {"_id": action.value, "user_id": user_id})
        await cl.Message("خاطره حذف شد.").send()
