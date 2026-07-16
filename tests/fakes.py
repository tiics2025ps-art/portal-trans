from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from collector.drive import JsonDocument, PreconditionFailed


class FakeJsonDrive:
    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.docs: dict[str, dict[str, Any]] = {}
        self.etags: dict[str, str] = {}
        self.counter = 0

    def ensure_json_file(self, parent_id: str, name: str, initial_data: dict[str, Any], state_key: str) -> str:
        if state_key in self.ids:
            return self.ids[state_key]
        self.counter += 1
        file_id = f"file-{self.counter}"
        self.ids[state_key] = file_id
        self.docs[file_id] = copy.deepcopy(initial_data)
        self.etags[file_id] = "1"
        return file_id

    def read_json(self, file_id: str) -> JsonDocument:
        return JsonDocument(file_id, self.etags[file_id], copy.deepcopy(self.docs[file_id]))

    def update_json_if_match(self, file_id: str, data: dict[str, Any], etag: str | None) -> str:
        if etag is not None and etag != self.etags[file_id]:
            raise PreconditionFailed("conflict")
        self.docs[file_id] = copy.deepcopy(data)
        self.etags[file_id] = str(int(self.etags[file_id]) + 1)
        return self.etags[file_id]


class FakeBudget:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, int]] = {}

    def increment(self, domain: str, field: str, amount: int = 1) -> int:
        values = self.data.setdefault(domain, {"requests": 0, "downloads": 0})
        values[field] += amount
        return values[field]

    def get(self, domain: str) -> dict[str, int]:
        return dict(self.data.get(domain, {"requests": 0, "downloads": 0}))
