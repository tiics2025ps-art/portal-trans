from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in (
            "workflow", "domain", "url", "http_status", "size", "sha256",
            "drive_file_id", "elapsed_seconds", "delay_seconds", "daily_count",
            "reason", "event",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = tuple(s for s in secrets if s and len(s) >= 6)
        self.patterns = (
            re.compile(r'("private_key"\s*:\s*")[^"]+(" )?', re.I),
            re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.I),
        )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r'("private_key"\s*:\s*")[^"]+("?)', r'\1[REDACTED]\2', message, flags=re.I)
        message = re.sub(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", message, flags=re.I)
        record.msg = message
        record.args = ()
        return True


def configure_logging(log_dir: Path, secrets: Iterable[str] = ()) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "collector.jsonl"
    formatter = JsonFormatter()
    redactor = SecretRedactionFilter(secrets)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    root.addHandler(stream)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)
    return log_path
