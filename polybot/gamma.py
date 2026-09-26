"""Read-only access to the Gamma markets API (market discovery)."""
from __future__ import annotations

import logging
from typing import Iterable

import requests

from .models import Market

log = logging.getLogger(__name__)


class Gamma:
    def __init__(self, host: str, session: requests.Session | None = None, timeout: float = 20.0):
        self.host = host.rstrip("/")
        self.s = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, **params) -> list | dict:
        r = self.s.get(f"{self.host}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def active_markets(self, limit: int = 600, page: int = 100) -> list[Market]:
        """Active, order-book-enabled markets sorted by 24h volume (most liquid first)."""
        out: list[Market] = []
        offset = 0
        while len(out) < limit:
            batch = self._get(
                "/markets",
                active="true", closed="false", limit=min(page, limit - len(out)), offset=offset,
                order="volume24hr", ascending="false",
            )
            if not batch:
                break
            for raw in batch:
                m = Market.from_gamma(raw)
                if m.condition_id and m.token_ids and m.accepting_orders:
                    out.append(m)
            offset += len(batch)
            if len(batch) < page:
                break
        log.info("gamma: %d active markets", len(out))
        return out

    def markets_by_condition(self, condition_ids: Iterable[str]) -> dict[str, Market]:
        """Fetch specific markets (used to detect resolution of held positions)."""
        found: dict[str, Market] = {}
        for cid in condition_ids:
            try:
                batch = self._get("/markets", condition_ids=cid, limit=1)
            except requests.RequestException as e:  # pragma: no cover - network
                log.warning("gamma lookup failed for %s: %s", cid, e)
                continue
            for raw in batch or []:
                m = Market.from_gamma(raw)
                if m.condition_id:
                    found[m.condition_id] = m
        return found
