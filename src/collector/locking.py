from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .drive import DriveClient, PreconditionFailed
from .errors import DailyBudgetExceeded, LockUnavailableError

LOGGER = logging.getLogger(__name__)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class LockLease:
    file_id: str
    owner: str
    workflow_run_id: str
    domain: str | None
    expires_at: datetime


class SharedDriveLock:
    def __init__(
        self,
        drive: DriveClient,
        state_folder_id: str,
        owner: str,
        workflow_run_id: str,
        ttl_minutes: int = 180,
    ) -> None:
        self.drive = drive
        self.state_folder_id = state_folder_id
        self.owner = owner
        self.workflow_run_id = workflow_run_id
        self.ttl_minutes = ttl_minutes
        self.file_id = drive.ensure_json_file(
            state_folder_id,
            "collector-lock.json",
            {"owner": None, "workflow_run_id": None, "started_at": None, "expires_at": None, "domain": None},
            "collector-lock",
        )
        self.lease: LockLease | None = None

    def acquire(self, domain: str | None = None, retries: int = 5) -> LockLease:
        for attempt in range(retries):
            document = self.drive.read_json(self.file_id)
            now = datetime.now(UTC)
            current = document.data
            expires_at = _parse_time(current.get("expires_at"))
            held = bool(current.get("owner")) and expires_at and expires_at > now
            same_holder = (
                current.get("owner") == self.owner
                and current.get("workflow_run_id") == self.workflow_run_id
            )
            if held and not same_holder:
                raise LockUnavailableError(
                    f"bloqueio válido por {current.get('owner')} até {expires_at.isoformat()}"
                )
            new_expiry = now + timedelta(minutes=self.ttl_minutes)
            payload: dict[str, Any] = {
                "owner": self.owner,
                "workflow_run_id": self.workflow_run_id,
                "started_at": now.replace(microsecond=0).isoformat(),
                "expires_at": new_expiry.replace(microsecond=0).isoformat(),
                "domain": domain,
            }
            try:
                self.drive.update_json_if_match(self.file_id, payload, document.etag)
            except PreconditionFailed:
                time.sleep(min(2**attempt, 8))
                continue
            verify = self.drive.read_json(self.file_id).data
            if (
                verify.get("owner") == self.owner
                and verify.get("workflow_run_id") == self.workflow_run_id
            ):
                self.lease = LockLease(
                    self.file_id, self.owner, self.workflow_run_id, domain, new_expiry
                )
                return self.lease
        raise LockUnavailableError("não foi possível adquirir o bloqueio após conflitos de versão")

    def refresh(self, domain: str | None = None) -> None:
        if not self.lease:
            raise LockUnavailableError("bloqueio ainda não adquirido")
        self.acquire(domain=domain)

    def release(self) -> None:
        if not self.lease:
            return
        for attempt in range(5):
            document = self.drive.read_json(self.file_id)
            current = document.data
            if not (
                current.get("owner") == self.owner
                and current.get("workflow_run_id") == self.workflow_run_id
            ):
                self.lease = None
                return
            payload = {
                "owner": None,
                "workflow_run_id": None,
                "started_at": None,
                "expires_at": None,
                "domain": None,
                "released_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
            try:
                self.drive.update_json_if_match(self.file_id, payload, document.etag)
                self.lease = None
                return
            except PreconditionFailed:
                time.sleep(min(2**attempt, 8))
        LOGGER.error("falha ao liberar bloqueio compartilhado", extra={"reason": "etag_conflict"})

    def __enter__(self) -> "SharedDriveLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class DailyBudget:
    def __init__(self, drive: DriveClient, state_folder_id: str, max_downloads_per_day: int) -> None:
        self.drive = drive
        self.max_downloads_per_day = max_downloads_per_day
        self.file_id = drive.ensure_json_file(
            state_folder_id,
            "daily-budget.json",
            {"version": 1, "days": {}},
            "daily-budget",
        )

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def get(self, domain: str) -> dict[str, int]:
        document = self.drive.read_json(self.file_id)
        return dict(document.data.get("days", {}).get(self._today(), {}).get(domain, {}))

    def increment(self, domain: str, field: str, amount: int = 1, retries: int = 8) -> int:
        if field not in {"requests", "downloads"}:
            raise ValueError("campo de orçamento inválido")
        for attempt in range(retries):
            document = self.drive.read_json(self.file_id)
            payload = dict(document.data or {})
            days = dict(payload.get("days", {}))
            # Retém somente 45 dias para o JSON não crescer eternamente, raro impulso humano.
            for old_day in sorted(days)[:-45]:
                days.pop(old_day, None)
            day = self._today()
            day_data = dict(days.get(day, {}))
            domain_data = dict(day_data.get(domain, {}))
            current = int(domain_data.get(field, 0))
            if field == "downloads" and current + amount > self.max_downloads_per_day:
                raise DailyBudgetExceeded(
                    f"limite diário de downloads atingido para {domain}: {current}/{self.max_downloads_per_day}"
                )
            domain_data[field] = current + amount
            day_data[domain] = domain_data
            days[day] = day_data
            payload["version"] = 1
            payload["days"] = days
            payload["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
            try:
                self.drive.update_json_if_match(self.file_id, payload, document.etag)
                return domain_data[field]
            except PreconditionFailed:
                time.sleep(min(0.25 * (2**attempt), 4))
        raise LockUnavailableError("conflitos sucessivos ao atualizar orçamento diário")
