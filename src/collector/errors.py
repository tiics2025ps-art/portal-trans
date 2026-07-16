from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class CollectorError(RuntimeError):
    """Erro base do coletor."""


@dataclass
class BlockedDomainError(CollectorError):
    domain: str
    status_code: int
    reason: str
    retry_after: datetime | None = None

    def __str__(self) -> str:
        suffix = f"; nova tentativa após {self.retry_after.isoformat()}" if self.retry_after else ""
        return f"domínio {self.domain} bloqueado por HTTP {self.status_code}: {self.reason}{suffix}"


class InvalidDocumentError(CollectorError):
    pass


class LockUnavailableError(CollectorError):
    pass


class DailyBudgetExceeded(CollectorError):
    pass


class UnsafeUrlError(CollectorError):
    pass


class RobotsDeniedError(CollectorError):
    pass


class BlockingPageDetected(CollectorError):
    pass
