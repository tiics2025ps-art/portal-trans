from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class QueueItem:
    id: int
    source_name: str
    domain: str
    original_url: str
    normalized_url: str
    document_type: str | None
    status: str
    attempts: int
    etag: str | None
    last_modified: str | None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._initialize()

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                domain TEXT NOT NULL,
                original_url TEXT NOT NULL,
                normalized_url TEXT NOT NULL UNIQUE,
                document_type TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                etag TEXT,
                last_modified TEXT,
                next_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER,
                original_url TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                final_url TEXT,
                identifier TEXT,
                domain TEXT NOT NULL,
                file_name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                etag TEXT,
                last_modified TEXT,
                drive_file_id TEXT NOT NULL,
                downloaded_at TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                UNIQUE(normalized_url, sha256),
                UNIQUE(sha256),
                FOREIGN KEY(queue_id) REFERENCES queue(id)
            );

            CREATE TABLE IF NOT EXISTS domains (
                domain TEXT PRIMARY KEY,
                blocked INTEGER NOT NULL DEFAULT 0,
                blocked_reason TEXT,
                blocked_status INTEGER,
                blocked_at TEXT,
                retry_after TEXT,
                manually_released_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                dry_run INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                pages_discovered INTEGER NOT NULL DEFAULT 0,
                queued INTEGER NOT NULL DEFAULT 0,
                downloaded INTEGER NOT NULL DEFAULT 0,
                uploaded INTEGER NOT NULL DEFAULT 0,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(domains)")}
        if "blocked_status" not in columns:
            self.connection.execute("ALTER TABLE domains ADD COLUMN blocked_status INTEGER")
        self.connection.commit()

    def checkpoint(self) -> None:
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(FULL)")
        self.connection.commit()

    def set_setting(self, key: str, value: str) -> None:
        now = utcnow_iso()
        self.connection.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now),
        )
        self.connection.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def start_run(self, run_id: str, owner: str, dry_run: bool) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO runs(run_id, owner, dry_run, started_at, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (run_id, owner, int(dry_run), utcnow_iso()),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, reason: str | None = None, **counts: int) -> None:
        fields = ["finished_at=?", "status=?", "reason=?"]
        params: list[object] = [utcnow_iso(), status, reason]
        for key in ("pages_discovered", "queued", "downloaded", "uploaded"):
            if key in counts:
                fields.append(f"{key}=?")
                params.append(int(counts[key]))
        params.append(run_id)
        self.connection.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id=?", params)
        self.connection.commit()

    def enqueue_many(
        self,
        source_name: str,
        domain: str,
        urls: Iterable[tuple[str, str, str | None]],
        max_queue_size: int,
    ) -> int:
        current = self.pending_count()
        capacity = max(0, max_queue_size - current)
        inserted = 0
        now = utcnow_iso()
        for original_url, normalized_url, document_type in urls:
            if inserted >= capacity:
                break
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO queue(
                    source_name, domain, original_url, normalized_url, document_type,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (source_name, domain, original_url, normalized_url, document_type, now, now),
            )
            if cursor.rowcount:
                inserted += 1
        self.connection.commit()
        return inserted

    def pending_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM queue WHERE status IN ('pending', 'retry')"
        ).fetchone()
        return int(row[0])

    def next_pending(self, domain: str | None = None) -> QueueItem | None:
        sql = """
            SELECT * FROM queue
            WHERE status IN ('pending', 'retry')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        """
        params: list[object] = [utcnow_iso()]
        if domain:
            sql += " AND domain=?"
            params.append(domain)
        sql += " ORDER BY created_at, id LIMIT 1"
        row = self.connection.execute(sql, params).fetchone()
        if not row:
            return None
        return QueueItem(
            id=row["id"],
            source_name=row["source_name"],
            domain=row["domain"],
            original_url=row["original_url"],
            normalized_url=row["normalized_url"],
            document_type=row["document_type"],
            status=row["status"],
            attempts=row["attempts"],
            etag=row["etag"],
            last_modified=row["last_modified"],
        )

    def mark_attempt(self, item_id: int) -> None:
        self.connection.execute(
            "UPDATE queue SET attempts=attempts+1, updated_at=? WHERE id=?",
            (utcnow_iso(), item_id),
        )
        self.connection.commit()

    def mark_retry(self, item_id: int, error: str, next_attempt_at: str | None = None) -> None:
        self.connection.execute(
            """
            UPDATE queue SET status='retry', last_error=?, next_attempt_at=?, updated_at=?
            WHERE id=?
            """,
            (error[:2000], next_attempt_at, utcnow_iso(), item_id),
        )
        self.connection.commit()

    def mark_failed(self, item_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE queue SET status='failed', last_error=?, updated_at=? WHERE id=?",
            (error[:2000], utcnow_iso(), item_id),
        )
        self.connection.commit()

    def mark_not_modified(self, item_id: int) -> None:
        self.connection.execute(
            "UPDATE queue SET status='not_modified', updated_at=? WHERE id=?",
            (utcnow_iso(), item_id),
        )
        self.connection.commit()

    def record_document(
        self,
        item: QueueItem,
        final_url: str,
        file_name: str,
        sha256: str,
        size: int,
        drive_file_id: str,
        etag: str | None,
        last_modified: str | None,
        identifier: str | None = None,
    ) -> None:
        now = utcnow_iso()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO documents(
                    queue_id, original_url, normalized_url, final_url, identifier, domain,
                    file_name, sha256, size, etag, last_modified, drive_file_id,
                    downloaded_at, status, attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?)
                """,
                (
                    item.id, item.original_url, item.normalized_url, final_url, identifier,
                    item.domain, file_name, sha256, size, etag, last_modified,
                    drive_file_id, now, item.attempts + 1,
                ),
            )
            self.connection.execute(
                """
                UPDATE queue SET status='complete', etag=?, last_modified=?, last_error=NULL,
                    updated_at=? WHERE id=?
                """,
                (etag, last_modified, now, item.id),
            )

    def has_document_url(self, normalized_url: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM documents WHERE normalized_url=? AND status='complete' LIMIT 1",
            (normalized_url,),
        ).fetchone()
        return bool(row)

    def has_hash(self, sha256: str) -> str | None:
        row = self.connection.execute(
            "SELECT drive_file_id FROM documents WHERE sha256=? AND status='complete' LIMIT 1",
            (sha256,),
        ).fetchone()
        return str(row[0]) if row else None

    def block_domain(
        self,
        domain: str,
        reason: str,
        retry_after: str | None = None,
        status_code: int | None = None,
    ) -> None:
        now = utcnow_iso()
        self.connection.execute(
            """
            INSERT INTO domains(domain, blocked, blocked_reason, blocked_status, blocked_at, retry_after, updated_at)
            VALUES (?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET blocked=1, blocked_reason=excluded.blocked_reason,
                blocked_status=excluded.blocked_status, blocked_at=excluded.blocked_at, retry_after=excluded.retry_after,
                updated_at=excluded.updated_at
            """,
            (domain, reason, status_code, now, retry_after, now),
        )
        self.connection.commit()

    def release_domain(self, domain: str) -> None:
        now = utcnow_iso()
        self.connection.execute(
            """
            UPDATE domains SET blocked=0, blocked_reason=NULL, blocked_status=NULL, retry_after=NULL,
                manually_released_at=?, updated_at=? WHERE domain=?
            """,
            (now, now, domain),
        )
        self.connection.commit()

    def domain_block(self, domain: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM domains WHERE domain=? AND blocked=1", (domain,)
        ).fetchone()

    def export_recent_run(self, run_id: str) -> dict[str, object]:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else {}

    @contextlib.contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
