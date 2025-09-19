# README.md — Jarvis Argus (نسخهٔ Chainlit — مستندات پروژه)

**نام پروژه:** Jarvis Argus — Chat & Media Generator
**فایل اصلی اجرا:** `app.py`
**هدف:** اپ چت مبتنی بر Chainlit که پاسخ‌های متنی را با Google Gemini به‌صورت استریم توکنی نمایش می‌دهد، از MongoDB برای ذخیره‌سازی استفاده می‌کند، و پشتیبانی پایه‌ای از آپلود تصویر در چت دارد.

---

## 📌 مروری کوتاه (Overview)

Jarvis Argus یک چت‌بات سرور‌محور است که:

* ساختار OOP با کلاس `JarvisArgus` دارد.
* احراز هویت با `bcrypt` و JWT انجام می‌شود.
* کاربران، مکالمات و پیام‌ها در MongoDB ذخیره می‌شوند (pymongo sync اما فراخوانی‌ها داخل `asyncio.to_thread` اجرا می‌شوند).
* استریم پاسخ‌های Google Gemini به صورت incremental (producer در thread → `asyncio.Queue` → consumer async) پیاده‌سازی شده است.
* امکان آپلود تصویر در پیام وجود دارد؛ تصویر به صورت اولیه به `data:` URI تبدیل می‌شود (قابل ارتقا به آپلود ابری).
* UI با Chainlit ساخته شده و قابلیت انتخاب مدل توسط کاربر فراهم است.

---

## ✨ قابلیت‌های اصلی (Features)

* ثبت‌نام و ورود کاربران با JWT.
* ایجاد، ذخیره و بارگذاری مکالمات (collection `conversations`).
* استریم زندهٔ پاسخ‌های مدل (Gemini) و نمایش تدریجی توکن‌ها در UI.
* پشتیبانی پایهٔ ارسال تصویر در پیام‌ها.
* انتخاب مدل‌های مختلف (MODELS با RPM/RPD و توضیحات).
* محدودسازی سرعت ارسال پیام (rate-limit ساده).

---

## 🔧 پیش‌نیازها (Prerequisites)

* Python 3.10+ (توصیه: 3.11 یا 3.12)
* MongoDB (Atlas یا محلی)
* دسترسی به کلید Google Generative AI (در صورت استفاده از Gemini)
* پکیج‌های مورد نیاز (نمونه در `requirements.txt` — بخش پایین)

---

## 🧾 متغیرهای محیطی مورد نیاز (Environment Variables)

در فایل `.env` یا متغیرهای محیطی سیستم مقدارهای زیر را قرار دهید:

```env
MONGO_URI="mongodb+srv://<username>:<password>@cluster0.mongodb.net/jarvis_argus_db?retryWrites=true&w=majority"
GEMINI_API_KEY="sk-..."          # اختیاری مگر اینکه از Gemini استفاده کنید
JWT_SECRET_KEY="a-very-secret"   # ضروری — از مقدار قوی استفاده کنید
CHAINLIT_API_KEY=""              # اختیاری
```

> **نکته امنیتی:** برای production از Secret Manager سرویس میزبان استفاده کنید و `.env` را در گیت قرار ندهید.

---

## ▶️ نصب و اجرای محلی (Run locally)

1. نصب وابستگی‌ها:

```bash
pip install -r requirements.txt
```

2. تنظیم متغیرهای محیطی (نمونه لینوکس/macOS):

```bash
export MONGO_URI="..."
export GEMINI_API_KEY="..."
export JWT_SECRET_KEY="..."
```

3. اجرا:

```bash
# فایل اصلی پروژه: app.py
chainlit run app.py
# یا مشخص کردن پورت:
chainlit run app.py --port 8000
```

پس از اجرا، آدرس محلی در خروجی نمایش داده می‌شود (معمولاً [http://localhost:8000](http://localhost:8000)).

---

## 🧭 نحوهٔ استفاده (Usage / Quick guide)

### فرمان‌های سریع داخل چت (متن)

در حال حاضر احراز هویت و مدیریت مکالمات از طریق فرمان‌های متنی انجام می‌شود:

* `/help` — نمایش فهرست دستورات
* `/signup <name> <email> <password>` — ثبت‌نام
* `/login <email> <password>` — ورود
* `/new` — شروع مکالمهٔ جدید
* `/convs` — نمایش فهرست مکالمات شما
* `/select <conv_id>` — انتخاب یک مکالمه و بارگذاری پیام‌ها

### انتخاب مدل

در شروع چت کاربر می‌تواند از طریق ChatSettings یک مدل را انتخاب کند (مقادیر از متغیر `MODELS` ساخته می‌شوند). شناسهٔ مدل در `cl.user_session["selected_model_id"]` قرار می‌گیرد.

### آپلود تصویر

* وقتی کاربر تصویر ارسال می‌کند، فایل بررسی و به data URI تبدیل می‌شود (`_image_element_to_data_uri`) و به تاریخچه پیام اضافه می‌گردد.
* توصیه: برای جلوگیری از غیرضروری‌شدن حجم دیتابیس، در پروژهٔ بعدی تصاویر را به S3/GCS آپلود کنید و فقط URL را ذخیره کنید.

---

## 🏗️ معماری داخلی (How it works)

1. کاربر پیام یا فرمان ارسال می‌کند (`@cl.on_message`).
2. اگر فرمان است، `handle_command` آن را مدیریت می‌کند.
3. اگر پیام متنی/چندرسانه‌ای است، `handle_user_message` پیام را پردازش، در session ذخیره و به DB append می‌کند.
4. تاریخچه پیام‌ها (user + assistant) به صورت `api_history` تبدیل می‌شود.
5. `stream_gemini_to_chainlit` در یک thread تولید را از SDK می‌گیرد و توکن‌ها را در `asyncio.Queue` می‌گذارد؛ consumer توکن‌ها را با `msg.stream_token(...)` استریم می‌کند.
6. پیام نهایی assistant در DB ذخیره می‌شود.

---

## ⚙️ فایل‌ها و ساختار پیشنهادی پروژه

```
.
├─ app.py                # اپ اصلی (Chainlit) — فایل اجرا
├─ requirements.txt      # وابستگی‌ها
├─ Dockerfile            # نمونهٔ داکر
├─ docker-compose.yml    # نمونه (App + Mongo برای dev)
├─ .env.example          # نمونه متغیرهای محیطی
├─ README.md             # این فایل
└─ LICENSE               # (اختیاری)
```

---

## ✅ پیشنهاد محتوی `requirements.txt`

```text
chainlit>=1.3.0
pymongo>=4.3.0
bcrypt>=4.0.0
PyJWT>=2.8.0
python-dotenv>=1.0.0
google-generativeai>=0.12.0
uvicorn>=0.22.0
```

(در صورت نیاز می‌توانم نسخه‌ها را دقیق‌تر یا مینیمال کنم.)

---

## 🐳 Docker — نمونه و راهنمایی (اختیاری)

### نمونه `Dockerfile` (ساده — مناسب dev/staging)

```dockerfile
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
```

### نمونه `docker-compose.yml` (توسعه: app + mongo)

```yaml
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
```

**توصیه‌ها:**

* برای production از secret manager سرویس میزبانی استفاده کنید.
* کانتینر را non-root اجرا کنید.
* پشت reverse-proxy (Nginx/Traefik) قرار دهید تا TLS termination و rate-limiting انجام شود.
* healthcheck و resource limits را تنظیم کنید.

---

## 🚀 اجرا در Render / Heroku / Railway

* متغیرهای محیطی (`MONGO_URI`, `GEMINI_API_KEY`, `JWT_SECRET_KEY`) را از داشبورد سرویس تنظیم کنید.
* دستور Start را روی `chainlit run app.py` قرار دهید (در صورت نیاز پورت را از متغیر محیطی خوانده و به `--port` پاس دهید).
* اگر سرویس محدودیت پورت دارد، Chainlit را با همان پورت اجرا کنید.

---

## 🧪 رفع خطا و عیب‌یابی (Troubleshooting)

* **اتصال MongoDB ناموفق**: بررسی کنید `MONGO_URI` صحیح باشد و اگر از Atlas استفاده می‌کنید، IP allowlist و credentials را تنظیم کرده باشید.
* **خطاهای JWT / Token**: مقدار `JWT_SECRET_KEY` را بررسی و توکن‌ها را دوباره بگیرید.
* **خطاهای Gemini**: مطمئن شوید `GEMINI_API_KEY` معتبر است و quota کافی دارید؛ خطای SDK را در لاگ نگاه کنید.
* **مشکل event loop**: فراخوانی‌های DB داخل `asyncio.to_thread` انجام شده‌اند؛ در صورت خطا لاگ کامل را paste کنید تا بررسی کنم.

---

## 🔐 امنیت (نکات مهم)

* `JWT_SECRET_KEY` را قوی و محافظت‌شده نگه دارید.
* رمزها همیشه با `bcrypt` هش می‌شوند.
* قبل از ذخیره فایل‌ها محدودیت اندازه و نوع MIME را بررسی کنید.
* در production از HTTPS، تنظیمات CORS محدود و rate-limiting استفاده کنید.

---

## ♻️ توسعهٔ آتی (Roadmap پیشنهادی)

* تغییر ذخیره‌سازی تصاویر به S3/GCS و ذخیره فقط URL در DB.
* فرم گرافیکی ثبت‌نام/ورود (Chainlit Form یا فرانت‌اند جدا) به‌جای فرمان متنی.
* افزودن like/dislike و جمع‌آوری بازخورد برای هر پاسخ.
* لایهٔ quota/cost-control براساس RPM/RPD برای مدیریت هزینهٔ API.
* اضافه کردن لاگ و مانیتورینگ (Sentry / Prometheus).

---

## مشارکت (Contributing)

* Pull requests خوش‌آمدیده؛ لطفاً قبل از ارسال PR یک issue باز کنید و تغییرات را توضیح دهید.
* برای تست محلی از `docker-compose` استفاده کنید تا Mongo محلی داشته باشید.

---

## مجوز (License)

MIT

--
