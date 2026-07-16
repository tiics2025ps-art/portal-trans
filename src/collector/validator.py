from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .errors import InvalidDocumentError


@dataclass(frozen=True)
class ValidationResult:
    sha256: str
    size: int
    mime_type: str


def validate_pdf(path: Path, content_type: str | None, max_size: int) -> ValidationResult:
    size = path.stat().st_size
    if size <= 8:
        raise InvalidDocumentError("PDF vazio ou pequeno demais")
    if size > max_size:
        raise InvalidDocumentError(f"arquivo excede o limite de {max_size} bytes")

    head = path.read_bytes()[:1024]
    lowered = head.lower().lstrip()
    if not head.startswith(b"%PDF-"):
        raise InvalidDocumentError("assinatura %PDF ausente")
    if lowered.startswith(b"<!doctype html") or b"<html" in lowered:
        raise InvalidDocumentError("conteúdo HTML recebido no lugar de PDF")
    if content_type:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized not in {"application/pdf", "application/octet-stream", "binary/octet-stream"}:
            raise InvalidDocumentError(f"Content-Type incompatível: {content_type}")

    try:
        reader = PdfReader(str(path), strict=False)
        if len(reader.pages) < 1:
            raise InvalidDocumentError("PDF sem páginas")
    except InvalidDocumentError:
        raise
    except Exception as exc:
        raise InvalidDocumentError(f"PDF corrompido ou incompleto: {exc}") from exc

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return ValidationResult(digest.hexdigest(), size, "application/pdf")
