from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from collector.errors import InvalidDocumentError
from collector.validator import validate_pdf


def make_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def test_valid_pdf_is_hashed(tmp_path: Path) -> None:
    path = tmp_path / "ok.pdf"
    make_pdf(path)
    result = validate_pdf(path, "application/pdf", 10_000_000)
    assert len(result.sha256) == 64
    assert result.size == path.stat().st_size


def test_invalid_html_disguised_as_pdf_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.pdf"
    path.write_text("<!doctype html><html>bloqueado</html>", encoding="utf-8")
    with pytest.raises(InvalidDocumentError):
        validate_pdf(path, "application/pdf", 10_000_000)
