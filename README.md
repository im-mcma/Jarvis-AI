Jarvis Argus - نسخه حرفه‌ایJarvis Argus یک پلتفرم چت پیشرفته و مدرن است که با استفاده از Python، Chainlit، و Google Gemini API ساخته شده است. این پروژه با معماری ماژولار و رابط کاربری کاملاً تعاملی، یک تجربه کاربری روان و حرفه‌ای را برای تعامل با مدل‌های زبان بزرگ فراهم می‌کند.![نمایی از رابط کاربری برنامه]✨ ویژگی‌های کلیدیمعماری ماژولار: کد به سه کلاس اصلی تقسیم شده (AuthManager, DBManager, ChatManager) که توسعه و نگهداری را آسان می‌کند.رابط کاربری تعاملی: بدون نیاز به دستورات متنی؛ تمام عملیات از طریق دکمه‌ها و منوهای گرافیکی انجام می‌شود.نوار کناری پویا: مشاهده، انتخاب، تغییر نام و حذف مکالمات به صورت زنده.مدیریت کامل پیام‌ها: قابلیت ویرایش، حذف و کپی هر پیام در تاریخچه چت.پشتیبانی چندوجهی (Multimodal): امکان آپلود تصویر همراه با متن برای تحلیل توسط مدل‌های Vision.انتخاب دینامیک مدل: کاربر می‌تواند مدل هوش مصنوعی مورد نظر خود را از طریق تنظیمات چت انتخاب کند.احراز هویت امن: سیستم ورود و ثبت‌نام با استفاده از JWT (JSON Web Tokens) و هشینگ رمز عبور.پایگاه داده NoSQL: ذخیره‌سازی تمام اطلاعات کاربران و مکالمات در MongoDB.🛠️ پشته فناوری (Tech Stack)فریم‌ورک اصلی: Chainlitمدل هوش مصنوعی: Google Gemini APIپایگاه داده: MongoDBزبان برنامه‌نویسی: Python 3.9+🚀 راه‌اندازی و نصب محلیبرای اجرای پروژه روی سیستم خود، مراحل زیر را دنبال کنید.۱. پیش‌نیازهاPython 3.9 یا بالاتردسترسی به یک پایگاه داده MongoDB (می‌توانید از نسخه رایگان MongoDB Atlas استفاده کنید)کلید API برای Google Gemini (از Google AI Studio دریافت کنید)۲. کلون کردن پروژهgit clone <URL_REPOSITORY_شما>
cd <DIRECTORY_پروژه>
۳. نصب وابستگی‌هاتوصیه می‌شود که از یک محیط مجازی (virtual environment) استفاده کنید.python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
محتوای فایل requirements.txt شما باید شامل موارد زیر باشد:chainlit
python-dotenv
pymongo
bcrypt
pyjwt
google-generativeai
Pillow
۴. پیکربندی متغیرهای محیطییک فایل با نام .env در ریشه پروژه ایجاد کرده و اطلاعات زیر را در آن قرار دهید:# آدرس اتصال به پایگاه داده MongoDB Atlas
MONGO_URI="mongodb+srv://<user>:<password>@<cluster-url>/?retryWrites=true&w=majority"

# کلید API شما از Google AI Studio
GEMINI_API_KEY="YOUR_GEMINI_API_KEY"

# یک کلید مخفی برای امضای توکن‌های JWT (یک رشته تصادفی و طولانی انتخاب کنید)
JWT_SECRET_KEY="YOUR_SUPER_SECRET_JWT_KEY"
۵. اجرای برنامهبا دستور زیر، برنامه را اجرا کنید:chainlit run app_final.py -w --port 8000
فلگ -w باعث می‌شود برنامه با هر تغییر در کد، به صورت خودکار رفرش شود.اکنون می‌توانید برنامه را در مرورگر خود روی آدرس http://localhost:8000 مشاهده کنید.☁️ استقرار روی RenderRender یک پلتفرم ابری عالی برای استقرار این نوع برنامه‌هاست.آماده‌سازی پروژه: مطمئن شوید که پروژه شما روی یک ریپازیتوری گیت (مانند GitHub) قرار دارد.ساخت سرویس جدید در Render:وارد داشبورد Render شوید و روی New + > Web Service کلیک کنید.ریپازیتوری گیت خود را متصل کنید.پیکربندی سرویس:Name: یک نام برای سرویس خود انتخاب کنید (مثلاً jarvis-argus-app).Region: نزدیک‌ترین منطقه جغرافیایی را انتخاب کنید.Branch: شاخه‌ای که می‌خواهید مستقر شود را انتخاب کنید (معمولاً main یا master).Runtime: Python 3 را انتخاب کنید.Build Command: pip install -r requirements.txtStart Command: chainlit run app_final.py --port $PORT --headlessنکته مهم: استفاده از متغیر $PORT و فلگ --headless برای استقرار روی Render ضروری است.افزودن متغیرهای محیطی:به تب Environment بروید.روی Add Environment Variable کلیک کرده و سه متغیر MONGO_URI، GEMINI_API_KEY و JWT_SECRET_KEY را با مقادیر واقعی خود (همانند فایل .env) اضافه کنید.استقرار:روی دکمه Create Web Service کلیک کنید. Render به صورت خودکار وابستگی‌ها را نصب و برنامه را اجرا می‌کند. پس از چند دقیقه، برنامه شما روی یک آدرس عمومی در دسترس خواهد بود.🐳 (اختیاری) استقرار با Dockerیک فایل Dockerfile ساده برای کانتینرسازی برنامه:# Use the official Python image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port Chainlit runs on
EXPOSE 8000

# Run the application
CMD ["chainlit", "run", "app_final.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
نحوه ساخت و اجرای ایمیج داکر:# ساخت ایمیج
docker build -t jarvis-argus .

# اجرای کانتینر (با پاس دادن متغیرهای محیطی)
docker run -p 8000:8000 --env-file .env jarvis-argus
📜 مجوز (License)این پروژه تحت مجوز MIT منتشر شده است. برای اطلاعات بیشتر فایل LICENSE را مطالعه کنید.
