FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 系统依赖（pandas/numpy 已有 wheel，无需编译工具；保留 tzdata）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . || pip install --no-cache-dir .

COPY common ./common
COPY engine ./engine
COPY api ./api
COPY sql ./sql

RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
