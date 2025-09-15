# Jarvis v7.2 - Apex Edition

یک اپلیکیشن چت هوش مصنوعی قدرتمند و زیبا، ساخته شده با FastAPI، Google Gemini و Firestore که برای استقرار آسان روی Render بهینه شده است.

![نمایی از رابط کاربری جارویس](https://i.imgur.com/your-screenshot.png) ## ✨ ویژگی‌ها

-   **چت Real-time:** پاسخ‌ها به صورت زنده و کلمه به کلمه (Stream) نمایش داده می‌شوند.
-   **پشتیبانی از مدل‌های Gemini:** امکان انتخاب بین مدل‌های مختلف گوگل (Pro, Flash و...).
-   **تاریخچه مکالمات:** ذخیره، بارگذاری و حذف مکالمات در پایگاه داده Firestore.
-   **تولید هوشمند عنوان:** عنوان هر چت جدید به صورت خودکار بر اساس اولین پیام کاربر ساخته می‌شود.
-   **رابط کاربری مدرن:** طراحی زیبا با افکت شیشه‌ای (Glassmorphism) و انیمیشن‌های نرم.
-   **کدبلاک‌های حرفه‌ای:** نمایش کدها با Syntax Highlighting و دکمه کپی آسان.
-   **آماده برای استقرار:** بهینه‌سازی شده برای استقرار سریع و آسان روی پلتفرم Render.

## 🛠️ پشته فناوری (Tech Stack)

-   **Backend:** FastAPI, Uvicorn, Python 3.9+
-   **AI Model:** Google Gemini API
-   **Database:** Google Cloud Firestore
-   **Frontend:** HTML, Tailwind CSS, JavaScript

## 🚀 راه‌اندازی و نصب محلی

برای اجرای این پروژه روی کامپیوتر خود، مراحل زیر را دنبال کنید.

### پیش‌نیازها

1.  **Python:** نسخه 3.9 یا بالاتر.
2.  **حساب Google Cloud:** برای استفاده از Firestore و ساخت فایل Credentials.
3.  **کلید API جمینای:** از [Google AI Studio](https://ai.google.dev/) دریافت کنید.

### مراحل نصب

1.  **کلون کردن ریپازیتوری:**
    ```bash
    git clone [https://github.com/your-username/jarvis-project.git](https://github.com/your-username/jarvis-project.git)
    cd jarvis-project
    ```

2.  **ساخت و فعال‌سازی محیط مجازی:**
    ```bash
    # For macOS/Linux
    python3 -m venv venv
    source venv/bin/activate

    # For Windows
    python -m venv venv
    .\venv\Scripts\activate
    ```

3.  **نصب بسته‌های مورد نیاز:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **تنظیم متغیرهای محیطی و اعتبارسنجی:**

    **روش اول (پیشنهادی و اولویت‌دار): فایل `credentials.json`**
    -   فایل کلید سرویس اکانت خود را از Google Cloud دانلود کرده، نام آن را به `credentials.json` تغییر دهید و آن را در ریشه اصلی پروژه (کنار `app.py`) قرار دهید.

    **روش دوم (برای سرور): فایل `.env`**
    -   یک فایل با نام `.env` در ریشه پروژه بسازید و کلید API جمینای خود را در آن قرار دهید:
        ```
        GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
        ```
    -   *برای استفاده از این روش برای Firestore، باید محتوای فایل `credentials.json` را در یک متغیر دیگر به نام `GOOGLE_CREDENTIALS_JSON` قرار دهید.*

5.  **اجرای سرور:**
    ```bash
    uvicorn app:app --reload
    ```
    اکنون اپلیکیشن در آدرس `http://127.0.0.1:8000` در دسترس است.

---
