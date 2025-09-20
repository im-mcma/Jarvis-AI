# tools/file_search.py

# --- وارد کردن وابستگی‌ها ---
from tools.base import BaseTool
import logging
import chainlit as cl

# وارد کردن کلاس‌های اصلی از فایل Saino.py
from Saino import DB, DocumentChunk

# ایجاد یک logger مخصوص این ابزار
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------

class FileSearchTool(BaseTool):
    """
    ابزاری برای جستجو در اسناد و فایل‌های آپلود شده توسط کاربر در فضای کاری.
    """
    # ۱. تعریف نام و توضیحات ابزار
    name = "search_in_documents"
    description = "در اسناد و فایل‌هایی که کاربر در این فضای کاری آپلود کرده است جستجو می‌کند تا به سوالات مربوط به محتوای آن‌ها پاسخ دهد."

    # ۲. تعریف پارامترهای ورودی با JSON Schema
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "عبارت کلیدی برای جستجو در اسناد"
            }
        },
        "required": ["query"],
    }

    # ۳. پیاده‌سازی منطق اجرایی ابزار
    async def execute(self, query: str) -> dict:
        logger.info(f"FileSearchTool با کوئری '{query}' فراخوانی شد.")
        try:
            user = cl.user_session.get("user")
            workspace_id = cl.user_session.get("workspace_id")
            if not user or not workspace_id:
                return {"status": "error", "error": "جلسه کاربر یا فضای کاری نامعتبر است."}

            # جستجو بر اساس کلیدواژه
            search_query = {
                "user_id": user.identifier,
                "workspace_id": workspace_id,
                "content": {"$regex": query, "$options": "i"}
            }
            
            chunks = await DB.find("documents", search_query, DocumentChunk, limit=10)
            
            if not chunks:
                return {"status": "ok", "data": "هیچ اطلاعات مرتبطی در اسناد آپلود شده برای این فضای کاری یافت نشد."}

            context = "\n---\n".join([f"گزیده‌ای از فایل '{c.file_name}':\n{c.content}" for c in chunks])
            
            return {
                "status": "ok", 
                "data": f"بر اساس اسناد شما، اطلاعات زیر یافت شد:\n{context}"
            }
        except Exception as e:
            logger.exception(f"خطا در ابزار جستجوی فایل برای کوئری: {query}")
            return {"status": "error", "error": str(e)}
