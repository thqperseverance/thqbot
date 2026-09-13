FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
# 构建期代理（见 docker-compose.yml 的 x-build-args）；留空即直连。
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
# 前端产物（可选）：缺失时 app.main 只提供 API
COPY web ./web

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
