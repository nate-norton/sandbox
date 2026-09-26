"""NFL / college-football strategy: trade Polymarket moneylines against ESPN's numbers.

Model probability per side, in order of preference:
  final score (game completed)  >  ESPN live win probability (in progress)  >  no-vig ESPN BET moneyline (pre-game)
Edge = model p - ask - taker fee. A trade needs edge above a margin that depends on how noisy the
model is, and is sized with fractional Kelly, capped by the risk profile. Held positions are sold
when the market pays more than the model says they are worth.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .config import Config
from .espn import Game
from .ledger import Ledger
from .models import Book, Leg, Market, Opportunity
from .strategies import _round_down, est_fee

log = logging.getLogger(__name__)


@dataclass
class Matched:
    market: Market
    game: Game
    sides: list[str]                # per outcome index: "home" / "away"


def is_moneyline(m: Market) -> bool:
    if not m.is_binary:
        return False
    if m.sports_type:
        return m.sports_type.lower() == "moneyline"
    q = m.question.lower()
    return " vs" in q and "spread" not in q and "o/u" not in q and "over" not in q and "exact score" not in q


def match_markets(markets: list[Market], games: list[Game], now: datetime, stats: Counter | None = None) -> list[Matched]:
    """A market matches a game when both outcome labels name the game's two teams and the timing agrees."""
    st = stats if stats is not None else Counter()
    out: list[Matched] = []
    for m in markets:
        if not is_moneyline(m):
            continue
        st["sports.moneyline_markets"] += 1
        when = m.game_start or m.end_date
        best = None
        for g in games:
            sides = []
            for i, o in enumerate(m.outcomes):
                labels = [o] + (m.outcome_aliases[i] if i < len(m.outcome_aliases) else [])
                side = next((s for s in (g.team_for(l) for l in labels) if s), None)
                sides.append(side)
            if None in sides or sides[0] == sides[1]:
                continue
            if when and g.start and abs((g.start - when).total_seconds()) > 36 * 3600:
                continue
            best = Matched(m, g, sides)
            break
        if best:
            out.append(best)
            st["sports.matched"] += 1
        else:
            st["sports.unmatched"] += 1
    return out


def _margin(cfg: Config, g: Game, p: float, src: str) -> float:
    """Required edge. Live contested games need more edge as the clock runs down, because the
    market sees possession, fouls and timeouts before a public feed does (ncaam-live-trader ramp)."""
    if src == "final":
        return cfg.sports_margin_final
    if src == "live_wp":
        if p >= cfg.sports_decided_p:
            return cfg.sports_margin_live_sure
        return cfg.sports_margin_live + cfg.sports_margin_live_ramp * g.elapsed_fraction() ** 2
    return cfg.sports_margin_pre


def _live_blocked(cfg: Config, g: Game, p: float, src: str, stats: Counter) -> bool:
    if src != "live_wp":
        return False
    if not g.stable:
        stats["sports.unstable"] += 1
        return True
    contested = cfg.sports_decided_p > p > 1.0 - cfg.sports_decided_p
    if contested and g.minutes_left() < cfg.sports_min_minutes_left:
        stats["sports.late_contested"] += 1
        return True
    return False


def blended_p(cfg: Config, g: Game, p: float, src: str, ask: float, bid: float) -> float:
    """Shrink ESPN's live win probability toward the market early in a game. ESPN's number reacts to
    a single drive at 0-0 in the first quarter (Maryland: 44% pregame, 57% after one drive, lost)
    while the market barely moves; the market is the sharper number until the score does the
    talking. The weight on the market starts at sports_market_blend at kickoff and falls linearly to
    zero at the end of regulation; a decided game (p past sports_decided_p) in its second half is
    taken at face value, since that is where the market lags the scoreboard."""
    if src != "live_wp" or cfg.sports_market_blend <= 0:
        return p
    elapsed = g.elapsed_fraction()
    decided = p >= cfg.sports_decided_p or p <= 1.0 - cfg.sports_decided_p
    if decided and elapsed >= 0.5:
        return p
    w_market = cfg.sports_market_blend * max(0.0, 1.0 - elapsed)
    mid = (ask + bid) / 2.0
    return (1.0 - w_market) * p + w_market * mid


def find_sports_edges(matched: list[Matched], books: dict[str, Book], cfg: Config, now: datetime,
                      equity: float, per_position_cap: float, held: set[str],
                      stats: Counter | None = None) -> list[Opportunity]:
    st = stats if stats is not None else Counter()
    out: list[Opportunity] = []
    for mm in matched:
        m, g = mm.market, mm.game
        if m.condition_id in held or m.closed:
            continue
        if g.start and g.state == "pre" and (g.start - now) > timedelta(hours=cfg.sports_max_hours_ahead):
            st["sports.too_far_ahead"] += 1
            continue
        if 0 < m.liquidity < cfg.sports_min_liquidity:      # unknown (0) liquidity: let book depth decide
            st["sports.illiquid"] += 1
            continue
        for idx, side in enumerate(mm.sides):
            p, src = g.model_p(side)
            if p is None:
                st["sports.no_model"] += 1
                continue
            if _live_blocked(cfg, g, p, src, st):
                continue
            b = books.get(m.token_ids[idx])
            if not b or b.best_ask is None or b.best_bid is None:
                st["sports.no_book"] += 1
                continue
            ask, bid = b.best_ask, b.best_bid
            if ask >= 0.99 or ask < cfg.sports_min_price:
                st["sports.price_out_of_band"] += 1
                continue
            if ask - bid > cfg.sports_max_spread:
                st["sports.wide_spread"] += 1
                continue
            p_raw = p
            p = blended_p(cfg, g, p, src, ask, bid)
            fee_ps = est_fee(b.fee_bps, 1, ask)
            edge = p - ask - fee_ps
            margin = _margin(cfg, g, p, src)
            if edge < margin:
                st["sports.no_edge"] += 1
                continue
            # fractional Kelly on the net odds offered at the ask
            f_star = (p - ask) / (1.0 - ask)
            stake = cfg.sports_kelly_frac * f_star * equity
            stake = min(stake, per_position_cap)
            size = _round_down(stake / ask, 1.0)
            if size < m.min_order_size:
                if stake >= 0.5 * m.min_order_size * ask:          # edge is real but tiny bankroll: take the minimum
                    size = m.min_order_size
                else:
                    st["sports.below_min_order"] += 1
                    continue
            if size * ask > per_position_cap + 1e-9:
                st["sports.over_cap"] += 1
                continue
            filled = b.cost_to_buy(size)
            if not filled:
                st["sports.no_depth"] += 1
                continue
            cost, worst = filled
            fee = est_fee(b.fee_bps, size, worst)
            ev = size * p - cost - fee
            if ev <= 0 or p - worst - fee_ps < margin:
                st["sports.slippage"] += 1
                continue
            st["sports.candidate"] += 1
            o = Opportunity("sports_edge", m, [Leg(m.token_ids[idx], "BUY", worst, size, m.outcomes[idx])],
                            cost + fee, ev, p - worst - fee_ps,
                            note=f"{m.outcomes[idx]} @ {ask:.3f} vs model {p:.3f} ({src}{'' if p == p_raw else f' {p_raw:.3f} blended with market'}, "
                                 f"margin {margin:.3f}); {g.summary}; kelly f*={f_star:.2f}")
            o.ai_p = p
            o.model_src = src
            out.append(o)
    out.sort(key=lambda o: -o.edge)
    return out


def sports_exits(matched: list[Matched], books: dict[str, Book], led: Ledger, cfg: Config) -> list[tuple[str, float, float, str]]:
    """(token_id, size, bid, note) for held positions the market now values above the model."""
    by_cid = {mm.market.condition_id: mm for mm in matched}
    out = []
    for tid, pos in led.positions.items():
        mm = by_cid.get(pos.condition_id)
        if not mm or tid not in mm.market.token_ids:
            continue
        idx = mm.market.token_ids.index(tid)
        p, src = mm.game.model_p(mm.sides[idx])
        b = books.get(tid)
        if p is None or not b or b.best_bid is None or src == "final":
            continue
        if src == "live_wp" and not mm.game.stable:
            continue
        bid = b.best_bid
        fee_ps = est_fee(b.fee_bps, 1, bid)
        decided = p >= cfg.sports_decided_p or p <= 1.0 - cfg.sports_decided_p
        allowance = cfg.sports_exit_margin_decided if decided else cfg.sports_exit_margin
        if bid - fee_ps - p >= allowance:
            out.append((tid, pos.size, bid, f"sell {pos.outcome} @ {bid:.3f}: model {p:.3f} ({src}); {mm.game.summary}"))
    return out


def near_misses(matched: list[Matched], books: dict[str, Book], now: datetime, n: int = 10) -> list[str]:
    rows = []
    for mm in matched:
        m, g = mm.market, mm.game
        for idx, side in enumerate(mm.sides):
            p, src = g.model_p(side)
            b = books.get(m.token_ids[idx])
            if p is None or not b or b.best_ask is None:
                continue
            rows.append((-(p - b.best_ask), f"{m.outcomes[idx]:<22} ask {b.best_ask:.3f} model {p:.3f} ({src:9}) {g.summary}"))
    rows.sort()
    return [r[1] for r in rows[:n]]
