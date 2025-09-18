# README — Jarvis: Senpai

نسخهٔ پروژه: **Jarvis: Senpai** — اپ چت/مولد رسانه با Streamlit و Google Gemini

---

## 📌 خلاصه

این مخزن یک اپلیکیشن Streamlit را پیاده‌سازی می‌کند که امکانات زیر را دارد:

* ورود و ثبت‌نام کاربران با JWT و رمزنگاری `bcrypt`.
* ذخیره‌سازی کاربران، مکالمات و پیام‌ها در MongoDB با استفاده از `motor` (async).
* چت متنی استریم‌شونده با Google Gemini (استفاده از `google.generativeai`).
* تولید رسانه (تصویر/ویدیو) به‌صورت placeholder (قابل تعویض با API واقعی).
* رابط کاربری RTL فارسی با CSS سفارشی.
* مدیریت مدل‌ها (MODELS) با نمایش RPM/RPD و قابلیت‌ها.

---

## 🔧 پیش‌نیازها

* Python 3.10+ (تست شده با 3.11/3.12/3.13)
* MongoDB (Atlas یا محلی)
* دسترسی به کلید Google Generative AI (GEMINI\_API\_KEY)
* بسته‌ها (با `pip` نصب کنید):

```bash
pip install -r requirements.txt
# یا به‌صورت مجزا
pip install streamlit motor pymongo bcrypt pyjwt google-generative-ai python-dotenv nest_asyncio pandas
```

---

## 🛠️ متغیرهای محیطی (Environment Variables)

قبل از اجرا، مقادیر زیر را در محیط سرور یا `.env` تنظیم کنید:

```env
MONGO_URI="mongodb+srv://<username>:<password>@cluster0.mongodb.net/mydb?retryWrites=true&w=majority"
GEMINI_API_KEY="sk-..."
JWT_SECRET_KEY="a-very-secret-key"
```

> **نکته:** در محیط‌های میزبانی مثل Render/Heroku/Render.com متغیرهای محیطی را از داشبورد سرویس تنظیم کنید، نه فایل `.env`.

---

## ▶️ اجرا محلی

```bash
export MONGO_URI="..."
export GEMINI_API_KEY="..."
export JWT_SECRET_KEY="..."
streamlit run app.py
```

یا در ویندوز (PowerShell):

```powershell
$env:MONGO_URI = "..."
$env:GEMINI_API_KEY = "..."
$env:JWT_SECRET_KEY = "..."
streamlit run app.py
```

---

## 📁 ساختار پروژه

```
├─ app.py                # اپ اصلی (Jarvis: Senpai)
├─ requirements.txt      # بسته‌های پایتون پیشنهادی
├─ Dockerfile (اختیاری) # برای ساخت ایمیج داکر
└─ README.md
```

---

## 🧭 معماری و جریان کار

1. کاربر وارد یا ثبت‌نام می‌کند. اطلاعات کاربر در collection `users` ذخیره می‌شود.
2. کاربر مکالمه جدیدی ایجاد می‌کند (collection `conversations`): هر مکالمه یک سند مستقل دارد.
3. پیام‌های کاربر و پاسخ‌ها در آرایهٔ `messages` داخل سند مکالمه ذخیره می‌شوند.
4. هنگام ارسال پیام متنی، تاریخچهٔ پیام‌ها به شکل مورد انتظار SDK به Google Gemini ارسال می‌شود.
5. پاسخ‌ها یا به‌صورت استریم‌شونده به کاربر نمایش داده می‌شوند یا در حالت رسانه، لینک تصویر/ویدیو به کاربر نشان داده و ذخیره می‌شود.

---

## ⚙️ نکات فنی مهم

* **Event loop**: چون Streamlit یک loop داخلی دارد، اپ از `nest_asyncio` استفاده می‌کند تا خطای `RuntimeError: Event loop is closed` رخ ندهد.
* **استریم Gemini**: متدها برای سازگاری با نسخه‌های مختلف SDK پیاده‌سازی شده‌اند؛ اگر SDK شما signature متفاوتی دارد، لازم است `call_gemini_sync` یا متد استریم را مطابق داکیومنت SDK آپدیت کنید.
* **تولید رسانه**: در حال حاضر `generate_media_impl` placeholder است و URLهای تصادفی تولید می‌کند. برای اتصال به API تولید تصویر/ویدیو، آن تابع را با فراخوانی رسمی جایگزین کنید.
* **Pagination پیام‌ها**: پیام‌ها با limit و offset بارگذاری می‌شوند تا سند مکالمه سنگین نشود.

---

## 🧪 تست و خطایابی (Troubleshooting)

* **خطای Event loop is closed**: اگر در اجرا این خطا را دیدید، مطمئن شوید `nest_asyncio.apply()` قبل از اجرای هر کوریوتین فراخوانی شده است. در کد این کار انجام شده.
* **مشکل اتصال MongoDB**:

  * آدرس `MONGO_URI` را بررسی کنید.
  * اگر از Atlas استفاده می‌کنید، IP سروِر را در allow list قرار دهید یا از connection string مناسب استفاده کنید.
* **خطاهای مربوط به Gemini**:

  * کلید API معتبر باشد و quota کافی داشته باشید.
  * نسخهٔ SDK و روش فراخوانی متدها ممکن است تغییر کند — در صورت خطا، پیام خطا و stack trace را بررسی و داکیومنت SDK را چک کنید.

---

## 🚀 استقرار (Deployment)

### Render / Heroku / Railway

* متغیرهای محیطی را تنظیم کنید (`MONGO_URI`, `GEMINI_API_KEY`, `JWT_SECRET_KEY`).
* دستور start را `streamlit run app.py` قرار دهید.

### Docker (نمونه‌ی ساده)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.enableCORS=false"]
```

---

## 🔐 امنیت

* JWT secret را قدرتمند انتخاب کنید.
* مراقب نمایش داده‌های حساس در لاگ‌ها باشید.
* برای production: HTTPS و محدودیت IP و rate-limiting را در لبهٔ شبکه اعمال کنید.

---

## ♻️ توسعه و گسترش

* جایگزینی `generate_media_impl` با API واقعی تولید تصویر/ویدیو.
* افزودن محدودیت و quota کاربر، سیستم اشتراک یا مصرف اعتبار (credits).
* لاگ ساختاریافته به Sentry، Datadog یا هر سرویس مانیتورینگ.
* اضافه کردن تست‌های واحد با mocking برای `motor` و `genai`.

---

## 🧾 لیست مدل‌ها (MODELS)

مدل‌های پیکربندی‌شده در برنامه:

* **چت متنی**:

  * Gemini 2.5 Pro — gemini-2.5-pro
  * Gemini 2.5 Flash — gemini-2.5-flash
  * Gemini 2.5 Flash-Lite — gemini-2.5-flash-lite
  * Gemini 2.0 Pro — gemini-2.0-pro
  * Gemini 2.0 Flash — gemini-2.0-flash

* **تولید تصویر**:

  * Gemini 2.5 Flash Image — gemini-2.5-flash-image-preview
  * Gemini 2.0 Flash Image — gemini-2.0-flash-image

* **تولید ویدیو**:

  * Veo 3 — veo-3

---

## ❓ سوالات متداول

**Q: آیا این برنامه هزینه‌های API را مدیریت می‌کند؟**
A: فعلاً نه. برای مدیریت هزینه باید لایهٔ quota یا credit در سمت سرور اضافه شود.

**Q: آیا داده‌ها رمزنگاری می‌شوند؟**
A: رمز عبور‌ها با bcrypt هش می‌شوند؛ اما محتوای مکالمات در پایگاه‌داده به‌صورت متن ذخیره می‌شود. اگر نیاز به رمزنگاری پیام‌ها دارید، باید از encryption-at-rest یا client-side encryption استفاده کنید.

---

## ✍️ کمک و مشارکت

Pull requests پذیرفته می‌شوند؛ لطفاً در صورت ارسال PR، یک issue باز کنید و تغییرات را توضیح دهید.

---

## 🧾 مجوز

این پروژه تحت مجوز MIT منتشر می‌شود (در صورت نیاز، یک فایل LICENSE اضافه کنید).

---

**تهیه‌شده توسط:** تیم توسعهٔ Jarvis: Senpai
*موفق باشید!*
