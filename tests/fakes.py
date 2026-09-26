"""Offline stand-ins for Gamma and the CLOB so the whole cycle runs in tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polybot.models import Book, Market

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def mk_market(cid: str, yes_tok: str, no_tok: str, hours: float, liq=10_000, vol=5_000, closed=False,
              prices=(0.5, 0.5), neg_risk=False, event_id="", min_size=5) -> Market:
    return Market(condition_id=cid, question=f"Q {cid}", token_ids=[yes_tok, no_tok], outcomes=["Yes", "No"],
                  outcome_prices=list(prices), end_date=NOW + timedelta(hours=hours), neg_risk=neg_risk,
                  liquidity=liq, volume24h=vol, spread=0.01, min_order_size=min_size, tick_size=0.01,
                  accepting_orders=True, closed=closed, event_id=event_id, event_title="E")


def mk_book(tok: str, asks: list[tuple[float, float]], bids: list[tuple[float, float]], fee=0) -> Book:
    return Book.from_clob({"asset_id": tok, "asks": [{"price": p, "size": s} for p, s in asks],
                           "bids": [{"price": p, "size": s} for p, s in bids]}, fee_bps=fee)


class FakeGamma:
    def __init__(self, markets: list[Market]):
        self.markets = markets

    def active_markets(self, limit=600):
        return [m for m in self.markets if not m.closed][:limit]

    def markets_by_condition(self, cids):
        return {m.condition_id: m for m in self.markets if m.condition_id in set(cids)}


class FakeClob:
    def __init__(self, books: dict[str, Book], fees: dict[str, int] | None = None):
        self._books = books
        self._fees = fees or {}
        self.calls = 0

    def books(self, token_ids, batch=40):
        self.calls += 1
        return {t: self._books[t] for t in token_ids if t in self._books}

    def fee_bps(self, token_id):
        return self._fees.get(token_id, 0)
