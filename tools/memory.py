# tools/memory_tool.py
from __main__ import BaseTool, DB, Memory, logger
import chainlit as cl

class MemoryTool(BaseTool):
    name = "manage_memory"
    description = "حافظه بلندمدت Agent را مدیریت می‌کند. برای به خاطر سپردن (add) یا یادآوری (retrieve) اطلاعات کلیدی استفاده می‌شود."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", 
                "description": "عملیات مورد نظر",
                "enum": ["add", "retrieve"]
            },
            "content": {
                "type": "string", 
                "description": "محتوایی که باید به حافظه اضافه شود یا کلیدواژه‌ای برای جستجو در حافظه."
            }
        },
        "required": ["action", "content"],
    }

    async def execute(self, action: str, content: str) -> dict:
        try:
            user = cl.user_session.get("user")
            workspace_id = cl.user_session.get("workspace_id")
            if not user or not workspace_id:
                return {"status": "error", "error": "جلسه کاربر نامعتبر است."}

            if action == "add":
                memory_entry = Memory(user_id=user.identifier, workspace_id=workspace_id, content=content)
                await DB.insert_one("memories", memory_entry)
                return {"status": "ok", "result": f"اطلاعات '{content[:30]}...' با موفقیت به حافظه اضافه شد."}
            
            elif action == "retrieve":
                search_query = {
                    "user_id": user.identifier,
                    "workspace_id": workspace_id,
                    "content": {"$regex": content, "$options": "i"}
                }
                memories = await DB.find("memories", search_query, Memory, limit=5)
                
                if not memories:
                    return {"status": "ok", "result": "هیچ خاطره مرتبطی با این موضوع یافت نشد."}
                
                context = "\n".join([f"- {m.content}" for m in memories])
                return {
                    "status": "ok", 
                    "result": f"بر اساس خاطرات ذخیره شده، اطلاعات زیر بازیابی شد:\n{context}"
                }
            else:
                return {"status": "error", "error": f"عملیات '{action}' نامعتبر است."}

        except Exception as e:
            logger.exception(f"خطا در ابزار مدیریت حافظه برای عملیات {action}")
            return {"status": "error", "error": str(e)}
