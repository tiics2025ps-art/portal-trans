from __future__ import annotations

from pathlib import Path

import pytest
import responses

from collector.downloader import HttpClient
from collector.errors import BlockedDomainError
from collector.rate_limit import SerialRateLimiter
from tests.fakes import FakeBudget


def client() -> HttpClient:
    return HttpClient(
        "ColetorDocumentosPublicos/1.0",
        SerialRateLimiter(0, 0),
        FakeBudget(),
        timeout_seconds=2,
        max_redirects=2,
        max_file_size_bytes=1024 * 1024,
        sleep_fn=lambda _: None,
        random_fn=lambda a, b: 0,
        retry_backoffs=(),
    )


@responses.activate
def test_http_403_stops_immediately() -> None:
    responses.get("https://example.gov/a.pdf", status=403)
    with pytest.raises(BlockedDomainError) as exc:
        client().download("https://example.gov/a.pdf", "example.gov", Path("/tmp"))
    assert exc.value.status_code == 403
    assert len(responses.calls) == 1


@responses.activate
def test_http_429_reads_retry_after_and_stops() -> None:
    responses.get("https://example.gov/a.pdf", status=429, headers={"Retry-After": "3600"})
    with pytest.raises(BlockedDomainError) as exc:
        client().download("https://example.gov/a.pdf", "example.gov", Path("/tmp"))
    assert exc.value.status_code == 429
    assert exc.value.retry_after is not None
    assert len(responses.calls) == 1


@responses.activate
def test_conditional_headers_are_sent(tmp_path: Path) -> None:
    responses.get("https://example.gov/a.pdf", status=304)
    result = client().download(
        "https://example.gov/a.pdf",
        "example.gov",
        tmp_path,
        etag='"abc"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
    )
    assert result is None
    request = responses.calls[0].request
    assert request.headers["If-None-Match"] == '"abc"'
    assert "If-Modified-Since" in request.headers
