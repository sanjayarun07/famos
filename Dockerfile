FROM python:3.12-slim
WORKDIR /app
# OCR for photos of notices and scanned PDFs.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY familyos ./familyos
RUN pip install --no-cache-dir .
USER nobody
EXPOSE 8000
CMD ["uvicorn", "familyos.main:app", "--host", "0.0.0.0", "--port", "8000"]
