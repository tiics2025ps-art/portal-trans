from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class SourceConfig:
    name: str
    base_url: str
    enabled: bool = False
    start_urls: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    allowed_path_prefixes: tuple[str, ...] = ("/",)
    document_url_patterns: tuple[str, ...] = (r"(?i)\.pdf(?:$|[?#])",)
    follow_url_patterns: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return urlparse(self.base_url).netloc.lower()


@dataclass(frozen=True)
class Settings:
    min_delay_seconds: float = 25
    max_delay_seconds: float = 45
    pause_every_downloads: int = 10
    min_pause_seconds: float = 300
    max_pause_seconds: float = 600
    max_files_per_run: int = 40
    max_files_per_domain_per_day: int = 200
    max_pages_per_run: int = 25
    max_queue_size: int = 500
    request_timeout_seconds: int = 45
    max_file_size_bytes: int = 100 * 1024 * 1024
    max_redirects: int = 5
    lock_ttl_minutes: int = 180
    user_agent: str = "ColetorDocumentosPublicos/1.0"
    contact_email: str | None = None
    dry_run: bool = True
    owner: str = "github-actions"
    workflow_run_id: str = "local"
    config_path: Path = Path("config/sources.yml")
    work_dir: Path = Path(".collector-work")
    log_dir: Path = Path("logs")
    google_drive_folder_id: str | None = None
    google_service_account_json: str | None = None
    scheduled_run: bool = False
    sources: tuple[SourceConfig, ...] = field(default_factory=tuple)

    @property
    def effective_user_agent(self) -> str:
        if self.contact_email:
            return f"{self.user_agent} (+mailto:{self.contact_email})"
        return self.user_agent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _load_sources(path: Path) -> tuple[SourceConfig, ...]:
    if not path.exists():
        return ()
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: list[SourceConfig] = []
    for item in data.get("sources", []):
        base_url = str(item["base_url"]).rstrip("/")
        start_urls = tuple(item.get("start_urls") or (base_url,))
        result.append(
            SourceConfig(
                name=str(item["name"]),
                base_url=base_url,
                enabled=bool(item.get("enabled", False)),
                start_urls=start_urls,
                document_types=tuple(item.get("document_types", [])),
                allowed_path_prefixes=tuple(item.get("allowed_path_prefixes", ["/"])),
                document_url_patterns=tuple(
                    item.get("document_url_patterns", [r"(?i)\.pdf(?:$|[?#])"])
                ),
                follow_url_patterns=tuple(item.get("follow_url_patterns", [])),
            )
        )
    return tuple(result)


def load_settings() -> Settings:
    config_path = Path(os.getenv("SOURCES_CONFIG", "config/sources.yml"))
    settings = Settings(
        min_delay_seconds=_env_float("MIN_DELAY_SECONDS", 25),
        max_delay_seconds=_env_float("MAX_DELAY_SECONDS", 45),
        pause_every_downloads=_env_int("PAUSE_EVERY_DOWNLOADS", 10),
        min_pause_seconds=_env_float("MIN_PAUSE_SECONDS", 300),
        max_pause_seconds=_env_float("MAX_PAUSE_SECONDS", 600),
        max_files_per_run=_env_int("MAX_FILES_PER_RUN", 40),
        max_files_per_domain_per_day=_env_int("MAX_FILES_PER_DOMAIN_PER_DAY", 200),
        max_pages_per_run=_env_int("MAX_PAGES_PER_RUN", 25),
        max_queue_size=_env_int("MAX_QUEUE_SIZE", 500),
        request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 45),
        max_file_size_bytes=_env_int("MAX_FILE_SIZE_BYTES", 100 * 1024 * 1024),
        max_redirects=_env_int("MAX_REDIRECTS", 5),
        lock_ttl_minutes=_env_int("LOCK_TTL_MINUTES", 180),
        contact_email=os.getenv("COLLECTOR_CONTACT_EMAIL"),
        dry_run=_env_bool("DRY_RUN", True),
        owner=os.getenv("COLLECTOR_OWNER", "github-actions"),
        workflow_run_id=os.getenv("GITHUB_RUN_ID", os.getenv("WORKFLOW_RUN_ID", "local")),
        config_path=config_path,
        work_dir=Path(os.getenv("WORK_DIR", ".collector-work")),
        log_dir=Path(os.getenv("LOG_DIR", "logs")),
        google_drive_folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID"),
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),
        scheduled_run=os.getenv("GITHUB_EVENT_NAME") == "schedule",
        sources=_load_sources(config_path),
    )
    if settings.min_delay_seconds > settings.max_delay_seconds:
        raise ValueError("MIN_DELAY_SECONDS não pode ser maior que MAX_DELAY_SECONDS")
    if settings.min_pause_seconds > settings.max_pause_seconds:
        raise ValueError("MIN_PAUSE_SECONDS não pode ser maior que MAX_PAUSE_SECONDS")
    return settings
