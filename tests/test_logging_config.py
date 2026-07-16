from __future__ import annotations

import logging
from pathlib import Path

from collector.logging_config import configure_logging


def test_secrets_are_redacted_from_logs(tmp_path: Path) -> None:
    secret = "segredo-super-confidencial"
    path = configure_logging(tmp_path, secrets=[secret])
    logging.getLogger("test").info("token=%s", secret)
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = path.read_text(encoding="utf-8")
    assert secret not in content
    assert "[REDACTED]" in content
