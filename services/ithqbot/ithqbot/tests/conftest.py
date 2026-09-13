from __future__ import annotations

import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _configure_test_store_uris(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    nodeid = request.node.nodeid
    if (
        "ithqbot/tests/test_commands.py" in nodeid
        or "ithqbot/tests/test_config_migration.py" in nodeid
        or "ithqbot/tests/test_memory_consolidation_types.py" in nodeid
    ):
        return
    root = tmp_path_factory.mktemp("ithqbot-stores")
    sessions_dir = root / "sessions"
    memory_dir = root / "memory"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ITHQBOT_SESSION_STORE", f"file://{sessions_dir.as_posix()}")
    monkeypatch.setenv("ITHQBOT_MEMORY_STORE", f"file://{memory_dir.as_posix()}")
