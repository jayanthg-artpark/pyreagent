"""Append-only audit log: who asked for what, when, and what happened.

Records go to an in-memory ring buffer (for GET /audit) and, if a path is
given, to a JSON Lines file for reproducibility and debugging.
"""

from __future__ import annotations

import collections
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .jobs import utcnow


class AuditRecord(BaseModel):
    ts: datetime = Field(default_factory=utcnow)
    event: str
    """invoke | job_finished | cancel | reserve | release | connect | disconnect"""
    caller: str
    device: str | None = None
    capability: str | None = None
    args: dict[str, Any] | None = None
    outcome: str
    """completed | accepted | rejected | failed | succeeded | cancelled | ok"""
    result: Any = None
    error: dict[str, Any] | None = None
    job_id: str | None = None
    duration_ms: float | None = None


class AuditLog:
    def __init__(self, path: str | Path | None = None, *, memory: int = 2000) -> None:
        self.path = Path(path) if path else None
        self._recent: collections.deque[AuditRecord] = collections.deque(maxlen=memory)
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **fields: Any) -> AuditRecord:
        entry = AuditRecord(**fields)
        with self._lock:
            self._recent.append(entry)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry.model_dump(mode="json"), separators=(",", ":")) + "\n")
        return entry

    def query(
        self,
        *,
        device: str | None = None,
        caller: str | None = None,
        event: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        with self._lock:
            entries = list(self._recent)
        matches = [
            e
            for e in reversed(entries)
            if (device is None or e.device == device)
            and (caller is None or e.caller == caller)
            and (event is None or e.event == event)
        ]
        return matches[:limit]
