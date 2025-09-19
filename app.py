import os
import uuid
import json
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# Chainlit
import chainlit as cl

# Google GenAI modern SDK
from google import genai
from google.genai import types as genai_types

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
    raise RuntimeError("GEMINI_API_KEY در .env تنظیم نشده است.")
if not CFG.TAVILY_API_KEY:
    logging.warning("TAVILY_API_KEY در .env تنظیم نشده است — ابزار جستجو غیرفعال خواهد بود.")

# Configure genai client
GENAI_CLIENT = genai.Client(api_key=CFG.GEMINI_API_KEY)

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
            raise RuntimeError("MONGO_URI در .env تنظیم نشده است.")
        try:
            self.client = AsyncIOMotorClient(CFG.MONGO_URI, serverSelectionTimeoutMS=5000)
            await self.client.admin.command("ping")
            self.db = self.client[CFG.DB_NAME]
            logger.info("✅ MongoDB connected")
        except Exception as e:
            logger.exception("MongoDB connection failed")
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
        ws_id = str(uuid.uuid4())
        await col.insert_one({"_id": ws_id, "name": name, "user_id": CFG.USER_ID, "created_at": datetime.now(timezone.utc)})
        return ws_id

    async def delete_workspace(self, workspace_id: str):
        for c in ["conversations", "messages", "notes", "custom_tools"]:
            await self._collection(c).delete_many({"workspace_id": workspace_id})
        await self._collection("workspaces").delete_one({"_id": workspace_id})
        logger.warning("Workspace %s deleted", workspace_id)

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
    def __init__(self):
        self._tavily = None
        if TAVILY_AVAILABLE and CFG.TAVILY_API_KEY:
            try:
                self._tavily = TavilyClient(CFG.TAVILY_API_KEY)
            except Exception:
                logger.exception("Tavily init failed")
                self._tavily = None

    def get_tool_declarations(self) -> List[genai_types.FunctionDeclaration]:
        return [
            genai_types.FunctionDeclaration(
                name="generate_image",
                description="Generate image from text prompt",
                parameters={
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                },
            ),
            genai_types.FunctionDeclaration(
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
            genai_types.FunctionDeclaration(
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
        logger.debug("Executing tool %s with %s", tool_name, kwargs)
        if tool_name == "generate_image":
            return await self._generate_image(kwargs.get("prompt", ""))
        if tool_name == "display_table":
            return await self._display_table(kwargs.get("json_data", "[]"), kwargs.get("title", "Data"))
        if tool_name == "search":
            return await self._search(kwargs.get("query", ""))
        return {"status": "error", "error": f"Unknown tool {tool_name}"}

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
            logger.exception("Image tool failed")
            await m.update(content=f"خطا در تولید تصویر: {e}")
            return {"status": "error", "error": str(e)}

    async def _display_table(self, json_data: str, title: str = "Data"):
        try:
            data = json.loads(json_data)
            if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                return {"status": "error", "error": "Invalid JSON: must be list of objects"}
            df = pd.DataFrame(data)
            md = f"### {title}\n\n" + df.to_markdown(index=False)
            await cl.Message(content=md, author="Table").send()
            return {"status": "ok", "text": md}
        except Exception as e:
            logger.exception("Table tool failed")
            return {"status": "error", "error": str(e)}

    async def _search(self, query: str):
        m = cl.Message(content=f"در حال جستجو برای `{query}` ...", author="System")
        await m.send()
        if not self._tavily:
            err = "Tavily client not available — نصب tavily-python و تنظیم TAVILY_API_KEY لازم است."
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
            logger.exception("Search tool failed")
            await m.update(content=f"خطا در جستجو: {e}")
            return {"status": "error", "error": str(e)}

TOOLS = CoreToolsPlugin()

# -------------------- Chat Manager --------------------
class ChatManager:
    def __init__(self, db: Database):
        self.db = db
        self.tools = TOOLS
        self.queue = asyncio.Queue()
        self._task = asyncio.create_task(self._worker())

    async def _worker(self):
        while True:
            message, workspace_id, settings = await self.queue.get()
            try:
                await self._process_message(message, workspace_id, settings)
            except Exception as e:
                logger.exception("Processing message failed")
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
            conv_id = getattr(res, "inserted_id", None) or (res.inserted_id if hasattr(res, "inserted_id") else None)
            if not conv_id:
                conv_id = str(uuid.uuid4())
                await self.db._collection("conversations").insert_one({"_id": conv_id, "title": title, "workspace_id": workspace_id, "user_id": CFG.USER_ID, "created_at": datetime.now(timezone.utc)})
            cl.user_session.set("current_conv_id", conv_id)

        await self.db.insert_one("messages", workspace_id, {
            "_id": ObjectId(),
            "conv_id": conv_id,
            "role": "user",
            "text": message.content or "",
            "created_at": datetime.now(timezone.utc)
        })

        await display_history(conv_id, workspace_id)

        msgs = await self.db.find("messages", workspace_id, {"conv_id": conv_id}, sort=("created_at", 1), limit=300)
        formatted = []
        for m in msgs:
            # Using genai_types.Content & Part for typed history (SDK-specific)
            try:
                part = genai_types.Part(text=m.get("text", ""))
                content = genai_types.Content(role="assistant" if m.get("role") == "assistant" else "user", parts=[part])
                formatted.append(content)
            except Exception:
                # Fallback: plain strings if types unavailable
                formatted.append(genai_types.Part(text=m.get("text", "")))

        model_id = (settings or {}).get("model_id") or "gemini-1.5-pro-latest"

        # call model under semaphore
        async with MODEL_SEMAPHORE:
            try:
                # Modern client: models.generate_content (synchronous API)
                response = GENAI_CLIENT.models.generate_content(
                    model=model_id,
                    contents=formatted + [genai_types.Part(text=message.content or "")],
                    tools=self.tools.get_tool_declarations()
                )
            except Exception as e:
                logger.exception("GenAI generate_content failed")
                await cl.Message(content=f"خطا در تماس با مدل: {e}", author="System").send()
                return

        # extract tool calls (SDK may vary)
        tool_calls = []
        try:
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                for cand in candidates:
                    parts = getattr(cand.content, "parts", []) or []
                    for p in parts:
                        fcs = getattr(p, "function_calls", None) or []
                        if fcs:
                            tool_calls.extend(fcs)
        except Exception:
            tool_calls = []

        if not tool_calls:
            text = getattr(response, "text", "") or ""
            await self._stream_and_save(conv_id, workspace_id, text)
            return

        # execute tool calls sequentially/safely and collect results
        results = []
        for tc in tool_calls:
            try:
                name = getattr(tc, "name", None) or tc.get("name")
                args = getattr(tc, "args", None) or tc.get("args", {})
                res = await self._safe_tool_execute(name, args)
                results.append((tc, res))
            except Exception as e:
                logger.exception("Tool execution loop error")
                results.append((tc, {"status":"error","error":str(e)}))

        # build function response parts
        func_parts = []
        for tc, res in results:
            try:
                fr = genai_types.FunctionResponse(name=getattr(tc, "name", tc.get("name")), response={"content": str(res)})
                part = genai_types.Part(function_response=fr)
                func_parts.append(part)
            except Exception:
                # fallback: simple part containing text
                try:
                    part = genai_types.Part(text=str(res))
                    func_parts.append(part)
                except Exception:
                    func_parts.append(genai_types.Part(text=str(res)))

        # send function responses back to model to get final reply
        async with MODEL_SEMAPHORE:
            try:
                final = GENAI_CLIENT.models.generate_content(
                    model=model_id,
                    contents=func_parts,
                    tools=self.tools.get_tool_declarations()
                )
            except Exception as e:
                logger.exception("Final genai call failed")
                await cl.Message(content=f"خطا در دریافت پاسخ نهایی: {e}", author="System").send()
                return

        final_text = getattr(final, "text", "") or ""
        await self._stream_and_save(conv_id, workspace_id, final_text)

    async def _safe_tool_execute(self, name: str, kwargs: Dict):
        try:
            res = await asyncio.wait_for(self.tools.execute(name, **(kwargs or {})), timeout=30)
            return res
        except asyncio.TimeoutError:
            logger.exception("Tool %s timed out", name)
            return {"status":"error","error":"timeout"}
        except Exception as e:
            logger.exception("Tool %s exception", name)
            return {"status":"error","error": str(e)}

    async def _stream_and_save(self, conv_id: Any, workspace_id: str, text: str):
        msg = cl.Message(content="", author=CFG.VERSION)
        await msg.send()
        try:
            await msg.stream_token(text)
        except Exception:
            await msg.update(content=text)
        await self.db.insert_one("messages", workspace_id, {
            "_id": ObjectId(),
            "conv_id": conv_id,
            "role": "assistant",
            "text": text,
            "created_at": datetime.now(timezone.utc)
        })

CHAT = ChatManager(DB)

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
    messages = await DB.find("messages", workspace_id, {"conv_id": conv_id}, sort=("created_at", 1), limit=300)
    for m in messages:
        author = CFG.VERSION if m.get("role") == "assistant" else "User"
        msg_id = str(m["_id"])
        actions = [cl.Action(name=ACTION.REPLY_TO_MESSAGE, value=msg_id, label="ریپلای")]
        await cl.Message(content=m.get("text",""), author=author, id=msg_id, actions=actions).send()

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
        logger.debug("Avatar not found (non-fatal).")
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
