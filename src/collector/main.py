from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests

from .config import Settings, SourceConfig, load_settings
from .crawler import Crawler
from .downloader import HttpClient
from .drive import DriveClient
from .errors import (
    BlockedDomainError,
    BlockingPageDetected,
    CollectorError,
    InvalidDocumentError,
    LockUnavailableError,
    RobotsDeniedError,
)
from .locking import DailyBudget, SharedDriveLock
from .logging_config import configure_logging
from .rate_limit import SerialRateLimiter
from .state import QueueItem, StateStore
from .validator import validate_pdf

LOGGER = logging.getLogger(__name__)
STATE_FILE_NAME = "collector-state.sqlite3"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coletor conservador de documentos públicos")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="executar descoberta e coleta")
    release_domain = sub.add_parser("release-domain", help="liberar manualmente um domínio")
    release_domain.add_argument("domain")
    release_lock = sub.add_parser("release-lock", help="liberar manualmente bloqueio expirado")
    release_lock.add_argument("--force", action="store_true")
    return parser.parse_args()


def _require_drive(settings: Settings) -> DriveClient:
    if not settings.google_service_account_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON não configurado")
    if not settings.google_drive_folder_id:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID não configurado")
    return DriveClient(settings.google_service_account_json, settings.google_drive_folder_id)


def _folder_for(item: QueueItem) -> str:
    value = (item.document_type or "").lower()
    if "contrat" in value:
        return "Contratos"
    if "empenh" in value:
        return "Empenhos"
    return "Processos"


def _sync_state_to_drive(
    drive: DriveClient,
    state: StateStore,
    state_folder_id: str,
    *,
    backup: bool,
) -> str:
    state.checkpoint()
    return drive.upload_or_replace_named(
        state_folder_id,
        state.path,
        STATE_FILE_NAME,
        "application/vnd.sqlite3",
        backup_prefix="collector-state" if backup else None,
    )


def _check_existing_block(state: StateStore, domain: str) -> None:
    block = state.domain_block(domain)
    if not block:
        return
    status = block["blocked_status"]
    retry_after = block["retry_after"]
    if status == 429 and retry_after:
        retry_at = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00")).astimezone(UTC)
        if retry_at <= datetime.now(UTC):
            state.release_domain(domain)
            return
    raise BlockedDomainError(
        domain,
        int(status or 0),
        str(block["blocked_reason"] or "bloqueio persistente; liberação manual exigida"),
        datetime.fromisoformat(str(retry_after).replace("Z", "+00:00")) if retry_after else None,
    )


def _discover(
    settings: Settings,
    crawler: Crawler,
    state: StateStore,
) -> tuple[int, int]:
    pages_total = 0
    queued_total = 0
    for source in settings.sources:
        if not source.enabled:
            continue
        _check_existing_block(state, source.domain)
        remaining_pages = max(0, settings.max_pages_per_run - pages_total)
        remaining_queue = max(0, settings.max_queue_size - state.pending_count())
        if remaining_pages == 0 or remaining_queue == 0:
            break
        result = crawler.discover(source, remaining_pages, remaining_queue)
        pages_total += result.pages_visited
        inserted = state.enqueue_many(
            source.name,
            source.domain,
            ((d.original_url, d.normalized_url, d.document_type) for d in result.documents),
            settings.max_queue_size,
        )
        queued_total += inserted
        LOGGER.info(
            "descoberta concluída",
            extra={
                "domain": source.domain,
                "event": "discovery",
                "reason": f"pages={result.pages_visited}; discovered={len(result.documents)}; queued={inserted}",
            },
        )
    return pages_total, queued_total


def _process_queue(
    settings: Settings,
    http: HttpClient,
    budget: DailyBudget,
    state: StateStore,
    drive: DriveClient,
    state_folder_id: str,
    lock_refresh=None,
) -> tuple[int, int]:
    downloaded = 0
    uploaded = 0
    temp_dir = settings.work_dir / "downloads"
    source_map: dict[str, SourceConfig] = {s.name: s for s in settings.sources if s.enabled}

    while downloaded < settings.max_files_per_run:
        item = state.next_pending()
        if not item:
            break
        source = source_map.get(item.source_name)
        if not source:
            state.mark_failed(item.id, "fonte ausente ou desativada")
            continue
        _check_existing_block(state, item.domain)
        if lock_refresh:
            lock_refresh(item.domain)
        current_budget = budget.get(item.domain)
        if int(current_budget.get("downloads", 0)) >= settings.max_files_per_domain_per_day:
            LOGGER.info(
                "limite diário atingido",
                extra={"domain": item.domain, "daily_count": current_budget.get("downloads", 0), "reason": "daily_download_budget"},
            )
            break

        state.mark_attempt(item.id)
        try:
            result = http.download(
                item.original_url,
                item.domain,
                temp_dir,
                etag=item.etag,
                last_modified=item.last_modified,
            )
            if result is None:
                state.mark_not_modified(item.id)
                continue
            downloaded += 1
            daily_count = budget.increment(item.domain, "downloads")
            pause = http.rate_limiter.after_download()
            if pause:
                LOGGER.info(
                    "pausa periódica aplicada",
                    extra={"domain": item.domain, "delay_seconds": round(pause.seconds, 3), "daily_count": daily_count, "event": pause.kind},
                )
            validation = validate_pdf(result.path, result.content_type, settings.max_file_size_bytes)
            existing_drive_id = state.has_hash(validation.sha256)
            if existing_drive_id:
                drive_file_id = existing_drive_id
            else:
                drive_file_id = drive.upload_document(
                    _folder_for(item),
                    result.path,
                    result.file_name,
                    validation.sha256,
                    result.final_url,
                )
                uploaded += 1
            state.record_document(
                item,
                result.final_url,
                result.file_name,
                validation.sha256,
                validation.size,
                drive_file_id,
                result.etag,
                result.last_modified,
            )
            _sync_state_to_drive(drive, state, state_folder_id, backup=False)
            LOGGER.info(
                "documento concluído",
                extra={
                    "domain": item.domain,
                    "url": item.original_url,
                    "http_status": result.http_status,
                    "size": validation.size,
                    "sha256": validation.sha256,
                    "drive_file_id": drive_file_id,
                    "elapsed_seconds": round(result.elapsed_seconds, 3),
                    "daily_count": daily_count,
                },
            )
        except InvalidDocumentError as exc:
            state.mark_failed(item.id, str(exc))
            LOGGER.warning(
                "documento inválido rejeitado",
                extra={"domain": item.domain, "url": item.original_url, "reason": str(exc)},
            )
        except requests.HTTPError as exc:
            if item.attempts + 1 >= 3:
                state.mark_failed(item.id, str(exc))
            else:
                state.mark_retry(item.id, str(exc))
        except (OSError, RuntimeError) as exc:
            if item.attempts + 1 >= 3:
                state.mark_failed(item.id, str(exc))
            else:
                state.mark_retry(item.id, str(exc))
        finally:
            for partial in temp_dir.glob("*.partial") if temp_dir.exists() else ():
                partial.unlink(missing_ok=True)
    return downloaded, uploaded


def _manual_release_domain(settings: Settings, drive: DriveClient, domain: str) -> int:
    folders = drive.ensure_structure()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    db_path = settings.work_dir / STATE_FILE_NAME
    drive.download_named(folders["Estado"], STATE_FILE_NAME, db_path)
    with StateStore(db_path) as state:
        state.release_domain(domain.lower())
        _sync_state_to_drive(drive, state, folders["Estado"], backup=True)
    print(f"Domínio liberado manualmente: {domain.lower()}")
    return 0


def _manual_release_lock(settings: Settings, drive: DriveClient, force: bool) -> int:
    folders = drive.ensure_structure()
    lock = SharedDriveLock(
        drive,
        folders["Estado"],
        owner=settings.owner,
        workflow_run_id=settings.workflow_run_id,
        ttl_minutes=settings.lock_ttl_minutes,
    )
    document = drive.read_json(lock.file_id)
    expires_raw = document.data.get("expires_at")
    expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00")) if expires_raw else None
    active = bool(document.data.get("owner")) and expires and expires.astimezone(UTC) > datetime.now(UTC)
    if active and not force:
        raise LockUnavailableError("bloqueio ainda está válido; use --force somente após confirmar que nenhum coletor está executando")
    drive.update_json_if_match(
        lock.file_id,
        {"owner": None, "workflow_run_id": None, "started_at": None, "expires_at": None, "domain": None, "released_manually_at": datetime.now(UTC).isoformat()},
        document.etag,
    )
    print("Bloqueio compartilhado liberado manualmente.")
    return 0


def run(settings: Settings) -> int:
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    log_path = configure_logging(
        settings.log_dir,
        secrets=(settings.google_service_account_json or "", settings.google_drive_folder_id or ""),
    )
    drive = _require_drive(settings)
    folders = drive.ensure_structure()
    state_folder_id = folders["Estado"]
    lock = SharedDriveLock(
        drive,
        state_folder_id,
        settings.owner,
        settings.workflow_run_id,
        settings.lock_ttl_minutes,
    )
    state: StateStore | None = None
    reason: str | None = None
    status = "failed"
    pages = queued = downloaded = uploaded = 0

    try:
        lock.acquire()
        db_path = settings.work_dir / STATE_FILE_NAME
        drive.download_named(state_folder_id, STATE_FILE_NAME, db_path)
        state = StateStore(db_path)
        state.start_run(settings.workflow_run_id, settings.owner, settings.dry_run)

        enabled = [s for s in settings.sources if s.enabled]
        if not enabled:
            reason = "nenhuma fonte habilitada"
            status = "no_sources"
            LOGGER.info(reason, extra={"reason": reason})
            return 0

        if not settings.dry_run and state.get_setting("dry_run_completed") != "1":
            reason = "primeiro DRY_RUN ainda não foi concluído"
            status = "blocked_bootstrap"
            raise CollectorError(reason)

        rate_limiter = SerialRateLimiter(
            settings.min_delay_seconds,
            settings.max_delay_seconds,
            settings.pause_every_downloads,
            settings.min_pause_seconds,
            settings.max_pause_seconds,
        )
        budget = DailyBudget(drive, state_folder_id, settings.max_files_per_domain_per_day)
        http = HttpClient(
            settings.effective_user_agent,
            rate_limiter,
            budget,
            settings.request_timeout_seconds,
            settings.max_redirects,
            settings.max_file_size_bytes,
        )
        crawler = Crawler(http, settings.effective_user_agent)
        pages, queued = _discover(settings, crawler, state)

        if settings.dry_run:
            state.set_setting("dry_run_completed", "1")
            status = "dry_run_complete"
            reason = f"DRY_RUN: pages={pages}; novos_itens={queued}; fila={state.pending_count()}"
            LOGGER.info("DRY_RUN concluído", extra={"reason": reason})
        else:
            downloaded, uploaded = _process_queue(
                settings, http, budget, state, drive, state_folder_id,
                lock_refresh=lambda domain: lock.refresh(domain),
            )
            status = "complete"
            reason = "limite atingido" if downloaded >= settings.max_files_per_run else "fila processada"
        return 0
    except BlockedDomainError as exc:
        reason = str(exc)
        status = "blocked"
        if state:
            state.block_domain(
                exc.domain,
                exc.reason,
                exc.retry_after.isoformat() if exc.retry_after else None,
                exc.status_code,
            )
        LOGGER.error("execução interrompida por bloqueio", extra={"domain": exc.domain, "http_status": exc.status_code, "reason": reason})
        return 2
    except (BlockingPageDetected, RobotsDeniedError) as exc:
        reason = str(exc)
        status = "blocked_content"
        if state:
            active_domains = [s.domain for s in settings.sources if s.enabled]
            for domain in active_domains:
                state.block_domain(domain, reason, status_code=0)
        LOGGER.error("execução interrompida por página de bloqueio ou robots.txt", extra={"reason": reason})
        return 3
    except LockUnavailableError as exc:
        reason = str(exc)
        status = "lock_unavailable"
        LOGGER.warning("execução não iniciada", extra={"reason": reason})
        return 4
    except Exception as exc:
        reason = str(exc)
        status = "failed"
        LOGGER.exception("falha não tratada", extra={"reason": reason})
        return 1
    finally:
        if state:
            try:
                state.finish_run(
                    settings.workflow_run_id,
                    status,
                    reason,
                    pages_discovered=pages,
                    queued=queued,
                    downloaded=downloaded,
                    uploaded=uploaded,
                )
                _sync_state_to_drive(drive, state, state_folder_id, backup=True)
            except Exception:
                LOGGER.exception("falha ao persistir estado final")
            finally:
                state.close()
        try:
            drive.upload_log(log_path, settings.workflow_run_id)
        except Exception:
            LOGGER.exception("falha ao enviar log resumido")
        lock.release()


def main() -> int:
    args = _parse_args()
    settings = load_settings()
    command = args.command or "run"
    if command == "run":
        return run(settings)
    configure_logging(settings.log_dir, secrets=(settings.google_service_account_json or "",))
    drive = _require_drive(settings)
    if command == "release-domain":
        return _manual_release_domain(settings, drive, args.domain)
    if command == "release-lock":
        return _manual_release_lock(settings, drive, args.force)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
