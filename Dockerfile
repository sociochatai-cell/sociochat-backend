FROM python:3.11-slim

WORKDIR /app

# Install system dependencies:
#   - gcc / libpq-dev: build psycopg2
#   - tesseract-ocr: OCR engine used by pytesseract for SCANNED PDFs
#   - poppler-utils: provides pdftoppm used by pdf2image to rasterize PDFs
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev tesseract-ocr poppler-utils && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + Chromium for JS/SPA website crawling (AI knowledge-base training)
RUN pip install --no-cache-dir playwright && playwright install --with-deps chromium

COPY . .

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "120", "app:app"]
