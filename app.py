# -*- coding: utf-8 -*-
"""
Argus Nova — Redis-atomic RL + Worker + FAISS (single-file)
----------------------------------------------------------
پیش‌نیازها (حداقل):
  pip install chainlit motor python-dotenv bcrypt PyJWT google-generativeai pillow aioredis rq redis sentence-transformers faiss-cpu

(توضیح: faiss-cpu و sentence-transformers سنگین‌اند؛ اگر نمی‌خواهی FAISS نصب بشه، می‌تونی از این دو عبور کنی و سیستم به صورت degraded کار می‌کند.)

متغیرهای محیطی مورد نیاز:
  MONGO_URI, GEMINI_API_KEY, JWT_SECRET_KEY
اختیاری:
  REDIS_URL  (برای rate limiter و queue و worker)
  WORKER_REDIS_URL (اگر می‌خواهی جدا باشد)

چند نکتهٔ اجرایی:
 - برای worker با RQ: بعد از ست کردن REDIS_URL، اجرا کن:
     rq worker image_tasks --url redis://:password@host:port
 - برای تست محلی بدون Redis، rate-limiter از حافظه استفاده می‌کند و enqueue کردن task به شکل محلی انجام می‌شود (بدون worker واقعی).
 - برای فعال‌سازی تولید تصویر واقعی، کارگر process_image_task را به API مربوطه وصل کن (قسمت TODO در کد).
"""
import os
import io
import re
import json
import time
import base64
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

import chainlit as cl
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

# async mongo
from motor.motor_asyncio import AsyncIOMotorClient
import bcrypt
import jwt
import google.generativeai as genai

# redis + rq
try:
    import aioredis
except Exception:
    aioredis = None
try:
    import redis
    from rq import Queue
except Exception:
    redis = None
    Queue = None

# faiss & sentence-transformers
try:
    import faiss
    from sentence_transformers import SentenceTransformer
except Exception:
    faiss = None
    SentenceTransformer = None

# ---------------- Config & logging ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("argus-nova-power")

VERSION = "3.1.0-power"

MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
REDIS_URL = os.getenv("REDIS_URL")  # used for rate-limiter and RQ
WORKER_REDIS_URL = os.getenv("WORKER_REDIS_URL", REDIS_URL)

if not MONGO_URI:
    raise EnvironmentError("MONGO_URI is required")
if not GEMINI_API_KEY:
    raise EnvironmentError("GEMINI_API_KEY is required")
if not JWT_SECRET_KEY:
    raise EnvironmentError("JWT_SECRET_KEY is required")

# configure gemini
try:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini configured")
except Exception as e:
    logger.exception("Gemini configure failed: %s", e)

# ---------------- Utilities ----------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def valid_email(e: str) -> bool:
    return bool(e and EMAIL_RE.match(e))

def now_ts() -> float:
    return time.time()

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def jwt_encode(payload: dict) -> str:
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode()
    return token

# ---------------- DB (Motor, message-per-doc) ----------------
class DB:
    def __init__(self, uri: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client.get_database("argus_nova_db")
        self.users = self.db["users"]
        self.conversations = self.db["conversations"]
        self.messages = self.db["messages"]
        asyncio.get_event_loop().create_task(self._ensure_indexes())

    async def _ensure_indexes(self):
        try:
            await self.users.create_index("email", unique=True)
            await self.conversations.create_index([("user_id", 1), ("created_at", -1)])
            await self.messages.create_index([("conv_id", 1), ("created_at", 1)])
            await self.messages.create_index([("content", "text")], name="msg_text_idx")
            logger.info("DB indexes ensured")
        except Exception as e:
            logger.warning("Index creation failed: %s", e)

    # users
    async def get_user_by_email(self, email: str) -> Optional[dict]:
        if not email:
            return None
        return await self.users.find_one({"email": email.lower().strip()})

    async def create_user(self, name: str, email: str, hashed: str) -> str:
        doc = {"name": name, "email": email.lower().strip(), "password": hashed, "created_at": now_utc()}
        res = await self.users.insert_one(doc)
        return str(res.inserted_id)

    # conversations
    async def create_conversation(self, user_id: str, title: str) -> str:
        doc = {"user_id": ObjectId(user_id), "title": title, "created_at": now_utc(), "last_message_snippet": ""}
        res = await self.conversations.insert_one(doc)
        return str(res.inserted_id)

    async def get_conversations(self, user_id: str, limit: int = 50) -> List[dict]:
        cur = self.conversations.find({"user_id": ObjectId(user_id)}).sort("created_at", -1).limit(limit)
        return await cur.to_list(length=limit)

    async def rename_conversation(self, conv_id: str, user_id: str, new_title: str) -> bool:
        res = await self.conversations.update_one({"_id": ObjectId(conv_id), "user_id": ObjectId(user_id)}, {"$set": {"title": new_title}})
        return res.modified_count > 0

    async def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        await self.messages.delete_many({"conv_id": ObjectId(conv_id)})
        res = await self.conversations.delete_one({"_id": ObjectId(conv_id), "user_id": ObjectId(user_id)})
        return res.deleted_count > 0

    # messages
    async def append_message(self, conv_id: str, user_id: str, role: str, content: str) -> str:
        doc = {"conv_id": ObjectId(conv_id), "user_id": ObjectId(user_id), "role": role, "content": content, "created_at": now_utc()}
        res = await self.messages.insert_one(doc)
        # update snippet
        try:
            await self.conversations.update_one({"_id": ObjectId(conv_id)}, {"$set": {"last_message_snippet": content[:120]}})
        except Exception:
            pass
        return str(res.inserted_id)

    async def get_messages(self, conv_id: str, limit: int = 50, before: Optional[datetime] = None) -> List[dict]:
        q = {"conv_id": ObjectId(conv_id)}
        if before:
            q["created_at"] = {"$lt": before}
        cur = self.messages.find(q).sort("created_at", -1).limit(limit)
        docs = await cur.to_list(length=limit)
        return list(reversed(docs))

    async def edit_message(self, msg_id: str, new_content: str) -> bool:
        res = await self.messages.update_one({"_id": ObjectId(msg_id)}, {"$set": {"content": new_content}})
        return res.modified_count > 0

    async def delete_message(self, msg_id: str) -> bool:
        res = await self.messages.delete_one({"_id": ObjectId(msg_id)})
        return res.deleted_count > 0

    async def search_messages(self, user_id: str, query: str, limit: int = 50) -> List[dict]:
        cur = self.messages.find({"$text": {"$search": query}, "user_id": ObjectId(user_id)}).sort([("created_at", -1)]).limit(limit)
        return await cur.to_list(length=limit)

# ---------------- Auth ----------------
class Auth:
    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

    def verify_password(self, password: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception:
            return False

    def create_jwt(self, user_info: dict, expire_days: int = 1) -> str:
        payload = {
            "sub": user_info["id"],
            "name": user_info.get("name"),
            "email": user_info.get("email"),
            "iat": int(now_utc().timestamp()),
            "exp": int((now_utc() + timedelta(days=expire_days)).timestamp())
        }
        return jwt_encode(payload)

    def decode_jwt(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        try:
            return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return None
        except Exception as e:
            logger.warning("JWT decode error: %s", e)
            return None

# ---------------- Redis-atomic Token Bucket (Lua) ----------------
REDIS_LUA_TOKEN_BUCKET = """
-- KEYS[1] = tokens_key
-- KEYS[2] = timestamp_key
-- ARGV[1] = capacity (number)
-- ARGV[2] = refill_rate (tokens per second)
-- ARGV[3] = now (timestamp, number)
-- ARGV[4] = requested (number)
local tokens_key = KEYS[1]
local ts_key = KEYS[2]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local tokens = tonumber(redis.call('get', tokens_key) or capacity)
local last = tonumber(redis.call('get', ts_key) or 0)
local delta = 0
if last > 0 then
  delta = math.max(0, now - last)
end
tokens = math.min(capacity, tokens + delta * refill_rate)
if tokens < requested then
  -- not enough
  redis.call('set', tokens_key, tokens)
  redis.call('set', ts_key, now)
  return 0
else
  tokens = tokens - requested
  redis.call('set', tokens_key, tokens)
  redis.call('set', ts_key, now)
  return 1
end
"""

class RedisTokenBucket:
    def __init__(self, redis_url: Optional[str], capacity: float = 3.0, refill_rate: float = 0.5):
        self.redis_url = redis_url
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.redis = None
        self.sha = None
        if redis_url and aioredis:
            asyncio.get_event_loop().create_task(self._init())

    async def _init(self):
        try:
            self.redis = await aioredis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
            # load script
            self.sha = await self.redis.script_load(REDIS_LUA_TOKEN_BUCKET)
            logger.info("Redis token-bucket ready (sha=%s)", self.sha)
        except Exception as e:
            logger.warning("Redis init failed for token-bucket: %s", e)
            self.redis = None
            self.sha = None

    async def consume(self, user_id: str, requested: float = 1.0) -> bool:
        now = now_ts()
        if self.redis and self.sha:
            try:
                tokens_key = f"tb:tokens:{user_id}"
                ts_key = f"tb:ts:{user_id}"
                res = await self.redis.evalsha(self.sha, 2, tokens_key, ts_key, str(self.capacity), str(self.refill_rate), str(now), str(requested))
                return int(res) == 1
            except Exception as e:
                logger.warning("Redis token-bucket eval failed: %s", e)
                # fallback to in-memory
        # fallback simple in-memory bucket (per-process)
        return InMemoryBucket(self.capacity, self.refill_rate).consume(requested)

# ---------------- In-memory fallback (per-process) ----------------
class InMemoryBucket:
    def __init__(self, capacity: float = 3.0, refill_rate: float = 0.5):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.ts = now_ts()

    def consume(self, amount: float = 1.0) -> bool:
        now = now_ts()
        elapsed = now - self.ts
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.ts = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

# ---------------- RQ Worker (image tasks) ----------------
# We'll queue a job named "process_image_task" and provide an example worker function.
def get_rq_queue():
    if not redis or not WORKER_REDIS_URL:
        return None
    try:
        rconn = redis.from_url(WORKER_REDIS_URL, decode_responses=True)
        q = Queue("image_tasks", connection=rconn)
        return q
    except Exception as e:
        logger.warning("RQ queue init failed: %s", e)
        return None

def process_image_task(prompt: str, user_id: str, conv_id: str) -> dict:
    """
    Worker job (RQ/Celery compatible). For now it's a placeholder that creates a simple image with
    the prompt text rendered on it and returns base64 PNG.
    In production: replace body with call to Gemini Image API or other image generation API.
    """
    try:
        W, H = 1024, 1024
        img = Image.new("RGB", (W, H), color=(30, 30, 30))
        draw = ImageDraw.Draw(img)
        # try to use a default font; fallback to basic
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 32)
        except Exception:
            font = ImageFont.load_default()
        text = f"Placeholder image\nprompt: {prompt[:120]}"
        draw.multiline_text((20, 20), text, fill=(240,240,240), font=font)
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        b64 = base64.b64encode(bio.read()).decode()
        return {"status":"ok", "image_base64": b64, "note":"placeholder image generated by worker"}
    except Exception as e:
        logger.exception("process_image_task failed: %s", e)
        return {"status":"error", "error": str(e)}

def enqueue_image_task(prompt: str, user_id: str, conv_id: str) -> Optional[str]:
    q = get_rq_queue()
    if not q:
        logger.info("RQ not configured - running task synchronously (local placeholder).")
        res = process_image_task(prompt, user_id, conv_id)
        # in sync case, save result to filesystem or DB as needed; return synthetic job id
        return None
    job = q.enqueue(process_image_task, prompt, user_id, conv_id, result_ttl=3600)
    return job.get_id()

# ---------------- Vector store (FAISS) ----------------
class VectorStore:
    """
    Simple FAISS-backed vector store using sentence-transformers.
    Stores embeddings in memory + disk snapshot.
    In production prefer a managed vector DB (Pinecone/Weaviate/Vectara).
    """
    def __init__(self, dim: int = 384, index_path: str = "faiss_index.bin", meta_path: str = "faiss_meta.json"):
        self.dim = dim
        self.index_path = index_path
        self.meta_path = meta_path
        self.index = None
        self.id_to_meta: Dict[int, dict] = {}
        self.next_id = 0
        self.model = None
        if SentenceTransformer:
            try:
                self.model = SentenceTransformer("all-MiniLM-L6-v2")  # light and performant
                self.dim = self.model.get_sentence_embedding_dimension()
            except Exception as e:
                logger.warning("Failed to load embedding model: %s", e)
                self.model = None
        if faiss:
            self._init_index()
            self._load_meta()

    def _init_index(self):
        if self.index is None:
            self.index = faiss.IndexFlatIP(self.dim)  # inner product, use normalized embeddings
            logger.info("FAISS index initialized dim=%s", self.dim)

    def _save_meta(self):
        try:
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump({"id_to_meta": self.id_to_meta, "next_id": self.next_id}, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("Failed saving faiss meta: %s", e)

    def _load_meta(self):
        if not os.path.exists(self.meta_path):
            return
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                self.id_to_meta = d.get("id_to_meta", {})
                self.next_id = d.get("next_id", 0)
        except Exception as e:
            logger.warning("Failed load faiss meta: %s", e)

    def add(self, text: str, meta: dict) -> Optional[int]:
        if not (faiss and self.model and self.index):
            return None
        try:
            emb = self.model.encode([text], convert_to_numpy=True, normalize_embeddings=True)
            self.index.add(emb)
            this_id = self.next_id
            self.id_to_meta[str(this_id)] = {"meta": meta, "text": text}
            self.next_id += 1
            self._save_meta()
            return this_id
        except Exception as e:
            logger.warning("FAISS add failed: %s", e)
            return None

    def search(self, query: str, top_k: int = 5) -> List[Tuple[dict, float]]:
        if not (faiss and self.model and self.index):
            return []
        emb = self.model.encode([query], convert_to_numpy=True, normalize_embeddings=True)
        D, I = self.index.search(emb, top_k)
        results = []
        for score, idx in zip(D[0], I[0]):
            if idx < 0:
                continue
            meta = self.id_to_meta.get(str(int(idx)), {})
            results.append((meta, float(score)))
        return results

# ---------------- Chat Manager & Chainlit glue (uses the above pieces) ----------------
# (I'll reuse earlier DB/Auth logic but wire-in token-bucket, enqueue, vector-store hooks)

from bson import ObjectId

DB_CONN = DB(MONGO_URI)
AUTH = Auth()
# Redis token-bucket instance
REDIS_BUCKET = RedisTokenBucket(REDIS_URL, capacity=float(os.getenv("RATE_BUCKET_SIZE", 3.0)), refill_rate=float(os.getenv("RATE_PER_SEC", 0.5)))
# fallback in-memory bucket map per-process for users when redis disabled
LOCAL_BUCKETS: Dict[str, InMemoryBucket] = {}

# RQ queue lazy init via get_rq_queue()
# Vector store
VSTORE = VectorStore() if (faiss and SentenceTransformer) else None

# Model definitions (same as before)
MODELS = {
    "chat": {
        "Gemini 2.5 Flash": {"id": "gemini-2.5-flash"},
        "Gemini 1.5 Flash": {"id": "gemini-1.5-flash-latest"},
    },
    "image": {"Gemini Image Preview": {"id": "gemini-2.5-flash-image-preview"}},
    "video": {"Veo 3": {"id": "veo-3"}},
}
MODEL_LOOKUP = {info["id"]: {"category": cat, "name": name, **info} for cat, group in MODELS.items() for name, info in group.items()}
DEFAULT_MODEL_ID = "gemini-2.5-flash" if "gemini-2.5-flash" in MODEL_LOOKUP else next(iter(MODEL_LOOKUP))

class ChatManager:
    def __init__(self, db: DB, auth: Auth):
        self.db = db
        self.auth = auth

    async def setup_user(self, user_payload: dict):
        await cl.Avatar(name=user_payload.get("name", "کاربر"), url=None).send()
        await cl.Avatar(name="آریو", url=None, for_chainlit_helpers=True).send()
        model_opts = [cl.select.Option(label=f"{name} ({cat})", value=info["id"]) for cat, group in MODELS.items() for name, info in group.items()]
        await cl.ChatSettings([
            cl.select.Select(id="Model", label="انتخاب مدل", options=model_opts, initial_value=DEFAULT_MODEL_ID),
            cl.select.Select(id="Temperature", label="دما", options=[cl.select.Option(str(x), value=str(x)) for x in [0.0,0.2,0.5,0.8,1.0]], initial_value="0.2"),
            cl.select.Select(id="MaxTokens", label="حداکثر توکن", options=[cl.select.Option(str(x), value=str(x)) for x in [128,256,512,1024]], initial_value="512")
        ]).send()
        await cl.Message(content=f"سلام {user_payload.get('name', 'کاربر')} — Argus Nova آماده است!").send()
        await self.render_sidebar(user_payload["sub"])

    async def render_sidebar(self, user_id: str):
        convs = await DB_CONN.get_conversations(user_id)
        conv_actions = [cl.Action(name="select_conv", value=str(c["_id"]), label=f"💬 {c.get('title','بدون عنوان')} — {c.get('last_message_snippet','')[:40]}") for c in convs]
        controls = [
            cl.Action(name="new_chat", value="new", label="➕ مکالمه جدید"),
            cl.Action(name="rename_conv", value="rename", label="✏️ تغییر نام"),
            cl.Action(name="delete_conv", value="delete", label="🗑️ حذف"),
            cl.Action(name="image_gen", value="image", label="🖼️ تولید تصویر (enqueue)")
        ]
        await cl.set_actions(controls + conv_actions)

    async def stream_gemini(self, history: List[Dict[str, Any]], model_id: str):
        if MODEL_LOOKUP.get(model_id, {}).get("category") in ["image", "video"]:
            yield f"مدل {model_id} رسانه‌ای است — غیرفعال."
            return
        try:
            api_history = []
            for m in history:
                role = "user" if m.get("role") == "user" else "model"
                api_history.append({"role": role, "parts": [{"text": m.get("content","")}]})
            model = genai.GenerativeModel(model_id)
            stream = model.generate_content(api_history, stream=True)
            assembled = ""
            for chunk in stream:
                t = getattr(chunk, "text", None)
                if t:
                    assembled += t
                    yield t
            return
        except Exception as e:
            logger.exception("Gemini stream failed: %s", e)
            yield f"**خطای API:** {e}"
            return

    async def display_history(self, conv_id: str, limit: int = 200):
        msgs = await DB_CONN.get_messages(conv_id, limit=limit)
        await cl.empty_chat()
        for m in msgs:
            author = "کاربر" if m.get("role") == "user" else "آریو"
            ts = m.get("created_at")
            ts_text = ""
            if ts and isinstance(ts, datetime):
                ts_text = f" — {ts.strftime('%Y-%m-%d %H:%M:%S')}"
            await cl.Message(content=(m.get("content","") + ts_text), author=author).send()

# instantiate
CHAT = ChatManager(DB_CONN, AUTH)

# ---------------- Chainlit handlers (glueing everything) ----------------
@cl.on_chat_start
async def on_chat_start():
    token = cl.user_session.get("jwt")
    payload = AUTH.decode_jwt(token)
    if payload:
        cl.user_session.set("user", payload)
        cl.user_session.set("model_id", cl.user_session.get("model_id", DEFAULT_MODEL_ID))
        await CHAT.setup_user(payload)
    else:
        cl.user_session.set("user", None)
        await cl.Message(content=("به Argus Nova خوش آمدید!\n"
                                   "ورود: `/login email password`\n"
                                   "ثبت‌نام: `/signup name email password`")).send()

@cl.on_settings_update
async def on_settings_update(settings: dict):
    model_id = settings.get("Model")
    if model_id:
        cl.user_session.set("model_id", model_id)
        await cl.Message(content=f"مدل به `{model_id}` تغییر کرد.").send()

@cl.on_message
async def on_message(message: cl.Message):
    user = cl.user_session.get("user")
    # auth flow
    if not user:
        txt = (message.content or "").strip()
        if txt.startswith("/login"):
            parts = txt.split()
            if len(parts) >= 3:
                email = parts[1]; pw = " ".join(parts[2:])
                if not valid_email(email):
                    await cl.Message(content="ایمیل نامعتبر").send(); return
                dbu = await DB_CONN.get_user_by_email(email)
                if dbu and AUTH.verify_password(pw, dbu["password"]):
                    uinfo = {"id": str(dbu["_id"]), "name": dbu.get("name"), "email": dbu.get("email")}
                    token = AUTH.create_jwt(uinfo)
                    cl.user_session.set("jwt", token)
                    await on_chat_start(); return
                await cl.Message(content="ایمیل یا رمز اشتباه").send(); return
            await cl.Message(content="فرمت: /login email password").send(); return

        if txt.startswith("/signup"):
            parts = txt.split()
            if len(parts) >= 4:
                name = parts[1]; email = parts[2]; pw = " ".join(parts[3:])
                if not valid_email(email):
                    await cl.Message(content="ایمیل نامعتبر").send(); return
                if await DB_CONN.get_user_by_email(email):
                    await cl.Message(content="این ایمیل قبلاً ثبت شده").send(); return
                hashed = AUTH.hash_password(pw)
                uid = await DB_CONN.create_user(name, email, hashed)
                await cl.Message(content="ثبت‌نام موفق — برای ورود از /login استفاده کنید").send(); return
            await cl.Message(content="فرمت: /signup name email password").send(); return

        await cl.Message(content=("برای ورود: `/login email password`\nبرای ثبت‌نام: `/signup name email password`")).send()
        return

    # rate-limit: try redis-atomic token-bucket first
    uid = user["sub"]
    allowed = False
    try:
        # try Redis atomic
        if REDIS_URL and aioredis:
            allowed = await REDIS_BUCKET.consume(uid, requested=1.0)
        else:
            # per-process local bucket map
            if uid not in LOCAL_BUCKETS:
                LOCAL_BUCKETS[uid] = InMemoryBucket(float(os.getenv("RATE_BUCKET_SIZE", 3.0)), float(os.getenv("RATE_PER_SEC", 0.5)))
            allowed = LOCAL_BUCKETS[uid].consume(1.0)
    except Exception as e:
        logger.warning("Rate limiter failure (fallback allow): %s", e)
        allowed = True

    if not allowed:
        await cl.Message(content="شما سریع پیام می‌فرستید — لطفاً کمی صبر کنید.").send()
        return

    # detect image element
    image = None
    try:
        image_el = next((el for el in message.elements if getattr(el, "mime", "").startswith("image")), None)
        if image_el:
            image = Image.open(io.BytesIO(image_el.content))
    except Exception:
        image = None

    # ensure conversation
    conv_id = cl.user_session.get("current_conv_id")
    if not conv_id:
        title = (message.content or "مکالمه جدید")[:120]
        conv_id = await DB_CONN.create_conversation(user["sub"], title)
        cl.user_session.set("current_conv_id", conv_id)
        await CHAT.render_sidebar(user["sub"])

    # append user message
    await DB_CONN.append_message(conv_id, user["sub"], "user", message.content or "")

    # vector-store upsert (async, non-blocking)
    if VSTORE and VSTORE.model:
        # schedule embedding in background thread so it doesn't block event loop
        text = message.content or ""
        asyncio.get_event_loop().run_in_executor(None, lambda: VSTORE.add(text, {"conv_id": str(conv_id), "role":"user"}))

    # display recent history
    await CHAT.display_history(conv_id)

    # settings
    model_id = cl.user_session.get("model_id", DEFAULT_MODEL_ID)
    temp = float(cl.user_session.get("Temperature") or 0.2)
    max_tokens = int(cl.user_session.get("MaxTokens") or 512)

    # If user triggers image generation via special prefix (e.g., /image prompt...), enqueue
    txt = (message.content or "").strip()
    if txt.startswith("/image"):
        prompt = txt[len("/image"):].strip() or "تصویر پیش‌فرض"
        jobid = enqueue_image_task(prompt, user["sub"], conv_id)
        await cl.Message(content=f"درخواست تولید تصویر ثبت شد. job_id: {jobid or 'local'}").send()
        return

    # media models placeholder
    if MODEL_LOOKUP.get(model_id, {}).get("category") in ["image", "video"]:
        reply = f"مدل ({model_id}) مولد رسانه است؛ تولید رسانه در این نسخه غیرفعال است."
        await DB_CONN.append_message(conv_id, user["sub"], "assistant", reply)
        await cl.Message(content=reply, author="آریو").send()
        return

    # stream from Gemini
    placeholder = cl.Message(content="", author="آریو")
    await placeholder.send()
    history = await DB_CONN.get_messages(conv_id, limit=40)

    assembled = ""
    try:
        async for token in CHAT.stream_gemini(history, model_id):
            assembled += token
            try:
                await placeholder.stream_token(token)
            except Exception:
                await placeholder.update(content=assembled)
        # persist assistant
        await DB_CONN.append_message(conv_id, user["sub"], "assistant", assembled)
    except Exception as e:
        logger.exception("streaming error: %s", e)
        await cl.Message(content=f"خطا در تولید پاسخ مدل: {e}", author="System").send()

@cl.on_action
async def on_action(action: cl.Action):
    user = cl.user_session.get("user")
    if not user:
        return
    conv_id = cl.user_session.get("current_conv_id")

    if action.name == "new_chat":
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat(); await cl.Message(content="مکالمه جدید آغاز شد").send(); return

    if action.name == "select_conv":
        conv_id = action.value
        cl.user_session.set("current_conv_id", conv_id)
        await CHAT.display_history(conv_id); return

    if action.name == "rename_conv" and conv_id:
        res = await cl.AskUserMessage(content="عنوان جدید را وارد کنید:", timeout=120).send()
        if res:
            await DB_CONN.rename_conversation(conv_id, user["sub"], res["output"])
            await CHAT.render_sidebar(user["sub"])
            await cl.Message(content="نام مکالمه تغییر کرد").send(); return

    if action.name == "delete_conv" and conv_id:
        await DB_CONN.delete_conversation(conv_id, user["sub"])
        cl.user_session.set("current_conv_id", None)
        await cl.empty_chat(); await CHAT.render_sidebar(user["sub"]); await cl.Message(content="مکالمه حذف شد").send(); return

    if action.name == "image_gen":
        # quick demo: ask user for prompt, then enqueue
        res = await cl.AskUserMessage(content="متن توضیح تصویر را وارد کنید:", timeout=180).send()
        if res:
            prompt = res["output"]
            jobid = enqueue_image_task(prompt, user["sub"], conv_id)
            await cl.Message(content=f"درخواست تولید تصویر ثبت شد: job={jobid or 'local'}").send()
        return

    if action.name == "export_conv" and conv_id:
        msgs = await DB_CONN.get_messages(conv_id, limit=10000)
        data = [{"author": ("user" if m.get("role")=="user" else "assistant"), "content": m.get("content"), "created_at": m.get("created_at").isoformat()} for m in msgs]
        fname = f"conversation_{conv_id}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        await cl.Message(content=f"Exported to `{fname}` on server.").send()
        return

    if action.name == "edit_message" and conv_id:
        res = await cl.AskUserMessage(content="متن جدید را وارد کنید:", timeout=180).send()
        if res:
            success = await DB_CONN.edit_message(action.value, res["output"])
            if success:
                await cl.Message(content="پیام ویرایش شد"); await CHAT.display_history(conv_id)
            else:
                await cl.Message(content="ویرایش ناموفق شد"); return

    if action.name == "delete_message" and conv_id:
        await DB_CONN.delete_message(action.value)
        await cl.Message(content="پیام حذف شد"); await CHAT.display_history(conv_id); return

    if action.name == "copy_message":
        await cl.Message(content="متن کپی شد!").send(); return
