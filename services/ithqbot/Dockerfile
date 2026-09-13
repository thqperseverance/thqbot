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

RUN python -m pip install --no-cache-dir --retries 10 uv

WORKDIR /workspace

# 先装依赖（利用 layer 缓存）
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p ithqbot && touch ithqbot/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf ithqbot

# 再装源码
COPY ithqbot/ ./ithqbot/
RUN uv pip install --system --no-cache .

# 运行时脚本（渲染配置 + 启动）
COPY entrypoint.sh /entrypoint.sh
COPY render_config.py /render_config.py
COPY config.template.json /config.template.json
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
