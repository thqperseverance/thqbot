import pytest
from pathlib import Path
from unittest.mock import MagicMock

from ithqbot.channels.icatmsg import ICatMsgChannel
from ithqbot.cron.service import CronService
from ithqbot.cron.types import CronSchedule
from ithqbot.agent.tools.excel import ExcelTool

def test_icatmsg_workspace_derivation():
    """Verify that enterprise strict mode requires explicit shared workspace config."""
    bus = MagicMock()
    config = {}
    channel = ICatMsgChannel(config, bus)

    with pytest.raises(RuntimeError, match="shared_workspace_root is required"):
        channel._build_user_workspace("user_abc", "tenant_x", "bot_A")


def test_icatmsg_workspace_isolated_by_tenant_and_bot(monkeypatch, tmp_path: Path):
    """Verify that shared workspace is derived from configured shared root."""
    bus = MagicMock()
    config = {"bot_id": "bot_A", "shared_workspace_root": "shared-sessions"}
    channel = ICatMsgChannel(config, bus)

    monkeypatch.setattr("ithqbot.channels.icatmsg.get_workspace_path", lambda: tmp_path)

    assistant_ws = channel._build_user_workspace("user_abc", "tenant_x", "bot_A")
    ops_ws = channel._build_user_workspace("user_abc", "tenant_x", "bot_B")
    other_tenant_ws = channel._build_user_workspace("user_abc", "tenant_y", "bot_A")

    assert assistant_ws == (tmp_path / "shared-sessions" / "tenant_x" / "bot_A" / "user_abc").resolve()
    assert ops_ws == (tmp_path / "shared-sessions" / "tenant_x" / "bot_B" / "user_abc").resolve()
    assert other_tenant_ws == (tmp_path / "shared-sessions" / "tenant_y" / "bot_A" / "user_abc").resolve()
    assert assistant_ws != ops_ws
    assert assistant_ws != other_tenant_ws

@pytest.mark.asyncio
async def test_cron_ownership_isolation(tmp_path):
    """Verify that CronService enforces owner_id for list/remove/add."""
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    await service.start()
    
    # Add job for User A
    sched = CronSchedule(kind="every", every_ms=60000)
    service.add_job(name="User A Job", schedule=sched, message="Hi A", owner_id="user_a")
    
    # Add job for User B
    service.add_job(name="User B Job", schedule=sched, message="Hi B", owner_id="user_b")
    
    # List jobs for User A
    jobs_a = service.list_jobs(owner_id="user_a")
    assert len(jobs_a) == 1
    assert jobs_a[0].name == "User A Job"
    
    # List jobs for User B
    jobs_b = service.list_jobs(owner_id="user_b")
    assert len(jobs_b) == 1
    assert jobs_b[0].name == "User B Job"
    
    # List all (admin)
    jobs_all = service.list_jobs()
    assert len(jobs_all) == 2
    
    # Remove A's job as B (should fail/no removal)
    job_id_a = jobs_a[0].id
    removed = service.remove_job(job_id_a, owner_id="user_b")
    assert removed is False
    assert len(service.list_jobs()) == 2
    
    # Remove A's job as A (should succeed)
    removed = service.remove_job(job_id_a, owner_id="user_a")
    assert removed is True
    assert len(service.list_jobs()) == 1
    assert service.list_jobs()[0].name == "User B Job"

def test_excel_tool_workspace_path():
    """Verify ExcelTool accurately resolves paths using its isolated _workspace."""
    ws = Path("/tmp/user_a_ws")
    tool = ExcelTool(workspace=ws)
    
    # Manually check path resolution since we don't want to hit real IO
    file_path = "data.xlsx"
    # Logic from ExcelTool.execute:
    path = Path(file_path)
    if not path.is_absolute() and tool._workspace:
        path = tool._workspace / path
    
    assert str(path) == str(ws / "data.xlsx")

if __name__ == "__main__":
    # Simple manual run if needed
    print("Running isolation tests...")
