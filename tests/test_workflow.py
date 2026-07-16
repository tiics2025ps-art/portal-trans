from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "collector.yml"


def test_workflow_has_manual_schedule_and_shared_concurrency() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert text.count("- cron:") == 1
    assert "group: public-document-collector" in text
    assert "cancel-in-progress: false" in text


def test_workflow_defaults_to_dry_run_and_limits_schedule() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "default: true" in text
    assert "COLLECTOR_SCHEDULE_ENABLED" in text
    assert 'MAX_FILES_PER_RUN: "40"' in text
    assert 'MAX_PAGES_PER_RUN: "25"' in text


def test_workflow_uses_no_proxy_tor_or_vpn() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    import re
    assert not re.search(r"\btor\b", text)
    assert not re.search(r"\bproxy(?:ies)?\b", text)
    assert not re.search(r"\bvpn\b", text)
