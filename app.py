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
    NEW_CONV,SELECT_CONV="nc","sc";ADD_CTX_WEB,NEW_NOTE_MODAL,SHOW_NOTES,SUMMARIZE_CONV="acw","nnm","sn","sm";DELETE_NOTE,NUKE_DATABASE="dn","nuke_db"

MODELS={"چت متنی":{"Gemini 2.5 Pro":{"id":"gemini-2.5-pro"},"Gemini 2.5 Flash":{"id":"gemini-2.5-flash"},"Gemini 2.0 Pro":{"id":"gemini-2.0-pro"}},"تولید تصویر":{"Gemini 2.5 Flash Image":{"id":"gemini-2.5-flash-image-preview"}}}
DEFAULT_MODEL_ID,SUMMARIZE_MODEL_ID="gemini-2.5-flash","gemini-2.5-flash-lite"
if APP_CONFIG.GEMINI_API_KEY:genai.configure(api_key=APP_CONFIG.GEMINI_API_KEY)


# ----------------------------------------------------------------------
# بخش ۲: لایه پایگاه داده
# ----------------------------------------------------------------------
class Database:
    def __init__(self, uri, db_name):
        self.client=None;self._uri,self._db_name=uri,db_name
    async def _connect(self):
        if self.client:return
        try:self.client=AsyncIOMotorClient(self._uri,serverSelectionTimeoutMS=5000);await self.client.admin.command('ping');self.db=self.client[self._db_name];self.c,self.m,self.n,self.cache,self.vstore=self.db["c"],self.db["m"],self.db["n"],self.db["cache"],self.db["vstore"];logger.info("✅ MongoDB connected.")
        except Exception as e:self.client=None;raise ConnectionError(f"Failed to connect: {e}")
    async def get_from_cache(self,k):await self._connect();doc=await self.cache.find_one({"_id":k});ts=doc.get('ts')if doc else None;ts=ts.replace(tzinfo=timezone.utc)if ts and ts.tzinfo is None else ts;return doc.get('data')if ts and(datetime.now(timezone.utc)-ts)<timedelta(seconds=APP_CONFIG.CACHE_TTL_SECONDS)else None
    async def set_in_cache(self,k,d):await self._connect();await self.cache.update_one({"_id":k},{"$set":{"data":d,"ts":datetime.now(timezone.utc)}},upsert=True)
    async def create_note(self,uid,c,t,r):await self._connect();await self.n.insert_one({"uid":uid,"txt":c,"tags":t,"remind_at":r,"ts":datetime.now(timezone.utc),"reminded":False})
    async def get_notes(self,uid):await self._connect();return await self.n.find({"uid":uid}).sort("ts",-1).to_list(100)
    async def delete_note(self,nid):await self._connect();await self.n.delete_one({"_id":ObjectId(nid)})
    async def get_due_reminders(self):await self._connect();return await self.n.find({"remind_at":{"$lte":datetime.now(timezone.utc)},"reminded":False}).to_list(100)
    async def mark_reminder_sent(self,nid):await self._connect();await self.n.update_one({"_id":nid},{"$set":{"reminded":True}})
    async def create_conversation(self,uid,t):await self._connect();res=await self.c.insert_one({"uid":uid,"title":t,"ts":datetime.now(timezone.utc)});return str(res.inserted_id)
    async def get_conversations(self,uid):await self._connect();return await self.c.find({"uid":uid}).sort("ts",-1).to_list(50)
    async def append_message(self,cid,uid,r,txt):await self._connect();await self.m.insert_one({"cid":ObjectId(cid),"uid":uid,"role":r,"txt":txt,"ts":datetime.now(timezone.utc)})
    async def get_messages(self,cid,limit=10):await self._connect();return list(reversed(await self.m.find({"cid":ObjectId(cid)}).sort("ts",-1).limit(limit).to_list(limit)))
    async def get_all_messages_text(self,cid):await self._connect();msgs=await self.m.find({"cid":ObjectId(cid)}).sort("ts",1).to_list(1000);return"\n".join(f"{m['role']}:{m['txt']}"for m in msgs)
    async def nuke(self,uid):await self._connect();await self.m.delete_many({"uid":uid});await self.c.delete_many({"uid":uid});await self.n.delete_many({"uid":uid});await self.cache.delete_many({});await self.vstore.delete_many({})


# ----------------------------------------------------------------------
# بخش ۳: لایه ابزارها
# ----------------------------------------------------------------------
class Tools:
    @staticmethod
    async def process_file(file:cl.File)->str:
        content,fname=" ",file.name.lower()
        try:
            if fname.endswith((".txt",".md")):content=file.content.decode('utf-8')
            elif fname.endswith(".pdf") and extract_text:content=extract_text(io.BytesIO(file.content))
            elif fname.endswith(".csv") and pd:content=f"خلاصه CSV:\n{pd.read_csv(io.BytesIO(file.content)).head().to_string()}"
            return f"[[شروع محتوای {file.name}]]\n{content}\n[[پایان]]" if content else ""
        except Exception as e: return f"[[خطا در پردازش {file.name}: {e}]]"
    @staticmethod
    async def web_search(query:str,db:Database)->str:
        if not TavilyClient:return""
        if cached:=await db.get_from_cache(f"web:{query}"):return cached
        try:
            res=await asyncio.to_thread(TavilyClient(api_key=APP_CONFIG.TAVILY_API_KEY).search,query=query)
            ctx="\n".join([f"- {obj['content']}"for obj in res.get('results',[])])
            if not ctx:raise ValueError("No results");result=f"[[نتایج وب:'{query}]]\n{ctx}\n";await db.set_in_cache(f"web:{query}",result);return result
        except Exception as e:await cl.Message(f"سرویس جستجو خطا داد: {e}",author="سیستم").send();return""

# ----------------------------------------------------------------------
# بخش ۴: لایه حافظه معنایی (ذخیره در MongoDB)
# ----------------------------------------------------------------------
class VectorStore:
    def __init__(self, db: Database):
        self.is_enabled, self.db = bool(faiss and SentenceTransformer), db
        if not self.is_enabled: logger.warning("VectorStore is disabled."); return
        try:
            self.model,self.dim=SentenceTransformer("all-MiniLM-L6-v2"),384
            self.map,self.next_id={},0;self.index=faiss.IndexFlatIP(self.dim)
            asyncio.create_task(self._load_from_db())
        except Exception as e: self.is_enabled=False; logger.error(f"VectorStore init failed: {e}")
    
    async def _load_from_db(self):
        try:
            await self.db._connect();data=await self.db.vstore.find_one({"_id":"main_index"})
            if data:
                self.map=data.get('map',{});self.next_id=data.get('next_id',0)
                if'index_data'in data:self.index=faiss.read_index(io.BytesIOReader(io.BytesIO(data['index_data'])))
            logger.info(f"✅ VectorStore loaded from DB with {self.next_id} embeddings.")
        except Exception as e:logger.error(f"Failed to load FAISS from DB: {e}");self.index=faiss.IndexFlatIP(self.dim)
    
    async def _blocking_save(self):
        try:
            buffer=io.BytesIO();faiss.write_index(self.index,io.BytesIOWriter(buffer));index_data=buffer.getvalue()
            doc={"_id":"main_index","map":self.map,"next_id":self.next_id,"index_data":Binary(index_data)}
            await self.db.vstore.update_one({"_id":"main_index"},{"$set":doc},upsert=True)
        except Exception as e: logger.error(f"FAISS save error: {e}",exc_info=True)

    def _blocking_add(self,txt,meta):
        try:e=self.model.encode([txt],normalize_embeddings=True);self.index.add(e);self.map[str(self.next_id)]=meta;self.next_id+=1;asyncio.run(self._blocking_save())
        except Exception as e:logger.error(f"FAISS add error: {e}",exc_info=True)
    def add(self,txt,meta):
        if self.is_enabled:asyncio.to_thread(self._blocking_add,txt,meta)
    
    def _blocking_search(self,q,k,cid):
        if self.index.ntotal==0:return[]
        e=self.model.encode([q],normalize_embeddings=True);_,I=self.index.search(e,k)
        return[item['txt']for i in I[0]if i!=-1 and(item:=self.map.get(str(i)))and item.get('cid')==cid and'txt'in item]
    async def search(self,query,k=2,cid=None):
        if not self.is_enabled:return[]
        return await asyncio.to_thread(self._blocking_search,query,k,cid)


# ----------------------------------------------------------------------
# بخش ۵: مدیر برنامه
# ----------------------------------------------------------------------
class ChatManager:
    def __init__(self,db,vstore):self.db,self.vstore,self.queue=db,vstore,asyncio.Queue();self.processor=asyncio.create_task(self._process_queue())
    async def _process_queue(self):
        while True:
            msg=await self.queue.get()
            try:await self._execute_message_processing(msg)
            except Exception as e:await cl.Message(f"خطا در پردازش: {e}",author="سیستم").send()
            self.queue.task_done()
    def handle_new_message(self,m):self.queue.put_nowait(m)
    async def _execute_message_processing(self,m):
        contexts=cl.user_session.get("contexts",[]);final_ctx="\n\n".join(c for c in contexts if c);txt,elements=(m.content or"").strip(),m.elements
        if elements:f_ctxs=await asyncio.gather(*[Tools.process_file(e)for e in elements]);final_ctx+="\n"+"\n".join(c for c in f_ctxs if c)
        if final_ctx:txt=f"{final_ctx}\n\nپرسش اصلی:{txt}";cl.user_session.set("contexts",[])
        cid=cl.user_session.get("cid")or await self.db.create_conversation(APP_CONFIG.USER_ID,txt[:50])
        if not cl.user_session.get("cid"):cl.user_session.set("cid",cid);await self.render_sidebar()
        await self.db.append_message(cid,APP_CONFIG.USER_ID,"user",txt);self.vstore.add(txt,{'cid':cid,'txt':txt})
        short_mem=await self.db.get_messages(cid);long_mem=await self.vstore.search(txt,2,cid)
        rag_ctx="[[حافظه]]\n"+"\n".join(f"- {t}" for t in long_mem)if long_mem else ""
        await self.display_history(cid);
        model=cl.user_session.get("settings",{}).get("model_id",DEFAULT_MODEL_ID);resp=cl.Message("",author="Saino");await resp.send()
        full_resp="".join([t async for t in self.stream_gemini(short_mem,model,rag_ctx)]);
        if full_resp:await resp.update(full_resp);await self.db.append_message(cid,APP_CONFIG.USER_ID,"assistant",full_resp)

    async def setup_session(self):await cl.Avatar(name="User",url="/public/user.png").send();await self.render_sidebar();await cl.Message("### Saino\nآماده.").send();
    if not self.vstore.is_enabled:await cl.Message("⚠️ حافظه بلندمدت غیرفعال است.",author="سیستم").send()
    async def render_sidebar(self):convs=await self.db.get_conversations(APP_CONFIG.USER_ID);actions=[cl.Action(n,l)for n,l in[(ACTION.NEW_CONV,"➕ مکالمه جدید"),(ACTION.SHOW_NOTES,"🗒️یادداشت‌ها"),(ACTION.ADD_CTX_WEB,"🌐 وب"),(ACTION.SUMMARIZE_CONV,"✍️خلاصه"),(ACTION.NUKE_DATABASE,"🗑️ پاک‌سازی")]];conv_hist=[cl.Action(ACTION.SELECT_CONV,str(c["_id"]),f"💬 {c.get('title','...')}")for c in convs];await cl.set_actions(actions+conv_hist)
    async def stream_gemini(self,h,m,c=None):
        if c:h.insert(-1,{"role":"user","text":c});api_hist=[{"role":msg.get("role"),"parts":[{"text":msg.get("text","")}]}for msg in h];
        stream=await genai.GenerativeModel(m).generate_content_async(api_hist,stream=True);
        async for chunk in stream:
            if text:=getattr(chunk,"text",None):yield text
    async def display_history(self,cid):await cl.empty_chat();[await cl.Message(m.get('text',''),author="User"if m.get('role')=="user"else"Saino").send() for m in await self.db.get_messages(cid,50)]
    async def show_all_notes(self):
        notes=await self.db.get_notes(APP_CONFIG.USER_ID)
        if not notes:await cl.Message("یادداشتی ثبت نشده.").send();return
        for n in notes:await cl.Message(f"{n['text']}\n`تگ‌ها:{','.join(n['tags'])}`" if n['tags'] else n['text'],actions=[cl.Action(ACTION.DELETE_NOTE,str(n["_id"]),"حذف")]).send()
    async def summarize_conversation(self,cid):
        cached=await self.db.get_from_cache(f"summary:{cid}");
        if cached:await cl.Message(cached,author="Saino").send();return
        m=await cl.Message("درحال خلاصه‌سازی...",author="سیستم");await m.send();txt=await self.db.get_all_messages_text(cid)
        summary="".join([t async for t in self.stream_gemini([{"role":"user","text":f"خلاصه کن:\n{txt}"}],SUMMARIZE_MODEL_ID)])
        await m.update(summary,author="Saino");await self.db.set_in_cache(f"summary:{cid}",summary)


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
            due=await DB_INSTANCE.get_due_reminders();
            for r in due:await cl.Message(f"**یادآور:** {r.get('text')}",author="سیستم").send();await DB_INSTANCE.mark_reminder_sent(r['_id'])
        except Exception as e:logger.error(f"Reminder task error: {e}", exc_info=True)
        finally:await asyncio.sleep(60)


# ----------------------------------------------------------------------
# بخش ۷: کنترل‌گرهای رابط کاربری
# ----------------------------------------------------------------------
@cl.on_chat_start
async def on_chat_start():
    global BACKGROUND_TASKS_STARTED;await CHAT_MANAGER.setup_session()
    if not BACKGROUND_TASKS_STARTED:asyncio.create_task(check_reminders());BACKGROUND_TASKS_STARTED=True
@cl.on_message
async def on_message(m:cl.Message):CHAT_MANAGER.handle_new_message(m)
@cl.on_action
async def on_action(action: cl.Action):
    match action.name:
        case ACTION.ADD_CTX_WEB:
            res=await cl.AskUserMessage("چه چیزی را جستجو کنم؟").send();
            if res and(q:=res.get('output')):
                m=await cl.Message("درحال جستجو...",author="سیستم");await m.send()
                ctx=await Tools.web_search(q,DB_INSTANCE)
                if ctx:contexts=cl.user_session.get("contexts",[]);contexts.append(ctx);cl.user_session.set("contexts",contexts);await m.update(f"✅ {len(contexts)} زمینه فعال است.")
                else:await m.delete()
        case ACTION.NUKE_DATABASE:
            res=await cl.AskActionMessage("آیا از پاک‌سازی کامل داده‌ها مطمئن هستید؟",actions=[cl.Action("confirm","بله"),cl.Action("cancel","خیر")]).send()
            if res and res.get('name')=='confirm':
                m=await cl.Message("در حال پاک‌سازی...",author="سیستم");await m.send();await DB_INSTANCE.nuke(APP_CONFIG.USER_ID);cl.user_session.set("current_conv_id",None);await CHAT_MANAGER.render_sidebar();await m.update("✅ پاک‌سازی کامل شد.");await cl.empty_chat()
        case ACTION.NEW_NOTE_MODAL:
            res=await cl.AskActionMessage("یادداشت جدید",actions=[cl.Action("confirm","ثبت")],inputs=[cl.TextInput("content","متن",required=True),cl.TextInput("tags","تگ‌ها (با کاما جدا کنید)"),cl.TextInput("reminder","یادآور (YYYY-MM-DDTHH:MM)")]).send()
            if res and res.get('name')=='confirm':
                tags=[t.strip()for t in res.get('tags','').split(',')if t.strip()];remind=datetime.fromisoformat(res.get('reminder'))if res.get('reminder')else None;await DB_INSTANCE.create_note(APP_CONFIG.USER_ID,res['content'],tags,remind);await cl.Message("یادداشت ثبت شد.").send()
        case ACTION.NEW_CONV:cl.user_session.set("current_conv_id",None);await cl.empty_chat()
        case ACTION.SELECT_CONV:cl.user_session.set("current_conv_id",action.value);await CHAT_MANAGER.display_history(action.value)
        case ACTION.SHOW_NOTES:await CHAT_MANAGER.show_all_notes()
        case ACTION.DELETE_NOTE:await DB_INSTANCE.delete_note(action.value);await CHAT_MANAGER.show_all_notes()
        case ACTION.SUMMARIZE_CONV:
            if cid:=cl.user_session.get("current_conv_id"):await CHAT_MANAGER.summarize_conversation(cid)
            else:await cl.Message("ابتدا یک مکالمه را انتخاب کنید.",author="سیستم").send()
