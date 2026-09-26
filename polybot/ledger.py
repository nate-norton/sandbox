"""Persistent state: cash, positions, trades, daily P&L. Stored as JSON and committed by CI."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Position:
    token_id: str
    condition_id: str
    question: str
    outcome: str
    size: float
    avg_price: float
    end_date: Optional[str]
    kind: str                       # strategy that opened it
    opened_at: str
    neg_risk: bool = False
    mark: float = 0.0               # last known mark price (for reporting)

    @property
    def cost(self) -> float:
        return self.size * self.avg_price


@dataclass
class Ledger:
    mode: str = "paper"
    cash: float = 25.0
    starting_bankroll: float = 25.0
    realized_pnl: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)   # token_id -> Position
    trades: list[dict] = field(default_factory=list)
    daily: dict[str, float] = field(default_factory=dict)           # YYYY-MM-DD -> realized pnl
    runs: int = 0
    last_run: str = ""
    notes: list[str] = field(default_factory=list)
    scan: dict[str, int] = field(default_factory=dict)              # last scan's filter statistics

    # ---------- persistence ----------
    @classmethod
    def load(cls, path: str, starting_bankroll: float, mode: str) -> "Ledger":
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            pos = {k: Position(**v) for k, v in d.get("positions", {}).items()}
            d["positions"] = pos
            led = cls(**d)
            if led.mode != mode:
                # switching paper -> live: keep history, drop simulated cash/positions
                led.notes.append(f"{_now()} mode changed {led.mode} -> {mode}; simulated positions cleared")
                led.mode = mode
                led.positions = {}
                led.cash = starting_bankroll
                led.starting_bankroll = starting_bankroll
                led.realized_pnl = 0.0
            return led
        return cls(mode=mode, cash=starting_bankroll, starting_bankroll=starting_bankroll)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        d = asdict(self)
        d["trades"] = d["trades"][-500:]      # keep the file bounded
        d["notes"] = d["notes"][-100:]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, path)

    # ---------- accounting ----------
    @property
    def deployed(self) -> float:
        return sum(p.cost for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + sum(p.size * (p.mark or p.avg_price) for p in self.positions.values())

    def pnl_today(self) -> float:
        return self.daily.get(_today(), 0.0)

    def record_buy(self, token_id: str, condition_id: str, question: str, outcome: str, size: float,
                   price: float, end_date: Optional[str], kind: str, neg_risk: bool, raw: Optional[dict] = None) -> None:
        cost = size * price
        self.cash -= cost
        p = self.positions.get(token_id)
        if p:
            total = p.size + size
            p.avg_price = (p.avg_price * p.size + cost) / total
            p.size = total
        else:
            self.positions[token_id] = Position(
                token_id=token_id, condition_id=condition_id, question=question, outcome=outcome,
                size=size, avg_price=price, end_date=end_date, kind=kind, opened_at=_now(), neg_risk=neg_risk, mark=price,
            )
        self.trades.append({"t": _now(), "side": "BUY", "token_id": token_id, "outcome": outcome, "size": size,
                            "price": price, "cost": round(cost, 4), "kind": kind, "q": question[:80], "raw": _short(raw)})

    def record_sell(self, token_id: str, size: float, price: float, kind: str, raw: Optional[dict] = None) -> float:
        p = self.positions.get(token_id)
        if not p:
            return 0.0
        size = min(size, p.size)
        proceeds = size * price
        pnl = (price - p.avg_price) * size
        self.cash += proceeds
        self._book_pnl(pnl)
        p.size -= size
        if p.size <= 1e-9:
            del self.positions[token_id]
        self.trades.append({"t": _now(), "side": "SELL", "token_id": token_id, "outcome": p.outcome, "size": size,
                            "price": price, "proceeds": round(proceeds, 4), "pnl": round(pnl, 4), "kind": kind,
                            "q": p.question[:80], "raw": _short(raw)})
        return pnl

    def settle(self, token_id: str, payout_per_share: float) -> float:
        """Market resolved: each share pays payout_per_share (1 or 0)."""
        p = self.positions.pop(token_id, None)
        if not p:
            return 0.0
        proceeds = p.size * payout_per_share
        pnl = proceeds - p.cost
        self.cash += proceeds
        self._book_pnl(pnl)
        self.trades.append({"t": _now(), "side": "SETTLE", "token_id": token_id, "outcome": p.outcome, "size": p.size,
                            "price": payout_per_share, "proceeds": round(proceeds, 4), "pnl": round(pnl, 4),
                            "kind": p.kind, "q": p.question[:80]})
        return pnl

    def _book_pnl(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.daily[_today()] = self.daily.get(_today(), 0.0) + pnl


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _short(raw: Optional[dict]) -> Optional[dict]:
    if not raw:
        return None
    keep = {}
    for k in ("orderID", "status", "success", "errorMsg", "takingAmount", "makingAmount"):
        if k in raw:
            keep[k] = raw[k]
    return keep or None
