FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY rules ./rules
COPY scripts ./scripts

ENV PORT=8080
# Cloud Run のフロントプロキシ越しでも X-Forwarded-Proto を信頼し、元スキーム(https)を反映させる
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
