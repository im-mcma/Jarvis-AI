# tools/file_search_tool.py
from __main__ import BaseTool, DB, DocumentChunk, logger
import chainlit as cl

class FileSearchTool(BaseTool):
    name = "search_in_documents"
    description = "در اسناد و فایل‌هایی که کاربر در این فضای کاری آپلود کرده است جستجو می‌کند تا به سوالات مربوط به محتوای آن‌ها پاسخ دهد."
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "عبارت کلیدی برای جستجو در اسناد"}},
        "required": ["query"],
    }

    async def execute(self, query: str) -> dict:
        try:
            user = cl.user_session.get("user")
            workspace_id = cl.user_session.get("workspace_id")
            if not user or not workspace_id:
                return {"status": "error", "error": "جلسه کاربر نامعتبر است."}

            # This is a simple keyword search. For better results, a semantic search with embeddings is recommended.
            # We use a regex for case-insensitive search in MongoDB.
            search_query = {
                "user_id": user.identifier,
                "workspace_id": workspace_id,
                "content": {"$regex": query, "$options": "i"}
            }
            
            chunks = await DB.find("documents", search_query, DocumentChunk, limit=10)
            
            if not chunks:
                return {"status": "ok", "result": "هیچ اطلاعات مرتبطی در اسناد آپلود شده برای این فضای کاری یافت نشد."}

            context = "\n---\n".join([f"گزیده‌ای از فایل '{c.file_name}':\n{c.content}" for c in chunks])
            
            return {
                "status": "ok", 
                "result": f"بر اساس اسناد شما، اطلاعات زیر یافت شد:\n{context}"
            }
        except Exception as e:
            logger.exception(f"خطا در ابزار جستجوی فایل برای کوئری: {query}")
            return {"status": "error", "error": str(e)}
