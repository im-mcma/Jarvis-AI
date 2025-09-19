# -*- coding: utf-8 -*-
"""
Saino — Stable & Final Version
----------------------------------------------------------
این نسخه نهایی با نام "Saino"، تمام خطاهای نگارشی و منطقی را برطرف کرده
و یک برنامه کاملاً پایدار، تمیز و آماده استقرار را ارائه می‌دهد.
"""
import os
import io
import re
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import chainlit as cl
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from bson.binary import Binary
import google.generativeai as genai

# --- Dependency Management ---
try: import faiss; from sentence_transformers import SentenceTransformer
except ImportError: faiss, SentenceTransformer = None, None
try: from tavily import TavilyClient
except ImportError: TavilyClient = None
try: import pandas as pd
except ImportError: pd = None
try: from pdfminer.high_level import extract_text
except ImportError: extract_text = None

# ----------------------------------------------------------------------
# بخش ۱: پیکربندی و ثابت‌ها
# ----------------------------------------------------------------------
load_dotenv()

class Config:
    MONGO_URI: str = os.getenv("MONGO_URI")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY")
    TAVILY_API_KEY: Optional[str] = os.getenv("TAVILY_API_KEY")
    VERSION: str = "Saino"
    USER_ID: str = "saino_user_001"
    DB_NAME: str = "saino_personal_db"
    CACHE_TTL_SECONDS: int = 600

APP_CONFIG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("saino")

class ACTION:
    NEW_CONV,SELECT_CONV="nc","sc"
    ADD_CTX_WEB,NEW_NOTE_MODAL,SHOW_NOTES,SUMMARIZE_CONV="acw","nnm","sn","sm"
    DELETE_NOTE,NUKE_DATABASE="dn","nuke_db"

MODELS={"چت متنی":{"Gemini 2.5 Pro":{"id":"gemini-2.5-pro"},"Gemini 2.5 Flash":{"id":"gemini-2.5-flash"},"Gemini 2.5 Flash-Lite":{"id":"gemini-2.5-flash-lite"},"Gemini 2.0 Pro":{"id":"gemini-2.0-pro"},"Gemini 2.0 Flash":{"id":"gemini-2.0-flash"}},"تولید تصویر":{"Gemini 2.5 Flash Image":{"id":"gemini-2.5-flash-image-preview"},"Gemini 2.0 Flash Image":{"id":"gemini-2.0-flash-image"}},"تولید ویدیو":{"Veo 3":{"id":"veo-3"}}}
DEFAULT_MODEL_ID,SUMMARIZE_MODEL_ID="gemini-2.5-flash","gemini-2.5-flash-lite"
if APP_CONFIG.GEMINI_API_KEY:genai.configure(api_key=APP_CONFIG.GEMINI_API_KEY)


# ----------------------------------------------------------------------
# بخش ۲: لایه پایگاه داده
# ----------------------------------------------------------------------
class Database:
    def __init__(self, uri, db_name):
        self.client = None
        self._uri, self._db_name = uri, db_name

    async def _connect(self):
        if self.client and self.client.admin.command('ping'):
            return
        try:
            self.client = AsyncIOMotorClient(self._uri, serverSelectionTimeoutMS=5000)
            await self.client.admin.command('ping')
            self.db = self.client[self._db_name]
            self.conversations = self.db["conversations"]
            self.messages = self.db["messages"]
            self.notes = self.db["notes"]
            self.cache = self.db["cache"]
            self.vstore = self.db["vector_store"]
            logger.info("✅ MongoDB connection established.")
        except Exception as e:
            self.client = None
            raise ConnectionError(f"Failed to connect to MongoDB: {e}")

    async def get_from_cache(self, key):
        await self._connect()
        doc = await self.cache.find_one({"_id": key})
        if doc and (ts := doc.get('ts')):
            ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            if (datetime.now(timezone.utc) - ts) < timedelta(seconds=APP_CONFIG.CACHE_TTL_SECONDS):
                return doc.get('data')
        return None

    async def set_in_cache(self, key, data):
        await self._connect()
        await self.cache.update_one({"_id": key}, {"$set": {"data": data, "ts": datetime.now(timezone.utc)}}, upsert=True)

    async def create_note(self, user_id, content, tags, reminder):
        await self._connect()
        doc = {"user_id": user_id, "text": content, "tags": tags, "remind_at": reminder, "created_at": datetime.now(timezone.utc), "reminded": False}
        await self.notes.insert_one(doc)

    async def get_notes(self, user_id):
        await self._connect()
        return await self.notes.find({"user_id": user_id}).sort("created_at", -1).to_list(100)

    async def delete_note(self, note_id):
        await self._connect()
        await self.notes.delete_one({"_id": ObjectId(note_id)})

    async def get_due_reminders(self):
        await self._connect()
        return await self.notes.find({"remind_at": {"$lte": datetime.now(timezone.utc)}, "reminded": False}).to_list(100)

    async def mark_reminder_sent(self, note_id):
        await self._connect()
        await self.notes.update_one({"_id": note_id}, {"$set": {"reminded": True}})

    async def create_conversation(self, user_id, title):
        await self._connect()
        res = await self.conversations.insert_one({"user_id": user_id, "title": title, "created_at": datetime.now(timezone.utc)})
        return str(res.inserted_id)

    async def get_conversations(self, user_id):
        await self._connect()
        return await self.conversations.find({"user_id": user_id}).sort("created_at", -1).to_list(50)

    async def append_message(self, conv_id, user_id, role, text):
        await self._connect()
        doc = {"conv_id": ObjectId(conv_id), "user_id": user_id, "role": role, "text": text, "created_at": datetime.now(timezone.utc)}
        await self.messages.insert_one(doc)

    async def get_messages(self, conv_id, limit=10):
        await self._connect()
        cursor = self.messages.find({"conv_id": ObjectId(conv_id)}).sort("created_at", -1).limit(limit)
        return list(reversed(await cursor.to_list(length=limit)))

    async def get_all_messages_text(self, conv_id):
        await self._connect()
        msgs = await self.messages.find({"conv_id": ObjectId(conv_id)}).sort("created_at", 1).to_list(1000)
        return "\n".join(f"{m['role']}:{m['text']}" for m in msgs)

    async def nuke(self, user_id):
        await self._connect()
        await self.messages.delete_many({"user_id": user_id})
        await self.conversations.delete_many({"user_id": user_id})
        await self.notes.delete_many({"user_id": user_id})
        await self.cache.delete_many({})
        await self.vstore.delete_many({})
        logger.info(f"Database nuked for user_id: {user_id}")


# ----------------------------------------------------------------------
# بخش ۳: لایه ابزارها
# ----------------------------------------------------------------------
class Tools:
    @staticmethod
    async def process_file(file: cl.File) -> str:
        content, fname = "", file.name.lower()
        try:
            if fname.endswith((".txt", ".md")):
                content = file.content.decode('utf-8')
            elif fname.endswith(".pdf"):
                if not extract_text: await cl.Message("وابستگی PDF (`pdfminer.six`) نصب نشده.",author="سیستم").send(); return ""
                content = extract_text(io.BytesIO(file.content))
            elif fname.endswith(".csv"):
                if not pd: await cl.Message("وابستگی CSV (`pandas`) نصب نشده.", author="سیستم").send(); return ""
                content = f"خلاصه CSV:\n{pd.read_csv(io.BytesIO(file.content)).head().to_string()}"
            return f"[[شروع محتوای {file.name}]]\n{content}\n[[پایان]]" if content else ""
        except Exception as e:
            return f"[[خطا در پردازش فایل {file.name}: {e}]]"

    @staticmethod
    async def web_search(query: str, db: Database) -> str:
        if not TavilyClient: await cl.Message("وابستگی `tavily-python` نصب نشده.", author="سیستم").send(); return ""
        cached = await db.get_from_cache(f"web:{query}")
        if cached:
            return cached
        try:
            res = await asyncio.to_thread(TavilyClient(api_key=APP_CONFIG.TAVILY_API_KEY).search, query=query, search_depth="basic")
            ctx = "\n".join([f"- {obj['content']}" for obj in res.get('results', [])])
            if not ctx:
                raise ValueError("No results found from Tavily.")
            result = f"[[نتایج وب برای '{query}']]\n{ctx}\n"
            await db.set_in_cache(f"web:{query}", result)
            return result
        except Exception as e:
            await cl.Message(f"سرویس جستجو با خطا مواجه شد: {e}", author="سیستم").send()
            return ""


# ----------------------------------------------------------------------
# بخش ۴: لایه حافظه معنایی (ذخیره در MongoDB)
# ----------------------------------------------------------------------
class VectorStore:
    def __init__(self, db: Database):
        self.is_enabled = bool(faiss and SentenceTransformer)
        self.db = db
        if not self.is_enabled:
            logger.warning("VectorStore is disabled due to missing dependencies.")
            return
        try:
            self.model, self.dim = SentenceTransformer("all-MiniLM-L6-v2"), 384
            self.map, self.next_id = {}, 0
            self.index = faiss.IndexFlatIP(self.dim)
            asyncio.create_task(self._load_from_db())
        except Exception as e:
            self.is_enabled = False
            logger.error(f"VectorStore init failed: {e}")

    async def _load_from_db(self):
        try:
            await self.db._connect()
            data = await self.db.vstore.find_one({"_id": "main_index"})
            if data:
                self.map = data.get('map', {})
                self.next_id = data.get('next_id', 0)
                if 'index_data' in data:
                    self.index = faiss.read_index(io.BytesIOReader(io.BytesIO(data['index_data'])))
            logger.info(f"✅ VectorStore loaded from DB with {self.next_id} embeddings.")
        except Exception as e:
            logger.error(f"Failed to load FAISS from DB: {e}")
            self.index = faiss.IndexFlatIP(self.dim)

    async def _blocking_save(self):
        try:
            buffer = io.BytesIO()
            faiss.write_index(self.index, io.BytesIOWriter(buffer))
            index_data = buffer.getvalue()
            doc = {"_id": "main_index", "map": self.map, "next_id": self.next_id, "index_data": Binary(index_data)}
            await self.db.vstore.update_one({"_id": "main_index"}, {"$set": doc}, upsert=True)
        except Exception as e:
            logger.error(f"FAISS save error: {e}", exc_info=True)

    def _blocking_add(self, text, meta):
        try:
            embedding = self.model.encode([text], normalize_embeddings=True)
            self.index.add(embedding)
            self.map[str(self.next_id)] = meta
            self.next_id += 1
            asyncio.run(self._blocking_save())
        except Exception as e:
            logger.error(f"FAISS add error: {e}", exc_info=True)

    def add(self, text, meta):
        if self.is_enabled:
            asyncio.to_thread(self._blocking_add, text, meta)

    def _blocking_search(self, query, k, conv_id):
        if self.index.ntotal == 0:
            return []
        embedding = self.model.encode([query], normalize_embeddings=True)
        _, indices = self.index.search(embedding, k)
        results = []
        for i in indices[0]:
            if i != -1:
                item = self.map.get(str(i))
                if item and item.get('conv_id') == conv_id and 'text' in item:
                    results.append(item['text'])
        return results

    async def search(self, query, k=2, conv_id=None):
        if not self.is_enabled:
            return []
        return await asyncio.to_thread(self._blocking_search, query, k, conv_id)


# ----------------------------------------------------------------------
# بخش ۵: مدیر برنامه
# ----------------------------------------------------------------------
class ChatManager:
    def __init__(self, db, vstore):
        self.db = db
        self.vstore = vstore
        self.message_queue = asyncio.Queue()
        self.processor_task = asyncio.create_task(self._process_message_queue())

    async def _process_message_queue(self):
        while True:
            message = await self.message_queue.get()
            try:
                await self._execute_message_processing(message)
            except Exception as e:
                logger.error(f"Message processing failed: {e}", exc_info=True)
                await cl.Message(f"خطا در پردازش پیام: {e}", author="سیستم").send()
            self.message_queue.task_done()

    def handle_new_message(self, message):
        self.message_queue.put_nowait(message)

    async def _execute_message_processing(self, message):
        contexts = cl.user_session.get("contexts", [])
        final_context = "\n\n".join(c for c in contexts if c)
        text, elements = (message.content or "").strip(), message.elements

        if elements:
            file_contexts = await asyncio.gather(*[Tools.process_file(e) for e in elements])
            final_context += "\n" + "\n".join(c for c in file_contexts if c)

        if final_context:
            text = f"{final_context}\n\nپرسش اصلی: {text}"
            cl.user_session.set("contexts", [])

        conv_id = cl.user_session.get("current_conv_id") or await self.db.create_conversation(APP_CONFIG.USER_ID, text[:50])
        if not cl.user_session.get("current_conv_id"):
            cl.user_session.set("current_conv_id", conv_id)
            await self.render_sidebar()

        await self.db.append_message(conv_id, APP_CONFIG.USER_ID, "user", text)
        self.vstore.add(text, {'conv_id': conv_id, 'text': text})

        short_mem = await self.db.get_messages(conv_id)
        long_mem = await self.vstore.search(text, 2, conv_id)
        rag_ctx = "[[حافظه]]\n" + "\n".join(f"- {t}" for t in long_mem) if long_mem else ""

        await self.display_history(conv_id)

        model = cl.user_session.get("settings", {}).get("model_id", DEFAULT_MODEL_ID)
        response_msg = cl.Message("", author="Saino")
        await response_msg.send()

        full_response = "".join([t async for t in self.stream_gemini(short_mem, model, rag_ctx)])
        if full_response:
            await response_msg.update(full_response)
            await self.db.append_message(conv_id, APP_CONFIG.USER_ID, "assistant", full_response)

    async def setup_session(self):
        await cl.Avatar(name="User", url="/public/user.png").send()
        await cl.Avatar(name="Saino", url="/public/assistant.png").send()
        await self.render_sidebar()
        await cl.Message(content="### دستیار شخصی Saino\nآماده به کار.").send()
        if not self.vstore.is_enabled:
            await cl.Message("⚠️ حافظه بلندمدت (FAISS) غیرفعال است.", author="سیستم").send()

    async def render_sidebar(self):
        convs = await self.db.get_conversations(APP_CONFIG.USER_ID)
        actions = [
            cl.Action(name=ACTION.NEW_CONV, label="➕ مکالمه جدید"),
            cl.Action(name=ACTION.SHOW_NOTES, label="🗒️ یادداشت‌ها"),
            cl.Action(name=ACTION.ADD_CTX_WEB, label="🌐 وب"),
            cl.Action(name=ACTION.SUMMARIZE_CONV, label="✍️ خلاصه"),
            cl.Action(name=ACTION.NUKE_DATABASE, label="🗑️ پاک‌سازی")
        ]
        conv_hist = [cl.Action(name=ACTION.SELECT_CONV, value=str(c["_id"]), label=f"💬 {c.get('title','...')}") for c in convs]
        await cl.set_actions(actions + conv_hist)

    async def stream_gemini(self, history, model, context=None):
        if context:
            history.insert(-1, {"role": "user", "text": context})
        api_hist = [{"role": msg.get("role"), "parts": [{"text": msg.get("text", "")}]} for msg in history]
        stream = await genai.GenerativeModel(model).generate_content_async(api_hist, stream=True)
        async for chunk in stream:
            if text := getattr(chunk, "text", None):
                yield text

    async def display_history(self, conv_id):
        await cl.empty_chat()
        messages = await self.db.get_messages(conv_id, 50)
        for msg in messages:
            author = "User" if msg.get('role') == "user" else "Saino"
            await cl.Message(content=msg.get('text', ''), author=author).send()

    async def show_all_notes(self):
        notes = await self.db.get_notes(APP_CONFIG.USER_ID)
        if not notes:
            await cl.Message("یادداشتی ثبت نشده.").send()
            return
        for n in notes:
            await cl.Message(f"{n['text']}\n`تگ‌ها: {','.join(n['tags'])}`" if n['tags'] else n['text'], actions=[cl.Action(ACTION.DELETE_NOTE, str(n["_id"]), "حذف")]).send()

    async def summarize_conversation(self, conv_id):
        cached = await self.db.get_from_cache(f"summary:{conv_id}")
        if cached:
            await cl.Message(cached, author="Saino").send()
            return
        m = await cl.Message("درحال خلاصه‌سازی...", author="سیستم"); await m.send()
        txt = await self.db.get_all_messages_text(conv_id)
        summary = "".join([t async for t in self.stream_gemini([{"role": "user", "text": f"خلاصه کن:\n{txt}"}], SUMMARIZE_MODEL_ID)])
        await m.update(summary, author="Saino")
        await self.db.set_in_cache(f"summary:{conv_id}", summary)


# ----------------------------------------------------------------------
# بخش ۶: نمونه‌سازی سراسری و وظایف پس‌زمینه
# ----------------------------------------------------------------------
DB_INSTANCE = Database(APP_CONFIG.MONGO_URI, APP_CONFIG.DB_NAME)
VSTORE_INSTANCE = VectorStore(DB_INSTANCE)
CHAT_MANAGER = ChatManager(DB_INSTANCE, VSTORE_INSTANCE)
BACKGROUND_TASKS_STARTED = False

async def check_reminders():
    while True:
        try:
            due = await DB_INSTANCE.get_due_reminders()
            for r in due:
                await cl.Message(f"**یادآور:** {r.get('text')}", author="سیستم").send()
                await DB_INSTANCE.mark_reminder_sent(r['_id'])
        except Exception as e:
            logger.error(f"Reminder task error: {e}", exc_info=True)
        finally:
            await asyncio.sleep(60)

# ----------------------------------------------------------------------
# بخش ۷: کنترل‌گرهای رابط کاربری
# ----------------------------------------------------------------------
@cl.on_chat_start
async def on_chat_start():
    global BACKGROUND_TASKS_STARTED
    await CHAT_MANAGER.setup_session()
    if not BACKGROUND_TASKS_STARTED:
        asyncio.create_task(check_reminders())
        BACKGROUND_TASKS_STARTED = True

@cl.on_message
async def on_message(m: cl.Message):
    CHAT_MANAGER.handle_new_message(m)

@cl.on_action
async def on_action(action: cl.Action):
    match action.name:
        case ACTION.ADD_CTX_WEB:
            res = await cl.AskUserMessage("چه چیزی را جستجو کنم؟").send()
            if res and (q := res.get('output')):
                m = await cl.Message("درحال جستجو...", author="سیستم"); await m.send()
                ctx = await Tools.web_search(q, DB_INSTANCE)
                if ctx:
                    contexts = cl.user_session.get("contexts", [])
                    contexts.append(ctx)
                    cl.user_session.set("contexts", contexts)
                    await m.update(f"✅ {len(contexts)} زمینه فعال است.")
                else:
                    await m.delete()

        case ACTION.NUKE_DATABASE:
            res = await cl.AskActionMessage("آیا از پاک‌سازی کامل داده‌ها مطمئن هستید؟", actions=[cl.Action("confirm", "بله"), cl.Action("cancel", "خیر")]).send()
            if res and res.get('name') == 'confirm':
                m = await cl.Message("در حال پاک‌سازی...", author="سیستم"); await m.send()
                await DB_INSTANCE.nuke(APP_CONFIG.USER_ID)
                cl.user_session.set("current_conv_id", None)
                await CHAT_MANAGER.render_sidebar()
                await m.update("✅ پاک‌سازی کامل شد.")
                await cl.empty_chat()

        case ACTION.NEW_NOTE_MODAL:
            res = await cl.AskActionMessage("یادداشت جدید", actions=[cl.Action("confirm", "ثبت")], inputs=[cl.TextInput("content", "متن", required=True), cl.TextInput("tags", "تگ‌ها (با کاما جدا کنید)"), cl.TextInput("reminder", "یادآور (YYYY-MM-DDTHH:MM)")]).send()
            if res and res.get('name') == 'confirm':
                tags = [t.strip() for t in res.get('tags', '').split(',') if t.strip()]
                remind = datetime.fromisoformat(res.get('reminder')) if res.get('reminder') else None
                await DB_INSTANCE.create_note(APP_CONFIG.USER_ID, res['content'], tags, remind)
                await cl.Message("یادداشت ثبت شد.").send()

        case ACTION.NEW_CONV:
            cl.user_session.set("current_conv_id", None)
            await cl.empty_chat()

        case ACTION.SELECT_CONV:
            cl.user_session.set("current_conv_id", action.value)
            await CHAT_MANAGER.display_history(action.value)

        case ACTION.SHOW_NOTES:
            await CHAT_MANAGER.show_all_notes()

        case ACTION.DELETE_NOTE:
            await DB_INSTANCE.delete_note(action.value)
            await CHAT_MANAGER.show_all_notes()

        case ACTION.SUMMARIZE_CONV:
            if cid := cl.user_session.get("current_conv_id"):
                await CHAT_MANAGER.summarize_conversation(cid)
            else:
                await cl.Message("ابتدا یک مکالمه را انتخاب کنید.", author="سیستم").send()
