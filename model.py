MODEL_INFO = {
    "gemini-2.5-pro": {
        "display": "Gemini 2.5 Pro",
        "rpm": 5, "rpd": 100, "monthly_30d": 3000,
        "inputs": ["audio", "images", "video", "text", "PDF"],
        "outputs": ["text"],
        "note": "مدل «thinking»، کانتکست بسیار بلند؛ تصویر/ویدیو/صدا را تحلیل می‌کند اما خروجی متن است."
    },
    "gemini-2.5-flash": {
        "display": "Gemini 2.5 Flash",
        "rpm": 10, "rpd": 250, "monthly_30d": 7500,
        "inputs": ["text", "images", "video", "audio"],
        "outputs": ["text"],
        "note": "قیمت-کارایی مناسب برای حجم بالا؛ خروجی متن."
    },
    "gemini-2.5-flash-lite": {
        "display": "Gemini 2.5 Flash-Lite",
        "rpm": 15, "rpd": 1000, "monthly_30d": 30000,
        "inputs": ["text", "image", "video", "audio", "PDF"],
        "outputs": ["text"],
        "note": "بهینه‌شده برای Throughput بالا/هزینه کم."
    },
    "gemini-live-2.5-flash-preview": {
        "display": "Gemini 2.5 Flash Live (preview)",
        "rpm": 3, "rpd": None, "monthly_30d": None,
        "inputs": ["audio", "video", "text"], "outputs": ["text", "audio"],
        "note": "حالت low-latency صوت + ویدیو؛ نرخ‌های کامل منتشر نشده."
    },
    "gemini-2.5-flash-preview-native-audio-dialog": {
        "display": "Gemini 2.5 Flash Native Audio Dialog (preview)",
        "rpm": 1, "rpd": 5, "monthly_30d": 150,
        "inputs": ["audio", "video", "text"], "outputs": ["audio","text"],
        "note": "خروجی همزمان صوت و متن؛ نرخ محدود برای preview."
    },
    "gemini-2.5-flash-preview-tts": {
        "display": "Gemini 2.5 Flash Preview TTS",
        "rpm": 3, "rpd": 15, "monthly_30d": 450,
        "inputs": ["text"], "outputs": ["audio"],
        "note": "TTS اختصاصی — نرخ و قیمت جدا در صفحه Pricing."
    },
    "gemini-2.5-flash-image-preview": {
        "display": "Gemini 2.5 Flash Image (preview)",
        "rpm": None, "rpd": None, "monthly_30d": None,
        "inputs": ["images","text"], "outputs": ["images","text"],
        "note": "محدودیت‌های preview و قیمت در مستندات."
    },
    "gemini-2.0-flash": {
        "display": "Gemini 2.0 Flash",
        "rpm": 15, "rpd": 200, "monthly_30d": 6000,
        "inputs": ["audio","images","video","text"], "outputs": ["text"],
        "note": "نسل قبلی Flash؛ مناسب realtime و استریم."
    },
    "gemini-2.0-flash-preview-image-generation": {
        "display": "Gemini 2.0 Flash Preview Image Gen",
        "rpm": 10, "rpd": 100, "monthly_30d": 3000,
        "inputs": ["audio","images","video","text"], "outputs": ["text","images"],
        "note": "محدودیت جدا برای تولید تصویر."
    },
    "gemini-embeddings": {
        "display": "Gemini Embeddings",
        "rpm": 100, "rpd": 1000, "monthly_30d": 30000,
        "inputs": ["text"], "outputs": ["vector embeddings"],
        "note": "مخصوص استخراج embeddings؛ نرخ و throughput بالاتر."
    },
    "gemma-3": {
        "display": "Gemma 3 & 3n",
        "rpm": 30, "rpd": 14400, "monthly_30d": 432000,
        "inputs": ["text","multimodal"], "outputs": ["text","vectors"],
        "note": "برای بارهای بزرگ و throughput بالا مناسب است."
    },
    "gemini-1.5-flash": {
        "display": "Gemini 1.5 Flash (deprecated)",
        "rpm": 15, "rpd": 50, "monthly_30d": 1500,
        "inputs": ["audio","images","video","text"], "outputs": ["text"],
        "note": "در حال Deprecated؛ هنوز در برخی فهرست‌ها دیده می‌شود."
    },
}
