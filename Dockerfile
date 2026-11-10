# استفاده از پایتون 3.11 به عنوان تصویر پایه
FROM python:3.11-slim

# تنظیم پوشه کاری داخل کانتینر
WORKDIR /app

# کپی کردن فایل requirements.txt و نصب پکیج‌ها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کردن بقیه فایل‌های پروژه
COPY . .

# دستور اجرای ربات
CMD ["python", "bot.py"]
