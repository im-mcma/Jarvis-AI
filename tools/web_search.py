# tools/web_search.py

# --- وارد کردن وابستگی‌ها ---
from tools.base import BaseTool
import asyncio
import logging
from tavily import TavilyClient
import chainlit as cl

# ایجاد یک logger مخصوص این ابزار
logger = logging.getLogger(__name__)

# وارد کردن متغیرهای سراسری از فایل اصلی Saino
from Saino import CFG

# ----------------------------------------------------------------------

class WebSearchTool(BaseTool):
    """
    ابزاری برای جستجوی به‌روز و واقعی در وب با استفاده از سرویس Tavily.
    """
    # ۱. تعریف نام و توضیحات ابزار
    name = "web_search"
    description = "برای یافتن اطلاعات به‌روز و واقعی در وب جستجو می‌کند. برای سوالات مربوط به رویدادهای اخیر، افراد، یا موضوعات خاص استفاده شود."
    
    # ۲. تعریف پارامترهای ورودی با JSON Schema
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "عبارت دقیق برای جستجو"
            }
        },
        "required": ["query"],
    }

    def __init__(self):
        # بررسی وجود API Key در زمان نمونه‌سازی
        self.tavily = TavilyClient(api_key=CFG.TAVILY_API_KEY) if CFG.TAVILY_API_KEY else None

    # ۳. پیاده‌سازی منطق اجرایی ابزار
    async def execute(self, query: str) -> dict:
        logger.info(f"WebSearchTool برای کوئری '{query}' فراخوانی شد.")
        
        # بررسی پیکربندی
        if not self.tavily:
            error_msg = "سرویس جستجوی Tavily پیکربندی نشده است. لطفا TAVILY_API_KEY را در فایل .env تنظیم کنید."
            logger.error(error_msg)
            return {"status": "error", "error": error_msg}
        
        # اطلاع‌رسانی به کاربر
        msg = cl.Message(content=f"🔍 در حال جستجو برای: `{query}` ...", author="System")
        await msg.send()
        
        try:
            # اجرای جستجو در یک Thread مجزا
            response = await asyncio.to_thread(
                self.tavily.search, query=query, search_depth="advanced", include_answer=True
            )
            
            answer = response.get("answer", "")
            results = response.get("results", [])
            
            # آماده‌سازی خروجی برای کاربر
            summary = f"### خلاصه جستجو برای `{query}`\n\n{answer}\n\n" if answer else ""
            md_links = "### منابع:\n"
            for res in results[:5]:
                md_links += f"- [{res.get('title', 'بدون عنوان')}]({res.get('url', '#')})\n"

            final_content = summary + md_links
            await msg.update(content=final_content)
            
            # Return a summary for the model to process
            return {"status": "ok", "data": answer or "خلاصه‌ای یافت نشد. نتایج در رابط کاربری نمایش داده شد."}
            
        except Exception as e:
            logger.exception(f"خطا در ابزار جستجوی وب برای کوئری: {query}")
            await msg.update(content=f"متاسفانه در حین جستجو خطایی رخ داد: {e}")
            return {"status": "error", "error": str(e)}
