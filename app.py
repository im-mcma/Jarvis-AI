#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Saino Elite - single-file operational version
Requirements:
  - Put a .env file with MONGO_URI and GEMINI_API_KEY
  - pip install chainlit motor python-dotenv google-generativeai pandas aiohttp
Run:
  chainlit run saino_elite.py
"""

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

import chainlit as cl
import google.generativeai as genai
from google.generativeai.types import Tool, FunctionDeclaration, Part

import pandas as pd

# -------------------- Config & Logging --------------------
load_dotenv()

@dataclass
class Config:
    MONGO_URI: str = os.getenv("MONGO_URI", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    VERSION: str = "Saino Elite"
    USER_ID: str = "saino_user_001"
    DB_NAME: str = "saino_elite_db"

CFG = Config()
if not CFG.GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is required in .env")

genai.configure(api_key=CFG.GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("saino")

# -------------------- Actions --------------------
class ACTION:
    NEW_CONV = "nc"
    SELECT_CONV = "sc"
    OPEN_SETTINGS = "os"
    NUKE_DATABASE = "nuke_db"
    SHOW_NOTES = "sn"
    NEW_NOTE_MODAL = "nnm"
    EDIT_NOTE = "en"
    DELETE_NOTE = "dn"
    MANAGE_TOOLS = "mt"
    MANAGE_WORKSPACES = "mw"
    ADD_WORKSPACE = "aw"
    SELECT_WORKSPACE = "sw"
    REPLY_TO_MESSAGE = "rtm"
    ADD_NOTE = "an"

# -------------------- Simple DB wrapper --------------------
class Database:
    _instance = None

    def __new__(cls, *a, **k):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def connect(self):
        if hasattr(self, "client") and getattr(self, "client", None):
            return
        try:
            if not CFG.MONGO_URI:
                raise RuntimeError("MONGO_URI not set in .env")
            self.client = AsyncIOMotorClient(CFG.MONGO_URI, serverSelectionTimeoutMS=5000)
            await self.client.admin.command("ping")
            self.db = self.client[CFG.DB_NAME]
            logger.info("✅ MongoDB connected")
        except Exception as e:
            logger.exception("MongoDB connection failed")
            raise

    def _collection(self, name: str):
        return self.db[name]

    # workspaces
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
        # delete docs in workspace
        for c in ["conversations", "messages", "notes", "custom_tools"]:
            await self._collection(c).delete_many({"workspace_id": workspace_id})
        await self._collection("workspaces").delete_one({"_id": workspace_id})
        logger.warning("Workspace %s deleted", workspace_id)

    # generic helpers
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
        document = dict(document)  # copy
        document["workspace_id"] = workspace_id
        document["user_id"] = CFG.USER_ID
        if "created_at" not in document:
            document["created_at"] = datetime.now(timezone.utc)
        res = await self._collection(collection).insert_one(document)
        return res

    async def update_one(self, collection: str, workspace_id: str, doc_id: Any, update_data: Dict):
        return await self._collection(collection).update_one({"_id": doc_id, "workspace_id": workspace_id}, {"$set": update_data})

    async def delete_one(self, collection: str, workspace_id: str, doc_id: Any):
        return await self._collection(collection).delete_one({"_id": doc_id, "workspace_id": workspace_id})

DB = Database()

# -------------------- Core Tools Plugin --------------------
class CoreToolsPlugin:
    def get_tool_declarations(self) -> List[FunctionDeclaration]:
        return [
            FunctionDeclaration(
                name="generate_image",
                description="Generate image from text prompt",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"}
                    },
                    "required": ["prompt"]
                }
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
                    "required": ["json_data"]
                }
            )
        ]

    async def execute(self, tool_name: str, **kwargs):
        if tool_name == "generate_image":
            return await self._generate_image(kwargs.get("prompt", ""))
        if tool_name == "display_table":
            return await self._display_table(kwargs.get("json_data", "[]"), kwargs.get("title", "Data"))
        return f"Unknown tool: {tool_name}"

    async def _generate_image(self, prompt: str):
        # Minimal user-facing feedback via Chainlit; real implementation should use model image API
        msg = cl.Message(content=f"در حال تولید تصویر برای: `{prompt}`...", author="System")
        await msg.send()
        try:
            # Try to call Gemini image model (placeholder flow). Actual API shapes may differ.
            # For stability, return a placeholder image shown to user.
            url_safe = prompt.replace(" ", "+")[:120]
            placeholder = f"https://placehold.co/512x512/222/fff?text={url_safe}"
            image = cl.Image(url=placeholder, name=prompt, display="inline")
            await msg.update(content=f"تصویر تولید شد برای: `{prompt}`", elements=[image])
            return "Image displayed (placeholder)."
        except Exception as e:
            logger.exception("Image tool failed")
            await msg.update(content=f"خطا در تولید تصویر: {e}")
            return f"Image tool failed: {e}"

    async def _display_table(self, json_data: str, title: str = "Data"):
        try:
            data = json.loads(json_data)
            if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                return "Invalid JSON: must be list of objects"
            df = pd.DataFrame(data)
            md = f"### {title}\n\n" + df.to_markdown(index=False)
            await cl.Message(content=md, author="Table").send()
            return "Table displayed."
        except Exception as e:
            logger.exception("Table tool failed")
            return f"Table tool failed: {e}"

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
            message: cl.Message
            workspace_id: str
            settings: Dict
            message, workspace_id, settings = await self.queue.get()
            try:
                await self._process_message(message, workspace_id, settings)
            except Exception as e:
                logger.exception("Processing message failed")
                await cl.Message(content=f"خطا داخلی: {e}", author="System").send()
            finally:
                self.queue.task_done()

    def handle_new_message(self, message: cl.Message, workspace_id: str, settings: Dict):
        # enqueue quickly
        self.queue.put_nowait((message, workspace_id, settings))

    async def _process_message(self, message: cl.Message, workspace_id: str, settings: Dict):
        # ensure conversation exists
        conv_id = cl.user_session.get("current_conv_id")
        if not conv_id:
            # create conversation doc
            title = (message.content or "")[:120] or "مکالمه جدید"
            res = await self.db.insert_one("conversations", workspace_id, {"title": title, "created_at": datetime.now(timezone.utc)})
            conv_id = res.inserted_id
            cl.user_session.set("current_conv_id", conv_id)

        # save user message
        await self.db.insert_one("messages", workspace_id, {
            "_id": ObjectId(),  # generate explicit ObjectId for consistent queries
            "conv_id": conv_id,
            "role": "user",
            "text": message.content,
            "created_at": datetime.now(timezone.utc)
        })

        # show history to user
        await display_history(conv_id, workspace_id)

        # prepare history for model
        msgs = await self.db.find("messages", workspace_id, {"conv_id": conv_id}, sort=("created_at", 1), limit=200)
        formatted = []
        for m in msgs:
            role = "assistant" if m.get("role") == "assistant" else "user"
            formatted.append({"role": role, "parts": [{"text": m.get("text","")} ]})

        # prepare model with tools
        model_id = settings.get("model_id", genai.DEFAULT_MODEL) if settings else None
        if not model_id:
            model_id = genai.DEFAULT_MODEL if hasattr(genai, "DEFAULT_MODEL") else CFG.GEMINI_API_KEY  # fallback weirdness
        model = genai.GenerativeModel(settings.get("model_id", "gemini-1.5-pro-latest"),
                                     tools=[Tool(function_declarations=self.tools.get_tool_declarations())])

        history_for_chat = formatted[:-1] if len(formatted) > 1 else []
        if history_for_chat:
            chat = model.start_chat(history=history_for_chat)
        else:
            chat = model.start_chat()

        try:
            last_text = formatted[-1]["parts"][0]["text"] if formatted else message.content
            response = await chat.send_message_async(last_text)
        except Exception as e:
            logger.exception("Model send failed")
            await cl.Message(content=f"خطا در تماس با مدل: {e}", author=CFG.VERSION).send()
            return

        # examine tool calls (best-effort)
        tool_calls = []
        try:
            tool_calls = response.candidates[0].content.parts[0].function_calls or []
        except Exception:
            tool_calls = []

        if not tool_calls:
            await self._stream_and_save(conv_id, workspace_id, response.text)
            return

        # execute tools in parallel
        tasks = [self.tools.execute(tc.name, **dict(tc.args)) for tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # build function-response parts for the model
        func_parts = []
        for tc, res in zip(tool_calls, results):
            content = res if not isinstance(res, Exception) else f"Tool error: {res}"
            func_parts.append(Part(function_response={"name": tc.name, "response": {"content": str(content)}}))

        try:
            final = await chat.send_message_async(func_parts)
        except Exception as e:
            logger.exception("Final model call failed")
            await cl.Message(content=f"خطا در دریافت پاسخ نهایی: {e}", author=CFG.VERSION).send()
            return

        await self._stream_and_save(conv_id, workspace_id, final.text)

    async def _stream_and_save(self, conv_id: Any, workspace_id: str, text: str):
        msg = cl.Message(content="", author=CFG.VERSION)
        await msg.send()
        # stream_token may not be present in some chainlit versions; fallback to update
        try:
            await msg.stream_token(text)
        except Exception:
            await msg.update(content=text)
        # persist assistant message
        await self.db.insert_one("messages", workspace_id, {
            "_id": ObjectId(),
            "conv_id": conv_id,
            "role": "assistant",
            "text": text,
            "created_at": datetime.now(timezone.utc)
        })

CHAT = ChatManager(DB)

# -------------------- UI & Handlers --------------------
async def render_sidebar(workspace_id: str):
    workspaces = await DB.get_workspaces()
    ws_items = [cl.SelectItem(id=ws["_id"], label=ws["name"]) for ws in workspaces]
    convs = await DB.find("conversations", workspace_id, sort=("created_at", -1), limit=50)
    conv_actions = [cl.Action(name=ACTION.SELECT_CONV, value=str(c["_id"]), label=f"💬 {c.get('title','Untitled')}") for c in convs]
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
    messages = await DB.find("messages", workspace_id, {"conv_id": conv_id}, sort=("created_at", 1), limit=200)
    for m in messages:
        author = CFG.VERSION if m.get("role") == "assistant" else "User"
        msg_id = str(m["_id"])
        actions = [cl.Action(name=ACTION.REPLY_TO_MESSAGE, value=msg_id, label="ریپلای")]
        await cl.Message(content=m.get("text",""), author=author, id=msg_id, actions=actions).send()

@cl.on_chat_start
async def on_chat_start():
    # connect DB and initialize workspace/session
    await DB.connect()
    workspaces = await DB.get_workspaces()
    if not workspaces:
        ws_id = await DB.create_workspace("عمومی")
    else:
        ws_id = workspaces[0]["_id"]
    cl.user_session.set("workspace_id", ws_id)
    cl.user_session.set("settings", {"model_id": "gemini-1.5-pro-latest"})
    # avatars (optional files)
    try:
        await cl.Avatar(name="User", path="./public/user.png").send()
        await cl.Avatar(name=CFG.VERSION, path="./public/assistant.png").send()
    except Exception:
        logger.debug("Avatar files not found or chainlit avatar API failed (non-fatal).")
    await render_sidebar(ws_id)
    await cl.Message(content=f"### {CFG.VERSION}\nدر فضای کاری **{ws_id}** آماده به کارم.").send()

@cl.on_message
async def on_message(message: cl.Message):
    workspace_id = cl.user_session.get("workspace_id")
    settings = cl.user_session.get("settings") or {}
    # handle reply_context if present
    reply_ctx = cl.user_session.get("reply_context")
    if reply_ctx:
        message.content = (reply_ctx or "") + "\n" + (message.content or "")
        cl.user_session.set("reply_context", None)
    CHAT.handle_new_message(message, workspace_id, settings)

@cl.on_action
async def on_action(action: cl.Action):
    workspace_id = cl.user_session.get("workspace_id")
    # select workspace from sidebar select
    if action.name == ACTION.SELECT_WORKSPACE:
        cl.user_session.set("workspace_id", action.value)
        cl.user_session.set("current_conv_id", None)
        await on_chat_start()
        return

    if action.name == ACTION.MANAGE_WORKSPACES:
        res = await cl.AskActionMessage("نام فضای جدید را وارد کن یا برای حذف انتخاب کن",
                                         actions=[cl.Action(ACTION.ADD_WORKSPACE, "➕ افزودن")],
                                         inputs=[cl.TextInput("ws_name", "نام فضای جدید")]).send()
        if res and res.get("name") == ACTION.ADD_WORKSPACE and res.get("ws_name"):
            await DB.create_workspace(res.get("ws_name"))
            await render_sidebar(workspace_id)
        return

    if action.name == ACTION.ADD_WORKSPACE:
        return

    if action.name == ACTION.REPLY_TO_MESSAGE:
        # fetch message and set reply context
        try:
            doc = await DB._collection("messages").find_one({"_id": ObjectId(action.value)})
            if doc:
                quoted = f"> {doc.get('role')}: {doc.get('text')}\n\n"
                await cl.Message(content=f"در حال ریپلای به پیام:\n{quoted}\nپیام جدید را ارسال کن.").send()
                cl.user_session.set("reply_context", quoted)
        except Exception:
            await cl.Message("پیام یافت نشد یا مشکل در دیتابیس.", author="System").send()
        return

    if action.name == ACTION.NEW_CONV:
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat()
        return

    if action.name == ACTION.SELECT_CONV:
        # action.value likely is an ObjectId string
        try:
            conv_id = ObjectId(action.value)
        except Exception:
            conv_id = action.value
        cl.user_session.set("current_conv_id", conv_id)
        await display_history(conv_id, workspace_id)
        return

    if action.name == ACTION.SHOW_NOTES:
        notes = await DB.find("notes", workspace_id, sort=("created_at", -1))
        if not notes:
            await cl.Message("یادداشتی وجود ندارد.", author="Notes").send()
            return
        for n in notes:
            nid = str(n["_id"])
            actions = [cl.Action(name=ACTION.EDIT_NOTE, value=nid, label="✏️ ویرایش"),
                       cl.Action(name=ACTION.DELETE_NOTE, value=nid, label="🗑️ حذف")]
            await cl.Message(content=n.get("content",""), author="Notes", id=nid, actions=actions).send()
        return

    if action.name == ACTION.NEW_NOTE_MODAL:
        res = await cl.AskActionMessage("متن یادداشت را وارد کن",
                                         inputs=[cl.TextInput("note_content", "متن")],
                                         actions=[cl.Action(ACTION.ADD_NOTE, "➕ افزودن")]).send()
        if res and res.get("name") == ACTION.ADD_NOTE and res.get("note_content"):
            await DB.insert_one("notes", workspace_id, {"content": res.get("note_content"), "created_at": datetime.now(timezone.utc)})
            await cl.Message("یادداشت افزوده شد.", author="Notes").send()
        return

    if action.name == ACTION.EDIT_NOTE:
        nid = action.value
        try:
            note_doc = await DB._collection("notes").find_one({"_id": ObjectId(nid)})
            if note_doc:
                res = await cl.AskActionMessage("ویرایش یادداشت",
                                                 inputs=[cl.TextInput("note_content", "متن", value=note_doc.get("content",""))],
                                                 actions=[cl.Action("confirm_edit", "✔ ذخیره")]).send()
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

# End of file
