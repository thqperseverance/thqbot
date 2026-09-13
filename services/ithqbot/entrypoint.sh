#!/bin/sh
# 从环境变量渲染 config.json，然后启动 ithqbot runtime。
# 这样 .env 是唯一配置来源，凭据不落盘进仓库。
set -e

CONFIG_TEMPLATE="${ITHQBOT_CONFIG_TEMPLATE:-/config.template.json}"
CONFIG_OUT="${ITHQBOT_CONFIG_OUT:-/root/.ithqbot/config.json}"

mkdir -p "$(dirname "$CONFIG_OUT")"
python /render_config.py "$CONFIG_TEMPLATE" "$CONFIG_OUT"

echo "[entrypoint] rendered config -> $CONFIG_OUT"
exec ithqbot "$@"
