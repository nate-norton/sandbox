"""Small on-disk cache for Jev assessments so a 30-minute cadence does not re-ask the same question."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from .decider import Assessment


class AiCache:
    def __init__(self, path: str, ttl_hours: float):
        self.path = path
        self.ttl = timedelta(hours=ttl_hours)
        self.data: dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except (ValueError, OSError):
                self.data = {}

    @staticmethod
    def key(condition_id: str, with_prices: bool) -> str:
        return f"{condition_id}:{'p' if with_prices else 'n'}"

    def get(self, condition_id: str, with_prices: bool, now: datetime) -> Assessment | None:
        d = self.data.get(self.key(condition_id, with_prices))
        if not d:
            return None
        at = datetime.strptime(d["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now - at > self.ttl:
            return None
        a = Assessment.from_dict(d)
        return None if a.error else a

    def put(self, a: Assessment) -> None:
        if a.error:
            return
        self.data[self.key(a.condition_id, a.with_prices)] = a.to_dict()

    def save(self, now: datetime) -> None:
        cutoff = now - self.ttl * 4
        keep = {}
        for k, d in self.data.items():
            try:
                at = datetime.strptime(d["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if at >= cutoff:
                keep[k] = d
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(keep, f, indent=0, sort_keys=True)
        os.replace(tmp, self.path)
