FROM python:3.11-slim

# 避免 Python 写 .pyc、缓冲日志，便于容器内观测
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DICOM_DATA_DIR=/data

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷应用代码
COPY app/ ./app/
COPY samples/ ./samples/

# 数据卷：SQLite、对象存储、暂存、批次 zip 全部位于 /data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 多 worker 并发；SQLite WAL + IMMEDIATE 事务保证跨 worker 一致性。
# --timeout 调大以容纳大批量同步处理。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "1800", \
     "--access-logfile", "-", "--error-logfile", "-", "app.wsgi:app"]
