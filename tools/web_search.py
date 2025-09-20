# tools/web_search_tool.py
import asyncio
from tavily import TavilyClient
import chainlit as cl
from __main__ import BaseTool, CFG, logger

class WebSearchTool(BaseTool):
    name = "web_search"
    description = "برای یافتن اطلاعات به‌روز و واقعی در وب جستجو می‌کند. برای سوالات مربوط به رویدادهای اخیر، افراد، یا موضوعات خاص استفاده شود."
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "عبارت دقیق برای جستجو"}},
        "required": ["query"],
    }

    def __init__(self):
        self.tavily = TavilyClient(api_key=CFG.TAVILY_API_KEY) if CFG.TAVILY_API_KEY else None

    async def execute(self, query: str) -> dict:
        if not self.tavily:
            return {"status": "error", "error": "سرویس جستجوی Tavily پیکربندی نشده است. لطفا TAVILY_API_KEY را در فایل .env تنظیم کنید."}
        
        msg = cl.Message(content=f"🔍 در حال جستجو برای: `{query}` ...", author="System")
        await msg.send()
        
        try:
            # Run the synchronous Tavily search in a separate thread to avoid blocking asyncio event loop
            response = await asyncio.to_thread(
                self.tavily.search, query, search_depth="advanced", include_answer=True
            )
            
            answer = response.get("answer", "")
            results = response.get("results", [])
            
            summary = f"### خلاصه جستجو برای `{query}`\n\n{answer}\n\n" if answer else ""
            md_links = "### منابع:\n"
            for res in results[:5]:
                md_links += f"- [{res.get('title', 'بدون عنوان')}]({res.get('url', '#')})\n"

            final_content = summary + md_links
            await msg.update(content=final_content)
            
            # Return a summary for the model to process
            return {"status": "ok", "summary": answer or "خلاصه‌ای یافت نشد. نتایج در رابط کاربری نمایش داده شد."}
        except Exception as e:
            logger.exception(f"خطا در ابزار جستجوی وب برای کوئری: {query}")
            await msg.update(content=f"متاسفانه در حین جستجو خطایی رخ داد: {e}")
            return {"status": "error", "error": str(e)}
