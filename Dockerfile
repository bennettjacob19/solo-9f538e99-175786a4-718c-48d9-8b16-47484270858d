FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GRAY_DB_PATH=/data/gray.db \
    GRAY_STABLE_PORT=18081 \
    GRAY_CANDIDATE_PORT=18082

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单服务：Web、网关与两个本地应答服务都在同一容器/进程内
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "wsgi:app"]
