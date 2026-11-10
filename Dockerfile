FROM python:3.10-slim

# تنظیم متغیرهای محیطی برای جلوگیری از بافر شدن لاگ‌ها و ساخت فایل‌های .pyc
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# تنظیم پوشه کاری
WORKDIR /app

# نصب پیش‌نیازهای سیستمی (برای اطمینان از کارکرد درست pandas و websocket)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# کپی و نصب پکیج‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# کپی بقیه فایل‌های پروژه
COPY . .

# اجرای ربات
CMD ["python", "bot.py"]
