from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _jlist(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return []


@dataclass
class Market:
    condition_id: str
    question: str
    token_ids: list[str]              # aligned with outcomes
    outcomes: list[str]
    outcome_prices: list[float]
    end_date: Optional[datetime]
    neg_risk: bool
    liquidity: float
    volume24h: float
    spread: float
    min_order_size: float
    tick_size: float
    accepting_orders: bool
    closed: bool
    event_id: str = ""
    event_title: str = ""
    slug: str = ""
    description: str = ""

    @classmethod
    def from_gamma(cls, m: dict) -> "Market":
        ev = (m.get("events") or [{}])[0] if m.get("events") else {}
        return cls(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            token_ids=[str(t) for t in _jlist(m.get("clobTokenIds"))],
            outcomes=[str(o) for o in _jlist(m.get("outcomes"))],
            outcome_prices=[float(p) for p in _jlist(m.get("outcomePrices")) or []],
            end_date=parse_iso(m.get("endDate") or m.get("endDateIso")),
            neg_risk=bool(m.get("negRisk") or ev.get("negRisk")),
            liquidity=float(m.get("liquidityNum") or m.get("liquidity") or 0),
            volume24h=float(m.get("volume24hr") or 0),
            spread=float(m.get("spread") or 0),
            min_order_size=float(m.get("orderMinSize") or 5),
            tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
            accepting_orders=bool(m.get("acceptingOrders", True)) and bool(m.get("enableOrderBook", True)),
            closed=bool(m.get("closed", False)),
            event_id=str(ev.get("id", "")),
            event_title=str(ev.get("title", "")),
            slug=str(m.get("slug", "")),
            description=str(m.get("description") or "")[:2000],
        )

    @property
    def is_binary(self) -> bool:
        return len(self.token_ids) == 2 and len(self.outcomes) == 2

    def hours_to_end(self, now: datetime) -> Optional[float]:
        if not self.end_date:
            return None
        return (self.end_date - now).total_seconds() / 3600.0

    @property
    def resolved_prices(self) -> Optional[list[float]]:
        """If Gamma reports a settled 1/0 price vector, return it."""
        if not self.closed or not self.outcome_prices:
            return None
        if all(p in (0.0, 1.0) for p in self.outcome_prices) and sum(self.outcome_prices) == 1.0:
            return self.outcome_prices
        return None


@dataclass
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    bids: list[Level] = field(default_factory=list)   # sorted best (highest) first
    asks: list[Level] = field(default_factory=list)   # sorted best (lowest) first
    fee_bps: int = 0

    @classmethod
    def from_clob(cls, d: dict, fee_bps: int = 0) -> "Book":
        bids = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("bids", [])), key=lambda l: -l.price)
        asks = sorted((Level(float(x["price"]), float(x["size"])) for x in d.get("asks", [])), key=lambda l: l.price)
        return cls(token_id=str(d.get("asset_id", "")), bids=bids, asks=asks, fee_bps=fee_bps)

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def ask_depth_at_or_below(self, price: float) -> float:
        return sum(l.size for l in self.asks if l.price <= price + 1e-12)

    def cost_to_buy(self, size: float) -> Optional[tuple[float, float]]:
        """Walk the asks. Returns (total_cost, worst_price) or None if insufficient depth."""
        remaining, cost, worst = size, 0.0, 0.0
        for l in self.asks:
            take = min(remaining, l.size)
            cost += take * l.price
            worst = l.price
            remaining -= take
            if remaining <= 1e-9:
                return cost, worst
        return None


@dataclass
class Leg:
    token_id: str
    side: str              # "BUY" or "SELL"
    price: float           # limit price (worst acceptable)
    size: float            # shares
    outcome: str = ""


@dataclass
class Opportunity:
    kind: str                       # "pair_arb" | "negrisk_arb" | "harvest"
    market: Market
    legs: list[Leg]
    cost: float                     # total USD outlay
    expected_profit: float          # USD, after estimated fees
    edge: float                     # per-share edge in price units
    note: str = ""
    ai_p: Optional[float] = None    # Jev's probability for the bought side, when assessed

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.market.condition_id}"
