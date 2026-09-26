"""Turns approved opportunities into fills: simulated in paper mode, FAK orders in live mode."""
from __future__ import annotations

import logging
from typing import Optional

from .clob import LiveClob
from .ledger import Ledger
from .models import Book, Leg, Opportunity

log = logging.getLogger(__name__)


class Executor:
    def __init__(self, ledger: Ledger, books: dict[str, Book], live: Optional[LiveClob] = None):
        self.led = ledger
        self.books = books
        self.live = live
        self.last_error = ""

    # ------------------------------------------------------------------ buys
    def buy(self, opp: Opportunity) -> float:
        """Execute every leg. Returns USD actually spent. Unwinds a half-filled arbitrage."""
        m = opp.market
        spent = 0.0
        self.last_error = ""
        filled_legs: list[tuple[Leg, float, float]] = []      # (leg, size, avg price)
        for leg in opp.legs:
            size, price, raw = self._fill_buy(leg, m.tick_size, m.neg_risk)
            if size <= 0:
                log.info("  leg not filled: %s %s @ %.3f", leg.side, leg.outcome, leg.price)
                self.last_error = str((raw or {}).get("errorMsg") or (raw or {}).get("status") or "")
                break
            self.led.record_buy(leg.token_id, m.condition_id, m.question, leg.outcome, size, price,
                                m.end_date.isoformat() if m.end_date else None, opp.kind, m.neg_risk, raw,
                                model_p=opp.ai_p, model_src=opp.model_src)
            spent += size * price
            filled_legs.append((leg, size, price))
            log.info("  filled BUY %.1f %s @ %.3f (%s)", size, leg.outcome, price, opp.kind)

        if opp.kind.endswith("_arb") and len(filled_legs) != len(opp.legs):
            # A hedge without its other half is a directional bet we did not choose: unwind it.
            for leg, size, _ in filled_legs:
                b = self.books.get(leg.token_id)
                bid = b.best_bid if b else None
                if bid:
                    got = self.sell(leg.token_id, size, bid, opp.kind + "_unwind")
                    spent -= got
                    log.warning("  unwound %.1f %s @ %.3f after partial arb fill", size, leg.outcome, bid)
                else:
                    log.warning("  could not unwind %s: no bid", leg.outcome)
        return max(spent, 0.0)

    def _fill_buy(self, leg: Leg, tick: float, neg_risk: bool) -> tuple[float, float, Optional[dict]]:
        if self.live is None:
            b = self.books.get(leg.token_id)
            if not b:
                return 0.0, 0.0, None
            r = b.cost_to_buy(leg.size)
            if not r:
                return 0.0, 0.0, None
            cost, worst = r
            if worst > leg.price + 1e-9:
                return 0.0, 0.0, None
            return leg.size, cost / leg.size, None
        try:
            filled, raw = self.live.fill_or_kill(leg, tick, neg_risk)
        except Exception as e:  # network / signing / rejection
            log.error("  order failed for %s: %s", leg.outcome, e)
            return 0.0, 0.0, {"errorMsg": str(e)[:200]}
        return filled, leg.price, raw

    # ------------------------------------------------------------------ sells
    def sell(self, token_id: str, size: float, price: float, kind: str) -> float:
        """Sell `size` shares at or above `price`. Returns proceeds."""
        p = self.led.positions.get(token_id)
        if not p:
            return 0.0
        leg = Leg(token_id, "SELL", price, size, p.outcome)
        if self.live is None:
            self.led.record_sell(token_id, size, price, kind)
            return size * price
        try:
            filled, raw = self.live.fill_or_kill(leg, None, p.neg_risk)
        except Exception as e:
            log.error("  sell failed for %s: %s", p.outcome, e)
            return 0.0
        if filled <= 0:
            return 0.0
        self.led.record_sell(token_id, filled, price, kind, raw)
        return filled * price
