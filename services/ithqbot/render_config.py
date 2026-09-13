"""把 config.template.json 渲染成 ithqbot 可用的 config.json。

模板里的字符串占位符形如 ``${APP_LLM_BASE_URL}``，值取自同名环境变量。

重要：**必填变量缺失时直接报错退出**，而不是渲染出一份空配置。
这一点很关键 —— ithqbot 的 ``--config`` 只被当作 bootstrap，真正的配置由
``FileConfigStore(<config 所在目录>).load("default")`` 读取 ``<目录>/config.json``；
一旦渲染出空配置，进程会静默回落到内置默认值（连到错误的数据库），
排查成本极高。

用法::

    python render_config.py config.template.json /root/.ithqbot/config.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 缺失即视为致命：这些变量决定了模型通道与三个存储后端
REQUIRED_ENV_VARS = (
    "APP_LLM_BASE_URL",
    "APP_LLM_API_KEY",
    "APP_LLM_MODEL",
    "POSTGRES_HOST",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "KAFKA_SERVERS",
    "KAFKA_INBOUND_TOPIC",
    "KAFKA_OUTBOUND_TOPIC",
    "REDIS_HOST",
    "ITHQBOT_WORKSPACE",
    "ITHQBOT_SHARED_WORKSPACE",
)


def _substitute(value: Any) -> Any:
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_substitute(item) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item) for key, item in value.items()}
    return value


def missing_env_vars(required: tuple[str, ...] = REQUIRED_ENV_VARS) -> list[str]:
    return [name for name in required if not os.environ.get(name, "").strip()]


def render(template_path: str | Path, out_path: str | Path, *, check_env: bool = True) -> Path:
    missing = missing_env_vars() if check_env else []
    if missing:
        raise SystemExit(
            "渲染 ithqbot 配置失败，以下环境变量为空：\n  - "
            + "\n  - ".join(missing)
            + "\n\n请先设置这些变量（可从仓库根 .env 加载，或用 scripts/dev-local.ps1）。"
        )

    template = json.loads(Path(template_path).read_text(encoding="utf-8"))
    rendered = _substitute(template)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rendered, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: render_config.py <template.json> <out.json>", file=sys.stderr)
        return 2
    out = render(argv[1], argv[2])
    print(f"rendered {argv[1]} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
