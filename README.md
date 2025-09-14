# Jarvis-Gemini

نسخه ساده‌شده و تمیز فقط برای مدل‌های Gemini/Gemma.

## ویژگی‌ها
- پشتیبانی از مدل‌های Gemini 2.5, 2.0, 1.5 و Gemma.
- endpoint واحد برای Chat.
- WebSocket برای گفتگو زنده.
- UI مدرن با تم شب.
- favicon سفارشی (`five.ico`).
- قابل اجرا روی Render.com.

## نصب محلی
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
