from __future__ import annotations

import logging
import re
import urllib.robotparser
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .config import SourceConfig
from .downloader import HttpClient
from .errors import BlockedDomainError, BlockingPageDetected, RobotsDeniedError, UnsafeUrlError

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredDocument:
    original_url: str
    normalized_url: str
    document_type: str | None


@dataclass(frozen=True)
class DiscoveryResult:
    documents: tuple[DiscoveredDocument, ...]
    pages_visited: int


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path or "/",
        query=query,
        fragment="",
    )
    return urlunparse(normalized)


def _url_allowed(url: str, source: SourceConfig) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc.lower() != source.domain:
        return False
    return any(parsed.path.startswith(prefix) for prefix in source.allowed_path_prefixes)


def _matches(url: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, url) for pattern in patterns)


def _document_type(url: str, configured: tuple[str, ...]) -> str | None:
    lowered = url.lower()
    for value in configured:
        if value.lower() in lowered:
            return value
    return configured[0] if len(configured) == 1 else None


class RobotsCache:
    def __init__(self, http: HttpClient, user_agent: str, fail_closed: bool = True) -> None:
        self.http = http
        self.user_agent = user_agent
        self.fail_closed = fail_closed
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str, domain: str) -> bool:
        if domain not in self._parsers:
            robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"
            try:
                text = self.http.get_robots(robots_url, domain)
                if text is None:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(["User-agent: *", "Allow: /"])
                else:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(text.splitlines())
                self._parsers[domain] = parser
            except (BlockedDomainError, BlockingPageDetected):
                raise
            except Exception as exc:
                LOGGER.warning(
                    "não foi possível carregar robots.txt",
                    extra={"domain": domain, "url": robots_url, "reason": str(exc)},
                )
                self._parsers[domain] = None
        parser = self._parsers[domain]
        if parser is None:
            return not self.fail_closed
        return parser.can_fetch(self.user_agent, url)


class Crawler:
    def __init__(self, http: HttpClient, user_agent: str) -> None:
        self.http = http
        self.robots = RobotsCache(http, user_agent, fail_closed=True)

    def discover(self, source: SourceConfig, max_pages: int, max_documents: int) -> DiscoveryResult:
        if not source.enabled:
            return DiscoveryResult((), 0)
        queue: deque[str] = deque(normalize_url(url) for url in source.start_urls)
        seen_pages: set[str] = set()
        seen_documents: set[str] = set()
        documents: list[DiscoveredDocument] = []

        while queue and len(seen_pages) < max_pages and len(documents) < max_documents:
            page_url = queue.popleft()
            if page_url in seen_pages or not _url_allowed(page_url, source):
                continue
            if not self.robots.allowed(page_url, source.domain):
                raise RobotsDeniedError(f"robots.txt não permite acessar {page_url}")
            try:
                html, _ = self.http.get_page(page_url, source.domain)
            except UnsafeUrlError:
                raise
            seen_pages.add(page_url)
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.find_all("a", href=True):
                absolute = normalize_url(urljoin(page_url, anchor.get("href", "")))
                if not _url_allowed(absolute, source):
                    continue
                if _matches(absolute, source.document_url_patterns):
                    if absolute not in seen_documents:
                        seen_documents.add(absolute)
                        documents.append(
                            DiscoveredDocument(
                                original_url=absolute,
                                normalized_url=absolute,
                                document_type=_document_type(absolute, source.document_types),
                            )
                        )
                        if len(documents) >= max_documents:
                            break
                    continue
                should_follow = (
                    _matches(absolute, source.follow_url_patterns)
                    if source.follow_url_patterns
                    else True
                )
                if should_follow and absolute not in seen_pages:
                    queue.append(absolute)

        return DiscoveryResult(tuple(documents), len(seen_pages))
