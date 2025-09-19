

نام پروژه: Jarvis Argus — Chat & Media Generator

فایل اصلی اجرا: app.py

هدف: اپ چت‌بات سروری با Chainlit که از Google Gemini برای پاسخ‌دهی استریم‌شونده استفاده می‌کند، از MongoDB برای ذخیره‌سازی بهره می‌برد و پشتیبانی پایه‌ای از آپلود تصویر دارد.

📌 مروری کوتاه (Overview)

Jarvis Argus یک چت‌بات سرورمحور است که:



ساختار OOP با کلاس JarvisArgus.

احراز هویت با bcrypt و JWT انجام می‌شود.

کاربران، مکالمات و پیام‌ها در MongoDB ذخیره می‌شوند (pymongo، فراخوانی‌ها داخل asyncio.to_thread).

استریم پاسخ‌های Google Gemini به‌صورت توکنی (producer thread → asyncio.Queue → consumer async).

امکان ارسال تصویر وجود دارد؛ تصویری که کاربر آپلود می‌کند موقتا به data: URI تبدیل می‌شود (قابل ارتقا به آپلود ابری).

UI با Chainlit ساخته شده و کاربر می‌تواند مدل را از طریق ChatSettings انتخاب کند.

✨ قابلیت‌های کلیدی

ثبت‌نام و ورود کاربران با JWT.

ایجاد، ذخیره و بارگذاری مکالمات (collection conversations).

استریم تدریجی پاسخ‌های مدل در UI.

پشتیبانی آپلود تصویر و درج آن در تاریخچهٔ مکالمه.

محدودسازی سادهٔ نرخ ارسال پیام (rate-limit).

🔧 پیش‌نیازها

Python 3.10+ (توصیه‌شده 3.11 یا 3.12)

MongoDB (Atlas یا محلی)

(اختیاری برای Gemini) کلید Google Generative AI (GEMINI_API_KEY)

متغیر JWT_SECRET_KEY

🛠️ متغیرهای محیطی (Environment Variables)

در فایل .env یا در تنظیمات سرویس میزبانی قرار دهید:



MONGO_URI="mongodb+srv://<username>:<password>@cluster0.mongodb.net/jarvis_argus_db?retryWrites=true&w=majority"

GEMINI_API_KEY="sk-..."          # در صورت استفاده از Gemini

JWT_SECRET_KEY="a-very-secret"   # ضروری — حتماً قوی انتخاب شود

CHAINLIT_API_KEY=""              # اختیاری

نکته امنیتی: در production از Secret Manager سرویس میزبان استفاده کنید و .env را در مخزن گیت قرار ندهید.

▶️ نصب و اجرای محلی

نصب وابستگی‌ها:

pip install -r requirements.txt

تنظیم متغیرهای محیطی (مثال لینوکس/macOS):

export MONGO_URI="..."

export GEMINI_API_KEY="..."

export JWT_SECRET_KEY="..."

اجرای اپ:

# فایل اصلی پروژه: app.py

chainlit run app.py

# یا تعیین پورت (مثلاً 8000)

chainlit run app.py --port 8000

پس از اجرا، آدرس محلی در خروجی نمایش داده می‌شود (معمولاً http://localhost:8000).

🧭 معماری و جریان کار (مختصر)

کاربر با /signup یا /login احراز هویت می‌شود؛ اطلاعات در collection users ذخیره می‌شود.

مکالمه‌ها در collection conversations ذخیره می‌شوند؛ هر سند یک آرایهٔ messages دارد.

پیام‌ها در session (cl.user_session["messages"]) و هم‌زمان به‌صورت append در DB ذخیره می‌شوند.

هنگام ارسال پیام، تاریخچه به فرمت مورد نیاز SDK تبدیل و برای Gemini ارسال می‌شود.

پاسخ‌ها در thread تولید و توکن‌ها در asyncio.Queue قرار می‌گیرند؛ consumer آن‌ها را با msg.stream_token(...) در UI نمایش می‌دهد.

پیام نهایی دستیار در DB ذخیره می‌شود.

🧾 دستورهای چت (بر اساس پیاده‌سازی)

/help — نمایش فهرست دستورات

/signup <name> <email> <password> — ثبت‌نام

/login <email> <password> — ورود

/new — شروع مکالمه جدید

/convs — لیست مکالمات کاربر

/select <conv_id> — انتخاب مکالمه و بارگذاری پیام‌ها

در نسخه‌های بعدی می‌توان فرم گرافیکی ثبت‌نام/ورود اضافه کرد تا نیازی به تایپ دستور نباشد.

⚙️ فایل‌ها و ساختار پیشنهادی پروژه

.

├─ app.py                # اپ اصلی (Chainlit)

├─ requirements.txt      # وابستگی‌ها

├─ Dockerfile            # نمونه داکر (اختیاری)

├─ docker-compose.yml    # نمونه (app + mongo) برای توسعه (اختیاری)

├─ .env.example          # نمونه متغیرهای محیطی

├─ README.md             # این فایل

└─ LICENSE               # فایل مجوز (MIT)

✅ محتوای پیشنهادی requirements.txt

chainlit>=1.3.0

pymongo>=4.3.0

bcrypt>=4.0.0

PyJWT>=2.8.0

python-dotenv>=1.0.0

google-generativeai>=0.12.0

uvicorn>=0.22.0

(در صورت تمایل نسخه‌ها را بفرستم دقیق‌تر یا مینیمال‌تر.)

🐳 Docker — نمونهٔ سریع و نکات (اختیاری)

نمونه Dockerfile (ساده — مناسب dev/staging)

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .

RUN apt-get update && apt-get install -y --no-install-recommends \

    gcc libffi-dev build-essential \

    && pip install --upgrade pip \

    && pip install --no-cache-dir -r requirements.txt \

    && apt-get remove -y build-essential gcc \

    && apt-get autoremove -y \

    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN useradd -m appuser && chown -R appuser /app

USER appuser

EXPOSE 8000

CMD ["chainlit", "run", "app.py", "--port", "8000"]

نمونه docker-compose.yml (توسعه: app + mongo)

version: '3.8'

services:

  mongo:

    image: mongo:6.0

    restart: unless-stopped

    environment:

      MONGO_INITDB_ROOT_USERNAME: root

      MONGO_INITDB_ROOT_PASSWORD: example

    volumes:

      - mongo_data:/data/db

    ports:

      - "27017:27017"



  app:

    build: .

    environment:

      MONGO_URI: "mongodb://root:example@mongo:27017/?authSource=admin"

      GEMINI_API_KEY: "${GEMINI_API_KEY}"

      JWT_SECRET_KEY: "${JWT_SECRET_KEY}"

    ports:

      - "8000:8000"

    depends_on:

      - mongo

    volumes:

      - ./:/app

    command: ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]



volumes:

  mongo_data:

🚀 استقرار در Render (راهنمای عملی)

در Render می‌توانید اپ را به دو روش اجرا کنید: (A) با Start Command مستقیم (بدون داکر) یا (B) با Docker. مراحل کلی هر دو روش در ادامه آمده است.



روش A — بدون Docker (اجرای مستقیم روی Render)

در داشبورد Render یک Web Service جدید ایجاد کن.

Source را به ریپوزیتوری گیت متصل کن (GitHub/GitLab).

Build Command: (معمولاً خالی یا pip install -r requirements.txt — Render خودش نصب می‌کند، ولی درج آن امن است)



pip install -r requirements.txt

Start Command: حتماً پورت را از متغیر محیطی $PORT بگیر:



chainlit run app.py --port $PORT

Environment (در بخش Environment): مقدارهای زیر را اضافه کن:

MONGO_URI

GEMINI_API_KEY (اگر استفاده می‌کنی)

JWT_SECRET_KEY

Instance Type: یک instance کوچک (مثلاً 512 MB یا 1GB) برای تست کافی است؛ سپس تست و scale را انجام بده.

Health Check (اختیاری): اگر امکان health endpoint نداری، از قابلیت health check سرویس استفاده کن یا Nginx در جلوی اپ بگذار.

نکته: Render مقدار $PORT را به سرویس اختصاص می‌دهد؛ حتما در Start Command از آن استفاده کن.

روش B — با Docker

در Render یک Web Service نوع Docker بساز.

Repo را متصل کن و Dockerfile موجود در رپو را انتخاب کن.

Environment variables را اضافه کن (MONGO_URI, GEMINI_API_KEY, JWT_SECRET_KEY).

Deploy را بزن.

مزیت Docker: کنترل کامل روی محیط اجرا، نسخهٔ پایتون، و لایه‌های سیستم‌عامل.

🚀 استقرار در Heroku / Railway (خلاصه)

در Heroku یک app بساز و config vars را تنظیم کن (MONGO_URI, GEMINI_API_KEY, JWT_SECRET_KEY).

Procfile برای Heroku:



web: chainlit run app.py --port $PORT

در Railway مشابه Render: پروژه را وصل کن و Environment variables را تنظیم کن؛ در Start Command از $PORT استفاده کن.

🧪 عیب‌یابی (Troubleshooting)

مشکل اتصال MongoDB: MONGO_URI را بررسی کن، اگر Atlas استفاده می‌کنی IP allowlist و credentials را تنظیم کن.

خطاهای JWT: مقدار JWT_SECRET_KEY را بررسی کن، توکن‌ها ممکن است منقضی شده باشند.

خطاهای Gemini: GEMINI_API_KEY معتبر باشد و quota کافی داشته باشد؛ خطاها را در لاگ بررسی کن.

خطای event loop: در این بازنویسی از asyncio.to_thread استفاده شده؛ اگر خطا دیدی، لاگ کامل را ارسال کن تا برسی کنم.

🔐 امنیت و توصیه‌ها

JWT_SECRET_KEY را قوی انتخاب کرده و امن نگهدار.

از HTTPS و reverse-proxy (Nginx/Traefik) برای TLS termination استفاده کن.

لاگ‌ها را به سرویسِ مانیتورینگ (Sentry/Datadog) وصل کن.

فایل‌های آپلودی را پیش از ذخیره محدود کن (max size) و نوع MIME را بررسی کن.

♻️ گسترش‌های پیشنهادی

آپلود تصاویر به S3/GCS و ذخیرهٔ URL به‌جای data-uri.

فرم گرافیکی ثبت‌نام/ورود (Chainlit Form یا فرانت‌اند مجزا).

لایهٔ quota/cost control براساس RPM/RPD.

افزودن دکمه‌های بازخورد و ذخیرهٔ فیدبک در DB.

مجوز (License)

این پروژه تحت مجوز MIT منتشر شده است.

مشارکت (Contributing)

Pull requests خوش‌آمدیده؛ لطفاً قبل از ارسال PR یک issue باز کنید و تغییرات را توضیح دهید. برای تست محلی می‌توانید از docker-compose استفاده کنید تا Mongo محلی داشته باشید.
