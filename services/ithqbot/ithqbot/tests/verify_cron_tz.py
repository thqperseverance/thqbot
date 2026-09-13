import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from ithqbot.agent.tools.cron import CronTool
from ithqbot.cron.service import CronService
from ithqbot import context

class MockCronService:
    def __init__(self):
        self.jobs = []
    def add_job(self, **kwargs):
        from types import SimpleNamespace
        job = SimpleNamespace(id="test-id", name=kwargs.get("name"))
        self.jobs.append(kwargs)
        return job
    def list_jobs(self, **kwargs):
        return []

async def test_cron_timezone():
    service = MockCronService()
    tool = CronTool(service)
    
    # Mock context
    context.account_id.set("user_1")
    context.tenant_id.set("tenant_1")
    tool.set_context("test_channel", "chat_1")
    
    # Case 1: Naive ISO string (should default to Asia/Shanghai)
    # 2026-04-12 10:00:00 Shanghai is 02:00:00 UTC
    at_str = "2026-04-12T10:00:00"
    await tool.execute(action="add", message="Test 1", at=at_str)
    
    job1 = service.jobs[0]
    ts1 = job1['schedule'].at_ms / 1000
    dt1 = datetime.fromtimestamp(ts1, tz=ZoneInfo("UTC"))
    print(f"Test 1 (Naive): {at_str} -> {dt1.isoformat()} (UTC)")
    
    expected_utc = datetime(2026, 4, 12, 2, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert dt1 == expected_utc, f"Expected {expected_utc}, got {dt1}"
    print("Test 1 Passed!")

    # Case 2: Naive ISO string with explicit TZ parameter
    at_str = "2026-04-12T10:00:00"
    await tool.execute(action="add", message="Test 2", at=at_str, tz="America/New_York")
    
    job2 = service.jobs[1]
    ts2 = job2['schedule'].at_ms / 1000
    dt2 = datetime.fromtimestamp(ts2, tz=ZoneInfo("UTC"))
    print(f"Test 2 (With TZ): {at_str} (NY) -> {dt2.isoformat()} (UTC)")
    
    # 10:00 NY is 14:00 UTC (assuming daylight saving or not, just check logic)
    # NY is UTC-4 or UTC-5. 
    expected_utc_ny = datetime(2026, 4, 12, 10, 0, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(ZoneInfo("UTC"))
    assert dt2 == expected_utc_ny, f"Expected {expected_utc_ny}, got {dt2}"
    print("Test 2 Passed!")

if __name__ == "__main__":
    asyncio.run(test_cron_timezone())
