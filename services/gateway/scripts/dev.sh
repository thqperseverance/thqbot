#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${SERVICE_DIR}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_EXEC="${PYTHON_BIN}"
elif [[ -x /usr/bin/python3 ]]; then
  PYTHON_EXEC="/usr/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_EXEC="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_EXEC="$(command -v python)"
else
  echo "python interpreter not found; set PYTHON_BIN explicitly" >&2
  exit 1
fi

HOST="${ICATMSG_GATEWAY_HOST:-0.0.0.0}"
PORT="${ICATMSG_GATEWAY_PORT:-9001}"
RELOAD="${ICATMSG_GATEWAY_RELOAD:-true}"

ARGS=(-m uvicorn app.main:app --host "${HOST}" --port "${PORT}")
if [[ "${RELOAD}" != "false" ]]; then
  ARGS+=(--reload)
fi

exec "${PYTHON_EXEC}" "${ARGS[@]}"
