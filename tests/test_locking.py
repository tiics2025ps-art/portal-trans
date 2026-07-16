from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from collector.errors import DailyBudgetExceeded, LockUnavailableError
from collector.locking import DailyBudget, SharedDriveLock
from tests.fakes import FakeJsonDrive


def test_local_lock_prevents_github_actions() -> None:
    drive = FakeJsonDrive()
    local = SharedDriveLock(drive, "state", "local", "local-1", ttl_minutes=30)
    local.acquire("example.gov")
    github = SharedDriveLock(drive, "state", "github-actions", "gh-1", ttl_minutes=30)
    with pytest.raises(LockUnavailableError):
        github.acquire("example.gov")


def test_two_workflows_cannot_acquire_same_lock() -> None:
    drive = FakeJsonDrive()
    first = SharedDriveLock(drive, "state", "github-actions", "1", ttl_minutes=30)
    second = SharedDriveLock(drive, "state", "github-actions", "2", ttl_minutes=30)
    first.acquire()
    with pytest.raises(LockUnavailableError):
        second.acquire()


def test_expired_lock_can_be_replaced() -> None:
    drive = FakeJsonDrive()
    lock = SharedDriveLock(drive, "state", "github-actions", "new", ttl_minutes=30)
    drive.docs[lock.file_id] = {
        "owner": "local",
        "workflow_run_id": "old",
        "started_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "domain": "example.gov",
    }
    lease = lock.acquire()
    assert lease.workflow_run_id == "new"


def test_daily_budget_is_shared_and_enforced() -> None:
    drive = FakeJsonDrive()
    first = DailyBudget(drive, "state", max_downloads_per_day=2)
    second = DailyBudget(drive, "state", max_downloads_per_day=2)
    assert first.increment("example.gov", "downloads") == 1
    assert second.increment("example.gov", "downloads") == 2
    with pytest.raises(DailyBudgetExceeded):
        first.increment("example.gov", "downloads")
