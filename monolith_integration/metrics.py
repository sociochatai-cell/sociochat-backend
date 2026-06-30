"""In-process counters for integration observability (export to metrics backend separately)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class IntegrationMetrics:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    capability_upsert_ok: int = 0
    capability_upsert_fail: int = 0
    usage_polls: int = 0
    usage_events_applied: int = 0
    usage_dedupe_hits: int = 0
    usage_quarantine: int = 0
    usage_http_retries: int = 0
    usage_replays: int = 0

    def inc(self, name: str, delta: int = 1) -> None:
        if name.startswith("_"):
            return
        with self._lock:
            if hasattr(self, name) and not callable(getattr(self, name)):
                setattr(self, name, int(getattr(self, name)) + delta)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "capability_upsert_ok": self.capability_upsert_ok,
                "capability_upsert_fail": self.capability_upsert_fail,
                "usage_polls": self.usage_polls,
                "usage_events_applied": self.usage_events_applied,
                "usage_dedupe_hits": self.usage_dedupe_hits,
                "usage_quarantine": self.usage_quarantine,
                "usage_http_retries": self.usage_http_retries,
                "usage_replays": self.usage_replays,
            }


integration_metrics = IntegrationMetrics()
