"""Bankroll protection. Every order passes through here first."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .config import Config
from .ledger import Ledger
from .models import Opportunity

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    cash: float
    deployed: float
    equity: float
    open_positions: int
    pnl_today: float
    spent_this_run: float = 0.0
    orders_this_run: int = 0


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def halted(self, st: RiskState) -> str | None:
        """Return a reason string if trading must stop for this run."""
        if os.path.exists(self.cfg.kill_switch_file):
            return f"kill switch present: {self.cfg.kill_switch_file}"
        limit = -self.cfg.daily_loss_limit_frac * max(st.equity, self.cfg.starting_bankroll)
        if st.pnl_today < limit:
            return f"daily loss limit hit ({st.pnl_today:.2f} < {limit:.2f})"
        floor = self.cfg.starting_bankroll * self.cfg.min_equity_frac
        if st.equity < floor:
            return f"equity {st.equity:.2f} below floor {floor:.2f} ({self.cfg.min_equity_frac:.0%} of start); stopping to preserve capital"
        return None

    def spendable(self, st: RiskState) -> float:
        """Cash available for new buys right now."""
        c = self.cfg
        cap_total = c.max_deployed_frac * st.equity - st.deployed
        cap_run = c.max_spend_per_run - st.spent_this_run
        cap_cash = st.cash - c.cash_reserve
        return max(0.0, min(cap_total, cap_run, cap_cash))

    def per_position_budget(self, st: RiskState) -> float:
        return max(0.0, min(self.cfg.max_position_frac * st.equity, self.spendable(st)))

    def approve(self, opp: Opportunity, st: RiskState) -> str | None:
        """None if approved, else the rejection reason."""
        c = self.cfg
        if st.orders_this_run + len(opp.legs) > c.max_orders_per_run:
            return "order cap for this run"
        if opp.cost > self.spendable(st) + 1e-9:
            return f"cost {opp.cost:.2f} exceeds spendable {self.spendable(st):.2f}"
        # arbitrage is hedged, so it may use the whole spendable amount; directional trades may not
        if opp.kind == "harvest":
            if st.open_positions >= c.max_open_positions:
                return "max open positions"
            if opp.cost > c.max_position_frac * st.equity + 1e-9:
                return f"cost {opp.cost:.2f} exceeds per-position cap"
        if opp.expected_profit <= 0:
            return "no expected profit"
        return None

    def commit(self, opp: Opportunity, st: RiskState, spent: float) -> None:
        st.spent_this_run += spent
        st.orders_this_run += len(opp.legs)
        st.cash -= spent
        st.deployed += spent
        if opp.kind == "harvest" and spent > 0:
            st.open_positions += 1


def state_from_ledger(led: Ledger) -> RiskState:
    return RiskState(cash=led.cash, deployed=led.deployed, equity=led.equity,
                     open_positions=len(led.positions), pnl_today=led.pnl_today())
