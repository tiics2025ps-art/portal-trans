from __future__ import annotations

from pathlib import Path

from collector.state import StateStore


def test_state_survives_restart_and_detects_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with StateStore(path) as state:
        inserted = state.enqueue_many(
            "fonte", "example.gov", [("https://example.gov/a.pdf", "https://example.gov/a.pdf", "contratos")], 500
        )
        assert inserted == 1
        assert state.pending_count() == 1
    with StateStore(path) as state:
        assert state.pending_count() == 1
        inserted = state.enqueue_many(
            "fonte", "example.gov", [("https://example.gov/a.pdf", "https://example.gov/a.pdf", "contratos")], 500
        )
        assert inserted == 0


def test_queue_limit_is_respected(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as state:
        urls = [(f"https://example.gov/{i}.pdf", f"https://example.gov/{i}.pdf", None) for i in range(20)]
        assert state.enqueue_many("x", "example.gov", urls, 5) == 5
        assert state.pending_count() == 5
