FROM python:3.11

WORKDIR /app
COPY . .

# ---- Install system deps for OCR & PDF ----
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8080"]
