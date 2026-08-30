FROM python:3.12-slim

# ffmpeg / ffprobe：音訊格式轉換與時長偵測，MeetGenius 必要相依
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# 音檔與產出目錄（docker-compose 會掛載 volume 到這幾個路徑）
RUN mkdir -p app/additional/meetgenius/uploads \
             app/additional/meetgenius/processed \
             app/additional/meetgenius/output

EXPOSE 8080

# workers 必須為 1：進度追蹤 (app.progress_latest) 與轉錄背景執行緒都存在單一 process 記憶體中，
# 多 worker 會導致輪詢不到進度。並行以 threads 處理。
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--worker-class", "gthread", \
     "--timeout", "600", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "run:app"]
