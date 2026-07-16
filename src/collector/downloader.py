from __future__ import annotations

import email.utils
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from .errors import (
    BlockedDomainError,
    BlockingPageDetected,
    UnsafeUrlError,
)
from .locking import DailyBudget
from .rate_limit import SerialRateLimiter

LOGGER = logging.getLogger(__name__)
SERVER_RETRY_CODES = {408, 500, 502, 503, 504}
BLOCK_MARKERS = (
    "captcha",
    "access denied",
    "acesso negado",
    "temporarily blocked",
    "bloqueado temporariamente",
    "unusual traffic",
    "cf-chl-",
    "cloudflare ray id",
    "faça login",
    "sign in to continue",
)


@dataclass(frozen=True)
class HttpResult:
    response: requests.Response
    elapsed_seconds: float
    daily_request_count: int


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    original_url: str
    final_url: str
    file_name: str
    content_type: str | None
    size: int
    etag: str | None
    last_modified: str | None
    http_status: int
    elapsed_seconds: float
    daily_request_count: int


def parse_retry_after(value: str | None, now: datetime | None = None) -> datetime | None:
    if not value:
        return None
    now = now or datetime.now(UTC)
    stripped = value.strip()
    if stripped.isdigit():
        return now + timedelta(seconds=int(stripped))
    try:
        parsed = email.utils.parsedate_to_datetime(stripped)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def safe_file_name(url: str, content_disposition: str | None = None) -> str:
    candidate: str | None = None
    if content_disposition:
        match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", content_disposition, re.I)
        if match:
            candidate = unquote(match.group(1).strip().strip('"'))
    if not candidate:
        candidate = unquote(Path(urlparse(url).path).name) or "documento.pdf"
    candidate = candidate.replace("\\", "_").replace("/", "_")
    candidate = re.sub(r"[\x00-\x1f\x7f]+", "", candidate)
    candidate = re.sub(r"[^\w.()\- áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]+", "_", candidate, flags=re.UNICODE)
    candidate = candidate.strip(" ._")[:180] or "documento.pdf"
    if not candidate.lower().endswith(".pdf"):
        candidate += ".pdf"
    return candidate


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        rate_limiter: SerialRateLimiter,
        budget: DailyBudget,
        timeout_seconds: int,
        max_redirects: int,
        max_file_size_bytes: int,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[float, float], float] = random.uniform,
        session: requests.Session | None = None,
        retry_backoffs: tuple[float, ...] = (60, 180, 600),
    ) -> None:
        self.rate_limiter = rate_limiter
        self.budget = budget
        self.timeout_seconds = timeout_seconds
        self.max_file_size_bytes = max_file_size_bytes
        self.sleep_fn = sleep_fn
        self.random_fn = random_fn
        self.retry_backoffs = retry_backoffs
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "*/*"})
        self.session.max_redirects = max_redirects

    @staticmethod
    def _assert_domain(url: str, allowed_domain: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeUrlError(f"esquema de URL não permitido: {parsed.scheme}")
        if parsed.username or parsed.password:
            raise UnsafeUrlError("credenciais embutidas na URL não são permitidas")
        if parsed.netloc.lower() != allowed_domain.lower():
            raise UnsafeUrlError(f"URL fora do domínio permitido: {parsed.netloc}")

    def _request(
        self,
        url: str,
        allowed_domain: str,
        *,
        stream: bool,
        headers: dict[str, str] | None = None,
    ) -> HttpResult:
        self._assert_domain(url, allowed_domain)
        last_response: requests.Response | None = None
        for attempt in range(len(self.retry_backoffs) + 1):
            daily_count = self.budget.increment(allowed_domain, "requests")
            start = time.monotonic()
            with self.rate_limiter.request_slot() as delay_event:
                if delay_event and delay_event.seconds:
                    LOGGER.info(
                        "intervalo entre requisições aplicado",
                        extra={"domain": allowed_domain, "url": url, "delay_seconds": round(delay_event.seconds, 3), "event": delay_event.kind},
                    )
                response = self.session.get(
                    url,
                    headers=headers,
                    stream=stream,
                    timeout=(10, self.timeout_seconds),
                    allow_redirects=True,
                )
            elapsed = time.monotonic() - start
            last_response = response
            self._assert_domain(response.url, allowed_domain)
            LOGGER.info(
                "resposta HTTP",
                extra={
                    "domain": allowed_domain,
                    "url": url,
                    "http_status": response.status_code,
                    "elapsed_seconds": round(elapsed, 3),
                    "daily_count": daily_count,
                },
            )
            if response.status_code == 403:
                response.close()
                raise BlockedDomainError(allowed_domain, 403, "acesso proibido; liberação manual exigida")
            if response.status_code == 429:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                response.close()
                raise BlockedDomainError(allowed_domain, 429, "limite remoto atingido", retry_after)
            if response.status_code in SERVER_RETRY_CODES:
                if attempt >= len(self.retry_backoffs):
                    return HttpResult(response=response, elapsed_seconds=elapsed, daily_request_count=daily_count)
                wait = self.retry_backoffs[attempt] + self.random_fn(-5, 5)
                response.close()
                self.sleep_fn(max(1, wait))
                continue
            return HttpResult(response=response, elapsed_seconds=elapsed, daily_request_count=daily_count)
        assert last_response is not None
        return HttpResult(last_response, 0.0, 0)

    @staticmethod
    def _detect_blocking_page(response: requests.Response, preview: bytes) -> None:
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and not preview.lstrip().startswith((b"<!DOCTYPE", b"<html", b"<HTML")):
            return
        text = preview.decode("utf-8", errors="ignore").lower()
        if any(marker in text for marker in BLOCK_MARKERS):
            raise BlockingPageDetected("CAPTCHA, login ou página de bloqueio detectada")


    def get_robots(self, url: str, allowed_domain: str) -> str | None:
        result = self._request(url, allowed_domain, stream=False)
        response = result.response
        if response.status_code == 404:
            response.close()
            return None
        if response.status_code >= 400:
            response.raise_for_status()
        content = response.content
        if len(content) > 1024 * 1024:
            raise ValueError("robots.txt excede 1 MiB")
        response.encoding = response.encoding or "utf-8"
        return response.text

    def get_page(self, url: str, allowed_domain: str) -> tuple[str, HttpResult]:
        result = self._request(url, allowed_domain, stream=False)
        response = result.response
        if response.status_code >= 400:
            response.raise_for_status()
        content = response.content
        self._detect_blocking_page(response, content[:32768])
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("página HTML excede 5 MiB")
        response.encoding = response.encoding or "utf-8"
        return response.text, result

    def download(
        self,
        url: str,
        allowed_domain: str,
        destination_dir: Path,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> DownloadResult | None:
        headers: dict[str, str] = {"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        result = self._request(url, allowed_domain, stream=True, headers=headers)
        response = result.response
        if response.status_code == 304:
            response.close()
            return None
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise requests.HTTPError(f"HTTP {status} ao baixar {url}")

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > self.max_file_size_bytes:
            response.close()
            raise ValueError("arquivo excede o limite configurado pelo Content-Length")

        destination_dir.mkdir(parents=True, exist_ok=True)
        file_name = safe_file_name(response.url, response.headers.get("Content-Disposition"))
        final_path = destination_dir / file_name
        temporary = destination_dir / f".{file_name}.partial"
        size = 0
        preview = bytearray()
        try:
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    if len(preview) < 32768:
                        preview.extend(chunk[: 32768 - len(preview)])
                    size += len(chunk)
                    if size > self.max_file_size_bytes:
                        raise ValueError("arquivo excede o limite configurado durante o download")
                    handle.write(chunk)
                handle.flush()
            self._detect_blocking_page(response, bytes(preview))
            temporary.replace(final_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            response.close()

        return DownloadResult(
            path=final_path,
            original_url=url,
            final_url=response.url,
            file_name=file_name,
            content_type=response.headers.get("Content-Type"),
            size=size,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            http_status=response.status_code,
            elapsed_seconds=result.elapsed_seconds,
            daily_request_count=result.daily_request_count,
        )
