import os
import io
import re
import json
import time
import base64
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import chainlit as cl
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import google.generativeai as genai

# --- Optional & New Dependencies ---
try: import aioredis 
except ImportError: aioredis = None
try: from rq import Queue, job
except ImportError: Queue, job = None, None
try: import redis
except ImportError: redis = None
try: import faiss; from sentence_transformers import SentenceTransformer
except ImportError: faiss, SentenceTransformer = None, None
try: from tavily import TavilyClient
except ImportError: TavilyClient = None
try: import pandas as pd
except ImportError: pd = None
try: from pdfminer.high_level import extract_text
except ImportError: extract_text = None

# ----------------------------------------------------------------------
# بخش ۱: پیکربندی و ثابت‌ها (Configuration & Constants)
# ----------------------------------------------------------------------
load_dotenv()

class Config:
    """ تمام تنظیمات برنامه را به صورت متمرکز نگهداری می‌کند. """
    MONGO_URI: str = os.getenv("MONGO_URI")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY")
    TAVILY_API_KEY: Optional[str] = os.getenv("TAVILY_API_KEY")
    REDIS_URL: Optional[str] = os.getenv("REDIS_URL")
    WORKER_REDIS_URL: Optional[str] = os.getenv("WORKER_REDIS_URL", os.getenv("REDIS_URL"))

    VERSION: str = "10.0.0-refactor"
    USER_ID: str = "argus_nova_user_001"
    
    DB_NAME: str = "argus_nova_personal_db"
    FAISS_INDEX_PATH: str = "faiss_personal_index.bin"
    FAISS_META_PATH: str = "faiss_personal_meta.json"
    CACHE_TTL_SECONDS: int = 600

    def __post_init__(self):
        if not self.MONGO_URI or not self.GEMINI_API_KEY:
            raise EnvironmentError("MONGO_URI and GEMINI_API_KEY are required.")

APP_CONFIG = Config()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("argus-nova-refactor")

# --- ثابت‌های برنامه ---
class ACTION:
    """ نام‌های ثابت برای تمام Actionهای رابط کاربری. """
    NEW_CONV, SELECT_CONV = "new_conv", "select_conv"
    ADD_CTX_WEB, NEW_NOTE_MODAL, SHOW_NOTES, SUMMARIZE_CONV = "add_ctx_web", "new_note_modal", "show_notes", "summarize_conv"
    DELETE_NOTE = "delete_note"

# --- دیکشنری مدل‌ها ---
MODELS = { "چت متنی": { "Gemini 2.5 Pro": {"id": "gemini-2.5-pro"}, "Gemini 2.5 Flash": {"id": "gemini-2.5-flash"}}, "تولید تصویر": { "Gemini 2.5 Flash Image": {"id": "gemini-2.5-flash-image-preview"}}}
DEFAULT_MODEL_ID, SUMMARIZE_MODEL_ID = "gemini-2.5-flash", "gemini-2.5-flash-lite"
if APP_CONFIG.GEMINI_API_KEY: genai.configure(api_key=APP_CONFIG.GEMINI_API_KEY)


# ----------------------------------------------------------------------
# بخش ۲: لایه پایگاه داده (Database Layer)
# ----------------------------------------------------------------------
class Database:
    """ مسئول تمام تعاملات با MongoDB، از جمله کش کردن. """
    def __init__(self, uri: str, db_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.conversations = self.db["conversations"]
        self.messages = self.db["messages"]
        self.settings = self.db["settings"]
        self.notes = self.db["notes"]
        self.cache = self.db["cache"]
        logger.info("Database connection established.")

    # --- Cache Methods ---
    async def get_from_cache(self, key: str) -> Optional[Any]:
        doc = await self.cache.find_one({"_id": key})
        if doc and (datetime.now(timezone.utc) - doc['ts']) < timedelta(seconds=APP_CONFIG.CACHE_TTL_SECONDS):
            return doc['data']
        return None

    async def set_in_cache(self, key: str, data: Any):
        await self.cache.update_one({"_id": key}, {"$set": {"data": data, "ts": datetime.now(timezone.utc)}}, upsert=True)

    # (Other DB methods are functionally unchanged but organized)
    async def get_settings(self, user_id): return await self.settings.find_one({"_id": user_id}) or {}
    async def save_settings(self, user_id, data): await self.settings.update_one({"_id": user_id}, {"$set": data}, upsert=True)
    async def create_note(self, uid, c, t=None, r=None): return str((await self.notes.insert_one({"uid":uid,"txt":c,"ts":datetime.now(timezone.utc),"tags":t or [], "remind_at":r, "reminded":False})).inserted_id)
    async def get_notes(self, user_id): return await self.notes.find({"user_id": user_id}).sort("ts", -1).to_list(100)
    async def delete_note(self, note_id): return (await self.notes.delete_one({"_id": ObjectId(note_id)})).deleted_count > 0
    async def get_due_reminders(self): return await self.notes.find({"remind_at": {"$lte": datetime.now(timezone.utc)}, "reminded": False}).to_list(100)
    async def mark_reminder_sent(self, note_id): await self.notes.update_one({"_id": note_id}, {"$set": {"reminded": True}})
    async def create_conversation(self, uid, title): return str((await self.conversations.insert_one({"user_id": uid, "title": title, "created_at": datetime.now(timezone.utc)})).inserted_id)
    async def get_conversations(self, user_id): return await self.conversations.find({"user_id": user_id}).sort("created_at", -1).to_list(50)
    async def append_message(self, cid, uid, role, txt): await self.messages.insert_one({"conv_id": ObjectId(cid), "user_id": uid, "role": role, "text": txt, "created_at": datetime.now(timezone.utc)})
    async def get_messages(self, cid, limit=10): return list(reversed(await self.messages.find({"conv_id": ObjectId(cid)}).sort("created_at", -1).limit(limit).to_list(limit)))
    async def get_all_messages_text(self, cid): msgs = await self.messages.find({"conv_id": ObjectId(cid)}).sort("created_at", 1).to_list(1000); return "\n".join(f"{m['role']}:{m['text']}" for m in msgs)


# ----------------------------------------------------------------------
# بخش ۳: لایه ابزارها (Tools Layer)
# ----------------------------------------------------------------------
class Tools:
    """ مجموعه‌ای از توابع استاتیک برای تعامل با سرویس‌های خارجی. """
    @staticmethod
    async def web_search(query: str, db: Database) -> str:
        cache_key = f"web_search:{query}"
        cached = await db.get_from_cache(cache_key)
        if cached: return cached
        if not (APP_CONFIG.TAVILY_API_KEY and TavilyClient): return "جستجوی وب غیرفعال است."
        
        try:
            client = TavilyClient(api_key=APP_CONFIG.TAVILY_API_KEY)
            res = await asyncio.to_thread(client.search, query=query, search_depth="basic")
            context = "\n".join([f"- {obj['content']}" for obj in res.get('results', [])])
            result_str = f"[[نتایج وب برای '{query}']]\n{context}\n"
            await db.set_in_cache(cache_key, result_str)
            return result_str
        except Exception as e:
            return f"خطا در جستجوی وب: {e}"

    @staticmethod
    def process_file(file: cl.File) -> str:
        bytes_io = io.BytesIO(file.content)
        fname = file.name.lower()
        content = f"خطا در پردازش فایل {file.name}"
        try:
            if fname.endswith((".txt", ".md")): content = bytes_io.read().decode('utf-8')
            elif fname.endswith(".json"): content = json.dumps(json.load(bytes_io), indent=2, ensure_ascii=False)
            elif fname.endswith(".pdf") and extract_text: content = extract_text(bytes_io)
            elif fname.endswith(".csv") and pd: df = pd.read_csv(bytes_io); content = f"فایل CSV با {len(df)} ردیف. ۵ ردیف اول:\n{df.head().to_string()}"
            return f"[[شروع محتوای فایل: {file.name}]]\n{content}\n[[پایان محتوای فایل]]"
        except Exception as e:
            return f"[[خطا در پردازش {file.name}: {e}]]"

# ----------------------------------------------------------------------
# بخش ۴: لایه حافظه معنایی (Vector Store Layer)
# ----------------------------------------------------------------------
class VectorStore:
    """ مسئولیت مدیریت حافظه بلندمدت معنایی با استفاده از FAISS. """
    def __init__(self, index_path, meta_path):
        if not (faiss and SentenceTransformer):
            self.model, self.index = None, None
            logger.warning("FAISS/SentenceTransformer not installed. VectorStore is disabled.")
            return
            
        try:
            self.model = SentenceTransformer("all-MiniLM-L6-v2")
            self.dim = self.model.get_sentence_embedding_dimension()
        except Exception:
            self.model = None; return
            
        self.index_path, self.meta_path = index_path, meta_path
        self.metadata_map, self.next_id = {}, 0
        self._load()

    def _load(self):
        try:
            self.index = faiss.read_index(self.index_path) if os.path.exists(self.index_path) else faiss.IndexFlatIP(self.dim)
            if os.path.exists(self.meta_path):
                with open(self.meta_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.metadata_map, self.next_id = data.get('map',{}), data.get('next_id',0)
        except Exception as e:
            logger.error(f"Failed to load FAISS index: {e}"); self.index = faiss.IndexFlatIP(self.dim)

    def save(self):
        try:
            faiss.write_index(self.index, self.index_path)
            with open(self.meta_path, 'w', encoding='utf-8') as f: json.dump({'map':self.metadata_map,'next_id':self.next_id}, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save FAISS index: {e}")
    
    def add(self, text, meta):
        if not self.model: return
        embedding = self.model.encode([text], normalize_embeddings=True)
        self.index.add(embedding)
        self.metadata_map[str(self.next_id)] = meta; self.next_id += 1; self.save()

    def search(self, query, k=2, conv_id=None):
        if not (self.model and self.index and self.index.ntotal > 0): return []
        embedding = self.model.encode([query], normalize_embeddings=True)
        _, indices = self.index.search(embedding, k)
        return [self.metadata_map[str(i)]['text'] for i in indices[0] if i != -1 and self.metadata_map.get(str(i),{}).get('conv_id') == conv_id]

# ----------------------------------------------------------------------
# بخش ۵: مدیر برنامه (Chat Manager)
# ----------------------------------------------------------------------
class ChatManager:
    """ مغز متفکر برنامه؛ منطق اصلی را مدیریت کرده و لایه‌ها را به هم متصل می‌کند. """
    def __init__(self, db, vstore):
        self.db = db
        self.vstore = vstore

    async def setup_session(self):
        """ یک نشست کاربری جدید را راه‌اندازی و UI اولیه را ترسیم می‌کند. """
        settings = await self.db.get_settings(APP_CONFIG.USER_ID)
        cl.user_session.set("settings", settings)
        await cl.Avatar(name="کاربر", url="/public/user.png").send()
        await cl.Avatar(name="آریو", url="/public/assistant.png").send()
        await self.render_sidebar()
        await cl.Message(content="### دستیار Argus Nova\n آماده به کار.").send()

    async def render_sidebar(self):
        """ نوار کناری را با دکمه‌ها و تاریخچه مکالمات ترسیم می‌کند. """
        convs = await self.db.get_conversations(APP_CONFIG.USER_ID)
        tool_actions = cl.Action(name="tools_group", label="ابزارها", actions=[cl.Action(ACTION.ADD_CTX_WEB, "🌐 افزودن زمینه از وب")])
        note_actions = cl.Action(name="notes_group", label="دفترچه یادداشت", actions=[cl.Action(ACTION.NEW_NOTE_MODAL, "➕ یادداشت جدید"), cl.Action(ACTION.SHOW_NOTES, "🗒️ مشاهده همه یادداشت‌ها")])
        conv_mgmt = [cl.Action(ACTION.NEW_CONV, "➕ مکالمه جدید"), cl.Action(ACTION.SUMMARIZE_CONV, "✍️ خلاصه‌سازی")]
        conv_history = [cl.Action(ACTION.SELECT_CONV, str(c["_id"]), f"💬 {c.get('title', '...')}") for c in convs]
        await cl.set_actions([tool_actions, note_actions, *conv_mgmt, *conv_history])
    
    async def stream_gemini(self, history, model_id, context=None):
        """ پاسخ مدل Gemini را استریم می‌کند. """
        if context: history.insert(-1, {"role": "user", "text": context})
        api_history = [{"role": m.get("role"), "parts": [{"text": m.get("text", "")}]} for m in history]
        stream = await genai.GenerativeModel(model_id).generate_content_async(api_history, stream=True)
        async for chunk in stream:
            if text := getattr(chunk, "text", None): yield text
    
    async def process_user_message(self, message: cl.Message):
        """ پیام کاربر را به صورت کامل پردازش می‌کند. """
        text, elements = (message.content or "").strip(), message.elements
        context = cl.user_session.get("context", "")
        
        # 1. افزودن زمینه از فایل‌های آپلود شده
        if elements: context += "\n".join([Tools.process_file(e) for e in elements])
        if context: text = f"{context}\n\nپرسش اصلی: {text}"; cl.user_session.set("context", None)

        # 2. مدیریت مکالمه
        conv_id = cl.user_session.get("current_conv_id")
        if not conv_id: conv_id = await self.db.create_conversation(APP_CONFIG.USER_ID, text[:50] or "مکالمه جدید"); cl.user_session.set("current_conv_id", conv_id); await self.render_sidebar()
        
        await self.db.append_message(conv_id, APP_CONFIG.USER_ID, "user", text)
        if self.vstore: await asyncio.to_thread(self.vstore.add, text, {'conv_id': conv_id, 'user_id': APP_CONFIG.USER_ID, 'text': text})

        # 3. آماده‌سازی حافظه برای مدل
        short_term_mem = await self.db.get_messages(conv_id)
        rag_context = ""
        if self.vstore:
            long_term_mem = await asyncio.to_thread(self.vstore.search, query=text, conv_id=conv_id)
            if long_term_mem: rag_context = "[[زمینه از حافظه بلندمدت]]\n" + "\n".join(f"- {t}" for t in long_term_mem)
        
        await self.display_history(conv_id)
        
        # 4. استریم کردن پاسخ
        model_id = cl.user_session.get("settings", {}).get("model_id", DEFAULT_MODEL_ID)
        response_msg = cl.Message("", author="آریو"); await response_msg.send()
        full_response = "".join([t async for t in self.stream_gemini(short_term_mem, model_id, rag_context)])
        
        if full_response:
            await response_msg.update(full_response)
            await self.db.append_message(conv_id, APP_CONFIG.USER_ID, "assistant", full_response)

    async def summarize_conversation(self, conv_id: str):
        cache_key, db = f"summary:{conv_id}", self.db
        if cached := await db.get_from_cache(cache_key):
            await cl.Message(cached, author="آریو").send(); return
        
        m = await cl.Message("درحال خلاصه‌سازی...", author="سیستم"); await m.send()
        full_text = await db.get_all_messages_text(conv_id)
        prompt = f"این مکالمه را به طور جامع خلاصه کن:\n\n{full_text}"
        summary = "".join([t async for t in self.stream_gemini([{"role": "user", "text": prompt}], SUMMARIZE_MODEL_ID)])
        await m.update(summary, author="آریو"); await db.set_in_cache(cache_key, summary)
    
    async def display_history(self, cid): await cl.empty_chat();[await cl.Message(m.get('text',''),author="کاربر"if m.get('role')=="user"else "آریو").send() for m in await self.db.get_messages(cid,50)]
    async def show_all_notes(self):
        notes=await self.db.get_notes(APP_CONFIG.USER_ID)
        if not notes: await cl.Message("یادداشتی نیست.").send(); return
        for n in notes: await cl.Message(f"{n['txt']}\n`تگ‌ها:{','.join(n['tags'])}`" if n['tags'] else n['txt'], actions=[cl.Action(ACTION.DELETE_NOTE,str(n["_id"]),"حذف")]).send()


# ----------------------------------------------------------------------
# بخش ۶: نمونه‌سازی سراسری و وظایف پس‌زمینه
# ----------------------------------------------------------------------
DB_INSTANCE = Database(APP_CONFIG.MONGO_URI, APP_CONFIG.DB_NAME)
VSTORE_INSTANCE = VectorStore(APP_CONFIG.FAISS_INDEX_PATH, APP_CONFIG.FAISS_META_PATH)
CHAT_MANAGER = ChatManager(DB_INSTANCE, VSTORE_INSTANCE)
REMINDER_TASK_STARTED = False

async def check_reminders():
    while True:
        await asyncio.sleep(60)
        due_reminders = await DB_INSTANCE.get_due_reminders()
        for r in due_reminders:
            await cl.Message(f"**یادآور:** {r.get('txt')}", author="سیستم").send()
            await DB_INSTANCE.mark_reminder_sent(r['_id'])

# ----------------------------------------------------------------------
# بخش ۷: کنترل‌گرهای رابط کاربری (UI Handlers)
# ----------------------------------------------------------------------
@cl.on_chat_start
async def on_chat_start():
    """ با شروع چت، نشست را راه‌اندازی و وظیفه پس‌زمینه را اجرا می‌کند. """
    global REMINDER_TASK_STARTED
    await CHAT_MANAGER.setup_session()
    if not REMINDER_TASK_STARTED:
        asyncio.create_task(check_reminders()); REMINDER_TASK_STARTED = True

@cl.on_message
async def on_message(message: cl.Message):
    """ هر پیام جدید کاربر را به مدیر برنامه ارسال می‌کند. """
    await CHAT_MANAGER.process_user_message(message)

@cl.on_action
async def on_action(action: cl.Action):
    """ تمام کلیک‌های کاربر روی دکمه‌ها را مدیریت می‌کند. """
    conv_id = cl.user_session.get("current_conv_id")
    match action.name:
        case ACTION.NEW_CONV:
            cl.user_session.set("current_conv_id", None); await cl.empty_chat()
        case ACTION.SELECT_CONV:
            cl.user_session.set("current_conv_id", action.value); await CHAT_MANAGER.display_history(action.value)
        case ACTION.SUMMARIZE_CONV if conv_id:
            await CHAT_MANAGER.summarize_conversation(conv_id)
        case ACTION.SHOW_NOTES:
            await CHAT_MANAGER.show_all_notes()
        case ACTION.ADD_CTX_WEB:
            res = await cl.AskUserMessage("چه چیزی را در وب جستجو کنم؟").send()
            if res and res.get('output'):
                context = await Tools.web_search(res['output'], DB_INSTANCE)
                cl.user_session.set("context", context)
                await cl.Message("✅ زمینه از وب اضافه شد. حالا سوال خود را بپرسید.").send()
        case ACTION.NEW_NOTE_MODAL:
            res = await cl.AskActionMessage("یادداشت جدید", actions=[cl.Action("confirm","ثبت")], inputs=[
                cl.TextInput("content","متن",required=True), cl.TextInput("tags","برچسب‌ها (با کاما جدا کنید)"),
                cl.TextInput("reminder", "یادآور (YYYY-MM-DDTHH:MM)")
            ]).send()
            if res and res.get('name') == 'confirm':
                tags = [t.strip() for t in res.get('tags','').split(',') if t.strip()]
                remind = datetime.fromisoformat(res.get('reminder')) if res.get('reminder') else None
                await DB_INSTANCE.create_note(APP_CONFIG.USER_ID, res['content'], tags, remind); await cl.Message("یادداشت ثبت شد.").send()
        case ACTION.DELETE_NOTE:
            await DB_INSTANCE.delete_note(action.value)
