import os
import uuid
import json
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Literal

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# Chainlit
import chainlit as cl

# Google GenAI modern SDK
from google.generativeai import GenerativeModel
from google.generativeai.types import FunctionDeclaration, Part, FunctionResponse, Tool
import pandas as pd

# Tavily client (optional)
try:
    from tavily import TavilyClient
    TAVILY_AVAILABLE = True
except Exception:
    TavilyClient = None
    TAVILY_AVAILABLE = False

# -------------------- Config & Logging --------------------
load_dotenv()

cl.set_settings({
    "Project": {
        "name": "Saino-AI",
        "author": "𝕚𝕞_𝕒𝕓𝕚e",
        "description": "یک چت‌بات با قابلیت‌های پیشرفته و استفاده از مدل‌های چندگانه Gemini 2.5 و ابزارهای خارجی.",
        "version": "1.2",
        "features": {
            "oauth": {
                "google": False,
                "github": False,
                "oauth_user_info": False
            }
        },
        "default_locale": "fa-IR",
        "ui": {
            "name": "Saino Elite",
            "hide_watermark": False,
            "theme": "dark"
        }
    },
    "App": {
        "user_env_vars": [
            "MONGO_URI",
            "GEMINI_API_KEY",
            "TAVILY_API_KEY"
        ],
        "log_level": "INFO"
    },
    "Data": {
        "disable_telemetry": True
    },
    "Chat": {
        "show_feedback": True,
        "thumbs_up_down": True,
        "voting_threshold": 1,
        "on_message_timeout": 60,
        "on_chat_start_timeout": 60,
        "show_agent_logs": True,
        "stream_timeout": 120,
        "message_history_size": 20
    }
})

@dataclass
class Config:
    MONGO_URI: str = os.getenv("MONGO_URI", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    VERSION: str = "Saino Elite"
    USER_ID: str = "saino_user_001"
    DB_NAME: str = "saino_elite_db"
    MAX_MODEL_CONCURRENCY: int = int(os.getenv("MAX_MODEL_CONCURRENCY", "3"))

CFG = Config()

if not CFG.GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY در فایل .env تنظیم نشده است.")
if not CFG.TAVILY_API_KEY:
    logging.warning("TAVILY_API_KEY در فایل .env تنظیم نشده است — ابزار جستجو غیرفعال خواهد بود.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("saino")

MODEL_SEMAPHORE = asyncio.Semaphore(CFG.MAX_MODEL_CONCURRENCY)

# -------------------- ACTIONS --------------------
class ACTION:
    NEW_CONV = "nc"
    SELECT_CONV = "sc"
    OPEN_SETTINGS = "os"
    SHOW_NOTES = "sn"
    NEW_NOTE_MODAL = "nnm"
    EDIT_NOTE = "en"
    DELETE_NOTE = "dn"
    MANAGE_WORKSPACES = "mw"
    ADD_WORKSPACE = "aw"
    SELECT_WORKSPACE = "sw"
    REPLY_TO_MESSAGE = "rtm"
    ADD_NOTE = "an"

# -------------------- Database wrapper --------------------
class Database:
    _instance = None

    def __new__(cls, *a, **k):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def connect(self):
        if hasattr(self, "client") and getattr(self, "client", None):
            return
        if not CFG.MONGO_URI:
            raise RuntimeError("MONGO_URI در فایل .env تنظیم نشده است.")
        try:
            self.client = AsyncIOMotorClient(CFG.MONGO_URI, serverSelectionTimeoutMS=5000)
            await self.client.admin.command("ping")
            self.db = self.client[CFG.DB_NAME]
            logger.info("✅ اتصال به MongoDB برقرار شد.")
        except Exception as e:
            logger.exception("❌ اتصال به MongoDB ناموفق بود.")
            raise

    def _collection(self, name: str):
        return self.db[name]

    async def get_workspaces(self) -> List[Dict]:
        return await self._collection("workspaces").find({"user_id": CFG.USER_ID}).to_list(100)

    async def create_workspace(self, name: str) -> Optional[str]:
        col = self._collection("workspaces")
        exists = await col.find_one({"name": name, "user_id": CFG.USER_ID})
        if exists:
            return None
        ws_id = str(ObjectId())
        await col.insert_one({"_id": ws_id, "name": name, "user_id": CFG.USER_ID, "created_at": datetime.now(timezone.utc)})
        return ws_id

    async def delete_workspace(self, workspace_id: str):
        await self._collection("conversations").delete_many({"workspace_id": workspace_id})
        await self._collection("messages").delete_many({"workspace_id": workspace_id})
        await self._collection("notes").delete_many({"workspace_id": workspace_id})
        await self._collection("workspaces").delete_one({"_id": workspace_id})
        logger.warning("فصای کاری %s حذف شد.", workspace_id)

    async def find(self, collection: str, workspace_id: str, query: Dict = None, sort: Optional[Tuple[str,int]] = None, limit: int = 100):
        q = {"workspace_id": workspace_id}
        if query:
            q.update(query)
        cursor = self._collection(collection).find(q)
        if sort:
            cursor = cursor.sort(sort[0], sort[1])
        return await cursor.limit(limit).to_list(limit)

    async def find_one(self, collection: str, workspace_id: str, query: Dict):
        q = {"workspace_id": workspace_id}
        q.update(query)
        return await self._collection(collection).find_one(q)

    async def insert_one(self, collection: str, workspace_id: str, document: Dict):
        doc = dict(document)
        doc["workspace_id"] = workspace_id
        doc["user_id"] = CFG.USER_ID
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        if "created_at" not in doc:
            doc["created_at"] = datetime.now(timezone.utc)
        res = await self._collection(collection).insert_one(doc)
        return res

    async def update_one(self, collection: str, workspace_id: str, doc_id: Any, update_data: Dict):
        return await self._collection(collection).update_one({"_id": doc_id, "workspace_id": workspace_id}, {"$set": update_data})

    async def delete_one(self, collection: str, workspace_id: str, doc_id: Any):
        return await self._collection(collection).delete_one({"_id": doc_id, "workspace_id": workspace_id})

DB = Database()

# -------------------- Core Tools Plugin --------------------
class CoreToolsPlugin:
    """
    مجموعه ابزارهای اصلی برای استفاده مدل.
    """
    def __init__(self):
        self._tavily = None
        if TAVILY_AVAILABLE and CFG.TAVILY_API_KEY:
            try:
                self._tavily = TavilyClient(api_key=CFG.TAVILY_API_KEY)
            except Exception:
                logger.exception("⚠️ Tavily client فعال نیست. کلید API نامعتبر است.")
                self._tavily = None

    def get_tool_declarations(self) -> List[FunctionDeclaration]:
        return [
            FunctionDeclaration(
                name="generate_image",
                description="Generate image from text prompt",
                parameters={
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                },
            ),
            FunctionDeclaration(
                name="display_table",
                description="Display JSON list of objects as a table",
                parameters={
                    "type": "object",
                    "properties": {
                        "json_data": {"type": "string"},
                        "title": {"type": "string"}
                    },
                    "required": ["json_data"],
                },
            ),
            FunctionDeclaration(
                name="search",
                description="Perform a web search using Tavily",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
        ]

    async def execute(self, tool_name: str, **kwargs):
        logger.debug(f"Executing tool {tool_name} with args: {kwargs}")
        if tool_name == "generate_image":
            return await self._generate_image(kwargs.get("prompt", ""))
        if tool_name == "display_table":
            return await self._display_table(kwargs.get("json_data", "[]"), kwargs.get("title", "Data"))
        if tool_name == "search":
            return await self._search(kwargs.get("query", ""))
        return {"status": "error", "error": f"ابزار ناشناخته: {tool_name}"}

    async def _generate_image(self, prompt: str):
        m = cl.Message(content=f"در حال تولید تصویر برای: `{prompt}` ...", author="System")
        await m.send()
        try:
            url_safe = (prompt or "image").replace(" ", "+")[:120]
            placeholder = f"https://placehold.co/512x512/222/fff?text={url_safe}"
            img = cl.Image(url=placeholder, name=prompt or "image", display="inline")
            await m.update(content=f"تصویر تولید شد برای: `{prompt}`", elements=[img])
            return {"status": "ok", "url": placeholder, "text": f"تصویر در {placeholder}"}
        except Exception as e:
            logger.exception("❌ خطا در ابزار تولید تصویر.")
            await m.update(content=f"خطا در تولید تصویر: {e}")
            return {"status": "error", "error": str(e)}

    async def _display_table(self, json_data: str, title: str = "Data"):
        try:
            data = json.loads(json_data)
            if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                return {"status": "error", "error": "JSON نامعتبر: باید لیستی از اشیاء باشد."}
            df = pd.DataFrame(data)
            md = f"### {title}\n\n" + df.to_markdown(index=False)
            await cl.Message(content=md, author="Table").send()
            return {"status": "ok", "text": md}
        except Exception as e:
            logger.exception("❌ خطا در ابزار نمایش جدول.")
            return {"status": "error", "error": str(e)}

    async def _search(self, query: str):
        m = cl.Message(content=f"در حال جستجو برای `{query}` ...", author="System")
        await m.send()
        if not self._tavily:
            err = "Tavily client در دسترس نیست. نصب پکیج `tavily-python` و تنظیم `TAVILY_API_KEY` در فایل .env لازم است."
            logger.error(err)
            await m.update(content=err)
            return {"status": "error", "error": err}
        try:
            raw = self._tavily.search(query)
            results = raw.get("results", raw if isinstance(raw, list) else [])
            md = f"### نتایج جستجو برای `{query}`:\n\n"
            out = []
            for i, r in enumerate(results[:6], 1):
                title = r.get("title", "") if isinstance(r, dict) else str(r)
                snippet = r.get("snippet", "") if isinstance(r, dict) else ""
                url = r.get("url", "") if isinstance(r, dict) else ""
                md += f"{i}. **{title}**\n{snippet}\n{url}\n\n"
                out.append({"title": title, "snippet": snippet, "url": url})
            await cl.Message(content=md, author="Search").send()
            return {"status": "ok", "results": out, "text": md}
        except Exception as e:
            logger.exception("❌ خطا در ابزار جستجو.")
            await m.update(content=f"خطا در جستجو: {e}")
            return {"status": "error", "error": str(e)}

TOOLS = CoreToolsPlugin()

# -------------------- Model Manager --------------------
class ModelManager:
    """
    مسئول انتخاب مدل مناسب بر اساس ورودی کاربر و نوع درخواست.
    """
    def __init__(self):
        self._models = {
            "gemini-2.5-pro": GenerativeModel("gemini-2.5-pro", api_key=CFG.GEMINI_API_KEY),
            "gemini-2.5-flash": GenerativeModel("gemini-2.5-flash", api_key=CFG.GEMINI_API_KEY),
            "gemini-2.5-flash-lite": GenerativeModel("gemini-2.5-flash-lite", api_key=CFG.GEMINI_API_KEY),
            "gemini-2.5-flash-preview-tts": GenerativeModel("gemini-2.5-flash-preview-tts", api_key=CFG.GEMINI_API_KEY),
            "gemini-2.5-flash-image-preview": GenerativeModel("gemini-2.5-flash-image-preview", api_key=CFG.GEMINI_API_KEY),
            "gemini-2.0-flash": GenerativeModel("gemini-2.0-flash", api_key=CFG.GEMINI_API_KEY),
            "gemini-2.0-flash-preview-image-generation": GenerativeModel("gemini-2.0-flash-preview-image-generation", api_key=CFG.GEMINI_API_KEY),
            "gemini-1.5-pro": GenerativeModel("gemini-1.5-pro", api_key=CFG.GEMINI_API_KEY),
        }
        
    def get_model(self, message: cl.Message) -> GenerativeModel:
        """
        یک مدل را بر اساس نوع محتوای پیام انتخاب می‌کند.
        """
        # بررسی وجود فایل‌های چندرسانه‌ای
        if message.elements:
            for element in message.elements:
                if element.type in ["image", "video", "audio"]:
                    logger.info("مدل چندحالته برای پردازش محتوای تصویری/صوتی انتخاب شد.")
                    return self._models.get("gemini-2.5-pro", self._models["gemini-1.5-pro"])
        
        # اگر ورودی فقط متن است
        if message.content and len(message.content) > 200:
            logger.info("مدل Pro برای پردازش متن بلند انتخاب شد.")
            return self._models.get("gemini-2.5-pro", self._models["gemini-1.5-pro"])
        
        # مدل پیش‌فرض برای مکالمه‌های سریع و متنی
        logger.info("مدل Flash برای پردازش سریع متن انتخاب شد.")
        return self._models.get("gemini-2.5-flash", self._models["gemini-1.5-pro"])

    def get_tts_model(self) -> GenerativeModel:
        """
        مدل TTS اختصاصی را بازمی‌گرداند.
        """
        return self._models.get("gemini-2.5-flash-preview-tts")
    
    def get_image_generation_model(self) -> GenerativeModel:
        """
        مدل تولید تصویر را بازمی‌گرداند.
        """
        return self._models.get("gemini-2.5-flash-image-preview", self._models.get("gemini-2.0-flash-preview-image-generation"))

MODELS = ModelManager()

# -------------------- Chat Manager --------------------
class ChatManager:
    """
    مسئول مدیریت جریان اصلی چت.
    """
    def __init__(self, db: Database, tools: CoreToolsPlugin, models: ModelManager):
        self.db = db
        self.tools = tools
        self.models = models
        self.queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker())

    async def _worker(self):
        while True:
            message, workspace_id, settings = await self.queue.get()
            try:
                await self._process_message(message, workspace_id, settings)
            except Exception as e:
                logger.exception("❌ خطا در پردازش پیام.")
                await cl.Message(content=f"خطای داخلی: {e}", author="System").send()
            finally:
                self.queue.task_done()

    def handle_new_message(self, message: cl.Message, workspace_id: str, settings: Dict):
        self.queue.put_nowait((message, workspace_id, settings))

    async def _process_message(self, message: cl.Message, workspace_id: str, settings: Dict):
        if not workspace_id:
            await cl.Message("خطا: فضای کاری مشخص نشده است.", author="System").send()
            return

        conv_id = cl.user_session.get("current_conv_id")
        if not conv_id:
            title = (message.content or "")[:120] or "مکالمه جدید"
            res = await self.db.insert_one("conversations", workspace_id, {"title": title})
            conv_id = str(res.inserted_id)
            cl.user_session.set("current_conv_id", conv_id)

        await self.db.insert_one("messages", workspace_id, {
            "_id": ObjectId(),
            "conv_id": ObjectId(conv_id),
            "role": "user",
            "text": message.content or "",
            "elements": [e.to_dict() for e in message.elements] if message.elements else None,
            "created_at": datetime.now(timezone.utc)
        })

        # انتخاب مدل مناسب بر اساس پیام ورودی
        model_to_use = self.models.get_model(message)
        
        # آماده‌سازی تاریخچه مکالمه برای مدل
        msgs = await self.db.find("messages", workspace_id, {"conv_id": ObjectId(conv_id)}, sort=("created_at", 1), limit=10)
        formatted_history = []
        for m in msgs:
            role = "user" if m.get("role") == "user" else "model"
            parts = [Part(text=m.get("text", ""))]
            if m.get("elements"):
                for el in m.get("elements"):
                    # تبدیل دوباره عناصر از dict به cl.Image/Video و غیره
                    # در اینجا برای سادگی فقط URL را به عنوان یک Part جدید اضافه می‌کنیم
                    if el.get("type") in ["image", "video", "audio"] and el.get("url"):
                        parts.append(Part(text=f"[{el['type']} at {el['url']}]"))
            formatted_history.append({"role": role, "parts": parts})

        async with MODEL_SEMAPHORE:
            try:
                # فراخوانی اولیه مدل برای بررسی نیاز به ابزار
                response = await model_to_use.generate_content_async(
                    contents=formatted_history,
                    tools=[Tool(function_declarations=self.tools.get_tool_declarations())]
                )
            except Exception as e:
                logger.exception("❌ خطا در تماس اولیه با مدل.")
                await cl.Message(content=f"خطا در تماس با مدل: {e}", author="System").send()
                return

        tool_calls = []
        try:
            candidates = response.candidates
            if candidates:
                for cand in candidates:
                    for p in cand.content.parts:
                        if hasattr(p, "function_call"):
                            tool_calls.append(p.function_call)
        except Exception:
            tool_calls = []

        # اگر ابزاری فراخوانی نشده باشد، پاسخ را استریم می‌کند
        if not tool_calls:
            await self._stream_and_save(conv_id, workspace_id, response)
            return

        # اجرای همزمان ابزارهای فراخوانی شده
        tasks = [self._safe_tool_execute(tc.name, tc.args) for tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        func_responses = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"❌ خطا در اجرای ابزار {tool_calls[i].name}: {res}")
                fr = FunctionResponse(name=tool_calls[i].name, response={"content": f"خطا: {res}"})
            else:
                fr = FunctionResponse(name=tool_calls[i].name, response={"content": str(res)})
            func_responses.append(fr)

        # ارسال پاسخ ابزارها به مدل برای دریافت پاسخ نهایی
        try:
            final_response = await model_to_use.generate_content_async(
                contents=formatted_history + [Part(role="function", function_responses=func_responses)]
            )
            await self._stream_and_save(conv_id, workspace_id, final_response)
        except Exception as e:
            logger.exception("❌ خطا در دریافت پاسخ نهایی از مدل.")
            await cl.Message(content=f"خطا در دریافت پاسخ نهایی: {e}", author="System").send()

    async def _safe_tool_execute(self, name: str, kwargs: Dict):
        try:
            res = await asyncio.wait_for(self.tools.execute(name, **(kwargs or {})), timeout=30)
            return res
        except asyncio.TimeoutError:
            logger.exception(f"⏰ ابزار {name} به اتمام رسید.")
            return {"status":"error","error":"timeout"}
        except Exception as e:
            logger.exception(f"❌ خطای استثنایی در ابزار {name}.")
            return {"status":"error","error": str(e)}

    async def _stream_and_save(self, conv_id: Any, workspace_id: str, response):
        """
        پاسخ مدل را به صورت استریم (جریانی) نمایش می‌دهد و سپس ذخیره می‌کند.
        """
        msg = cl.Message(content="", author=CFG.VERSION)
        await msg.send()
        
        full_text = ""
        try:
            async for chunk in response:
                text_chunk = chunk.text
                if text_chunk:
                    await msg.stream_token(text_chunk)
                    full_text += text_chunk
        except Exception as e:
            logger.exception("❌ خطا در استریم کردن پاسخ.")
            # اگر استریم با خطا مواجه شد، کل متن دریافت شده را نمایش می‌دهد.
            await msg.update(content=full_text + f"\n\n**خطای استریمینگ:** {e}")

        # ذخیره کامل پاسخ در دیتابیس
        await self.db.insert_one("messages", workspace_id, {
            "_id": ObjectId(),
            "conv_id": ObjectId(conv_id),
            "role": "assistant",
            "text": full_text,
            "created_at": datetime.now(timezone.utc)
        })

CHAT = ChatManager(DB, TOOLS, MODELS)

# -------------------- UI Handlers --------------------
async def render_sidebar(workspace_id: str):
    workspaces = await DB.get_workspaces()
    ws_items = [cl.SelectItem(id=ws["_id"], label=ws["name"]) for ws in workspaces]
    convs = await DB.find("conversations", workspace_id, sort=("created_at", -1), limit=50)
    conv_actions = [cl.Action(name=ACTION.SELECT_CONV, value=str(c["_id"]), label=f"💬 {c.get('title','بدون عنوان')}") for c in convs]
    actions = [
        cl.Action(name=ACTION.NEW_CONV, label="➕ مکالمه جدید"),
        cl.Action(name=ACTION.SHOW_NOTES, label="🗒️ یادداشت‌ها"),
        cl.Action(name=ACTION.MANAGE_WORKSPACES, label="🗂️ مدیریت فضاها"),
        cl.Action(name=ACTION.OPEN_SETTINGS, label="⚙️ تنظیمات")
    ]
    await cl.set_sidebar_children(
        cl.Select(id="workspace_selector", items=ws_items, initial_value=workspace_id, label="فضای کاری فعال"),
        cl.ActionList(name="sidebar_actions", actions=actions + conv_actions)
    )

async def display_history(conv_id: Any, workspace_id: str):
    await cl.empty_chat()
    messages = await DB.find("messages", workspace_id, {"conv_id": ObjectId(conv_id)}, sort=("created_at", 1), limit=300)
    for m in messages:
        author = CFG.VERSION if m.get("role") == "assistant" else "User"
        msg_id = str(m["_id"])
        actions = [cl.Action(name=ACTION.REPLY_TO_MESSAGE, value=msg_id, label="ریپلای")]
        
        elements = []
        if m.get("elements"):
            for el_data in m.get("elements"):
                el_type = el_data.get("type")
                if el_type == "image":
                    elements.append(cl.Image(name=el_data.get("name"), url=el_data.get("url"), display=el_data.get("display")))
                # افزودن انواع دیگر عنصر در صورت نیاز
        
        await cl.Message(content=m.get("text",""), author=author, id=msg_id, actions=actions, elements=elements).send()

@cl.on_chat_start
async def on_chat_start():
    await DB.connect()
    workspaces = await DB.get_workspaces()
    if not workspaces:
        ws_id = await DB.create_workspace("عمومی")
    else:
        ws_id = workspaces[0]["_id"]
    cl.user_session.set("workspace_id", ws_id)
    cl.user_session.set("settings", {"model_id": "gemini-1.5-pro-latest"})
    try:
        await cl.Avatar(name="User", path="./public/user.png").send()
        await cl.Avatar(name=CFG.VERSION, path="./public/assistant.png").send()
    except Exception:
        logger.debug("❌ آواتار یافت نشد.")
    await render_sidebar(ws_id)
    await cl.Message(content=f"### {CFG.VERSION}\nدر فضای کاری **{ws_id}** آماده‌ام.").send()

@cl.on_message
async def on_message(message: cl.Message):
    workspace_id = cl.user_session.get("workspace_id")
    settings = cl.user_session.get("settings") or {}
    reply_ctx = cl.user_session.get("reply_context")
    if reply_ctx:
        message.content = (reply_ctx or "") + "\n" + (message.content or "")
    cl.user_session.set("reply_context", None)
    CHAT.handle_new_message(message, workspace_id, settings)

@cl.on_action
async def on_action(action: cl.Action):
    workspace_id = cl.user_session.get("workspace_id")
    if action.name == ACTION.SELECT_WORKSPACE:
        cl.user_session.set("workspace_id", action.value)
        cl.user_session.set("current_conv_id", None)
        await on_chat_start()
        return
    if action.name == ACTION.MANAGE_WORKSPACES:
        res = await cl.AskActionMessage("نام فضای جدید را وارد کن یا برای حذف انتخاب کن", actions=[cl.Action(ACTION.ADD_WORKSPACE, "➕ افزودن")], inputs=[cl.TextInput("ws_name","نام فضای جدید")]).send()
        if res and res.get("name") == ACTION.ADD_WORKSPACE and res.get("ws_name"):
            await DB.create_workspace(res.get("ws_name"))
            await render_sidebar(workspace_id)
        return
    if action.name == ACTION.REPLY_TO_MESSAGE:
        try:
            doc = await DB.find_one("messages", workspace_id, {"_id": ObjectId(action.value)})
            if doc:
                quoted = f"> {doc.get('role')}: {doc.get('text')}\n\n"
                await cl.Message(content=f"در حال ریپلای به پیام:\n{quoted}\nپیام جدید را ارسال کن.").send()
                cl.user_session.set("reply_context", quoted)
        except Exception:
            await cl.Message("پیام یافت نشد یا خطا در دیتابیس.", author="System").send()
        return
    if action.name == ACTION.NEW_CONV:
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat()
        return
    if action.name == ACTION.SELECT_CONV:
        try:
            conv_id = ObjectId(action.value)
        except Exception:
            conv_id = action.value
        cl.user_session.set("current_conv_id", conv_id)
        await display_history(conv_id, workspace_id)
        return
    if action.name == ACTION.SHOW_NOTES:
        notes = await DB.find("notes", workspace_id, sort=("created_at",-1))
        if not notes:
            await cl.Message("یادداشتی وجود ندارد.", author="Notes").send()
            return
        for n in notes:
            nid = str(n["_id"])
            actions = [cl.Action(name=ACTION.EDIT_NOTE, value=nid, label="✏️ ویرایش"), cl.Action(name=ACTION.DELETE_NOTE, value=nid, label="🗑️ حذف")]
            await cl.Message(content=n.get("content",""), author="Notes", id=nid, actions=actions).send()
        return
    if action.name == ACTION.NEW_NOTE_MODAL:
        res = await cl.AskActionMessage("متن یادداشت را وارد کن", inputs=[cl.TextInput("note_content","متن")], actions=[cl.Action(ACTION.ADD_NOTE,"➕ افزودن")]).send()
        if res and res.get("name") == ACTION.ADD_NOTE and res.get("note_content"):
            await DB.insert_one("notes", workspace_id, {"content": res.get("note_content"), "created_at": datetime.now(timezone.utc)})
            await cl.Message("یادداشت افزوده شد.", author="Notes").send()
        return
    if action.name == ACTION.EDIT_NOTE:
        nid = action.value
        try:
            note_doc = await DB.find_one("notes", workspace_id, {"_id": ObjectId(nid)})
            if note_doc:
                res = await cl.AskActionMessage("ویرایش یادداشت", inputs=[cl.TextInput("note_content","متن", value=note_doc.get("content",""))], actions=[cl.Action("confirm_edit","✔ ذخیره")]).send()
                if res and res.get("name") == "confirm_edit" and res.get("note_content"):
                    await DB.update_one("notes", workspace_id, ObjectId(nid), {"content": res.get("note_content"), "updated_at": datetime.now(timezone.utc)})
                    await cl.Message("یادداشت بروزرسانی شد.", author="Notes").send()
        except Exception:
            await cl.Message("خطا در یافتن یادداشت.", author="Notes").send()
        return
    if action.name == ACTION.DELETE_NOTE:
        nid = action.value
        try:
            await DB.delete_one("notes", workspace_id, ObjectId(nid))
            await cl.Message("یادداشت حذف شد.", author="Notes").send()
        except Exception:
            await cl.Message("خطا در حذف یادداشت.", author="Notes").send()
        return
