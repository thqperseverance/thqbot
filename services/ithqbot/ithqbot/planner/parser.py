from __future__ import annotations

import json
import re
from typing import Any


def parse_output(text: str) -> dict[str, Any]:
    candidate = (text or "").strip()
    if not candidate:
        raise ValueError("Invalid JSON from LLM")

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", candidate, re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except Exception:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Invalid JSON from LLM") from None
        try:
            parsed = json.loads(candidate[start:end + 1])
        except Exception as exc:
            raise ValueError("Invalid JSON from LLM") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Invalid JSON from LLM")
    return parsed
