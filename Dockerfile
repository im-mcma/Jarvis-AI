# از یک ایمیج پایه رسمی پایتون نسخه 3.12-slim استفاده می‌کنیم تا حجم ایمیج کم باشه.
FROM python:3.12-slim

# نصب پکیج‌های سیستمی لازم برای کامپایل پکیج‌های پایتون.
# این کار از خطاهای زمان نصب مثل 'gcc not found' جلوگیری می‌کنه.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# پوشه کاری رو در کانتینر /app تعیین می‌کنیم.
# این کار به خوانایی دستورات کمک می‌کنه.
WORKDIR /app

# فایل‌های مهم رو از سیستم محلی به کانتینر کپی می‌کنیم.
# این فایل‌ها شامل requirements.txt و start.sh هستند.
COPY requirements.txt ./
COPY start.sh ./

# وابستگی‌های پایتون رو از requirements.txt نصب می‌کنیم.
# از --no-cache-dir استفاده می‌کنیم تا حجم ایمیج کم بمونه.
RUN pip install --no-cache-dir -r requirements.txt

# مطمئن می‌شیم که اسکریپت start.sh قابل اجراست.
RUN chmod +x ./start.sh

# بقیه فایل‌های پروژه (مثل Saino.py و پوشه‌ها) رو به کانتینر کپی می‌کنیم.
COPY . .

# پورت پیش‌فرض Chainlit (8000) رو در معرض دید قرار می‌دیم.
# Render از این اطلاعات استفاده می‌کنه تا پورت بیرونی رو به این پورت داخلی وصل کنه.
EXPOSE 8000

# دستور نهایی برای اجرای برنامه
# این دستور اسکریپت start.sh رو با استفاده از bash اجرا می‌کنه.
# start.sh شامل دستورات لازم برای اجرای Chainlit با پورت صحیح هست.
CMD ["bash", "start.sh"]
