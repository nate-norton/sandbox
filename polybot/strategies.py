"""Opportunity finders. Pure functions over markets + order books, so they are unit-testable."""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime

from .config import Config
from .decider import Assessment
from .models import Book, Leg, Market, Opportunity

log = logging.getLogger(__name__)


def est_fee(bps: int, size: float, price: float) -> float:
    """Taker-fee estimate in USD: Polymarket charges `rate * price * (1 - price)` per share
    (docs.polymarket.com/trading/fees). At 1000 bps that is 2.5c per share at 50c and 0.4c at 96c.
    """
    return bps / 10_000.0 * size * price * (1.0 - price)


def _round_down(x: float, step: float) -> float:
    return int(x / step + 1e-9) * step


# --------------------------------------------------------------------------- pair arbitrage
def find_pair_arbs(markets: list[Market], books: dict[str, Book], cfg: Config, now: datetime,
                   budget: float, stats: Counter | None = None) -> list[Opportunity]:
    """Binary market where YES ask + NO ask < 1: buying both legs locks in $1 per pair at resolution."""
    out: list[Opportunity] = []
    st = stats if stats is not None else Counter()
    best_pair = 9.0
    for m in markets:
        if not m.is_binary or m.closed:
            continue
        hrs = m.hours_to_end(now)
        if hrs is None or hrs <= 0 or hrs > cfg.arb_max_days_to_resolution * 24:
            st["arb.window"] += 1
            continue
        by, bn = books.get(m.token_ids[0]), books.get(m.token_ids[1])
        if not by or not bn or by.best_ask is None or bn.best_ask is None:
            st["arb.no_book"] += 1
            continue
        # Best-level edge; then size to what the book can actually fill at those levels.
        pair_cost = by.best_ask + bn.best_ask
        best_pair = min(best_pair, pair_cost)
        edge = 1.0 - pair_cost
        fee_per_share = est_fee(by.fee_bps, 1, by.best_ask) + est_fee(bn.fee_bps, 1, bn.best_ask)
        edge -= fee_per_share
        if edge < cfg.arb_min_edge:
            st["arb.no_edge"] += 1
            continue
        depth = min(by.ask_depth_at_or_below(by.best_ask), bn.ask_depth_at_or_below(bn.best_ask))
        size = _round_down(min(depth, budget / pair_cost), 1.0)
        if size < m.min_order_size:
            st["arb.too_small"] += 1
            continue
        profit = size * edge
        if profit < cfg.arb_min_profit_usd:
            st["arb.profit_below_min"] += 1
            continue
        legs = [
            Leg(m.token_ids[0], "BUY", by.best_ask, size, m.outcomes[0]),
            Leg(m.token_ids[1], "BUY", bn.best_ask, size, m.outcomes[1]),
        ]
        out.append(Opportunity("pair_arb", m, legs, size * pair_cost, profit, edge,
                               note=f"YES {by.best_ask:.3f} + NO {bn.best_ask:.3f} = {pair_cost:.3f}, {hrs/24:.1f}d to end"))
    if best_pair < 9.0:
        st["arb.best_pair_x1000"] = int(best_pair * 1000)
    out.sort(key=lambda o: -o.expected_profit)
    return out


# --------------------------------------------------------------------------- neg-risk arbitrage
def find_negrisk_arbs(markets: list[Market], books: dict[str, Book], cfg: Config, now: datetime,
                      budget: float) -> list[Opportunity]:
    """Multi-outcome event where exactly one outcome pays: sum of YES asks < 1 => buy every YES."""
    groups: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        if m.neg_risk and m.event_id and m.is_binary and not m.closed:
            groups[m.event_id].append(m)
    out: list[Opportunity] = []
    for ev, ms in groups.items():
        if len(ms) < 2:
            continue
        hrs = [m.hours_to_end(now) for m in ms]
        if any(h is None or h <= 0 or h > cfg.arb_max_days_to_resolution * 24 for h in hrs):
            continue
        yes_books = [books.get(m.token_ids[0]) for m in ms]
        if any(b is None or b.best_ask is None for b in yes_books):
            continue
        total = sum(b.best_ask for b in yes_books)
        fee_ps = sum(est_fee(b.fee_bps, 1, b.best_ask) for b in yes_books)
        edge = 1.0 - total - fee_ps
        # extra margin: an event can gain outcomes later, which is why this is opt-in
        if edge < cfg.arb_min_edge * 2:
            continue
        depth = min(b.ask_depth_at_or_below(b.best_ask) for b in yes_books)
        size = _round_down(min(depth, budget / total), 1.0)
        if size < max(m.min_order_size for m in ms):
            continue
        profit = size * edge
        if profit < cfg.arb_min_profit_usd:
            continue
        legs = [Leg(m.token_ids[0], "BUY", b.best_ask, size, m.outcomes[0] + " / " + m.question[:40])
                for m, b in zip(ms, yes_books)]
        out.append(Opportunity("negrisk_arb", ms[0], legs, size * total, profit, edge,
                               note=f"{len(ms)} outcomes sum to {total:.3f} in event {ms[0].event_title[:50]}"))
    out.sort(key=lambda o: -o.expected_profit)
    return out


# --------------------------------------------------------------------------- favorite harvesting
def find_harvests(markets: list[Market], books: dict[str, Book], cfg: Config, now: datetime,
                  per_position_budget: float, held: set[str], stats: Counter | None = None,
                  assessments: dict[str, Assessment] | None = None) -> list[Opportunity]:
    """Buy the heavy favorite of a liquid market that resolves within hours.

    This is not risk-free: it monetises the favourite-longshot bias (favourites at 94-98c
    historically win slightly more often than their price implies) and the time value of a
    quick resolution. Every filter below exists to avoid the cases where that bias reverses:
    thin books, wide spreads, fee-bearing markets, and long holding periods.
    """
    out: list[Opportunity] = []
    st = stats if stats is not None else Counter()
    for m in markets:
        if not m.is_binary or m.closed or m.condition_id in held:
            continue
        hrs = m.hours_to_end(now)
        if hrs is None or hrs <= 0.5 or hrs > cfg.harvest_max_hours:
            st["harvest.window"] += 1
            continue
        st["harvest.in_window"] += 1
        if m.liquidity < cfg.harvest_min_liquidity or m.volume24h < cfg.harvest_min_volume24h:
            st["harvest.illiquid"] += 1
            continue
        for idx in (0, 1):
            b = books.get(m.token_ids[idx])
            if not b or b.best_ask is None or b.best_bid is None:
                st["harvest.no_book"] += 1
                continue
            ask, bid = b.best_ask, b.best_bid
            if ask < cfg.harvest_min_price or ask > cfg.harvest_max_price:
                st["harvest.price_out_of_range"] += 1
                continue
            if b.fee_bps > cfg.harvest_max_fee_bps:
                st["harvest.fee_market"] += 1
                continue
            if ask - bid > cfg.harvest_max_spread:
                st["harvest.wide_spread"] += 1
                continue
            fee_ps = est_fee(b.fee_bps, 1, ask)
            gross = (1.0 - ask - fee_ps) / ask            # net return if it resolves YES
            annualized = gross * (365 * 24 / max(hrs, 1.0))
            if annualized < cfg.harvest_min_annualized:
                st["harvest.low_annualized"] += 1
                continue
            size = _round_down(per_position_budget / ask, 1.0)
            if size < m.min_order_size:
                st["harvest.below_min_order"] += 1
                continue
            filled = b.cost_to_buy(size)
            if not filled:
                st["harvest.no_depth"] += 1
                continue
            cost, worst = filled
            if worst > cfg.harvest_max_price:
                st["harvest.slippage"] += 1
                continue
            # Expected value assumes the favourite is underpriced by `harvest_assumed_edge`
            # (net of bad-resolution risk). Paying up through the book erodes that edge.
            p_win = min(0.999, ask + cfg.harvest_assumed_edge)
            fee = est_fee(b.fee_bps, size, worst)
            cost += fee
            ev = size * p_win - cost
            if ev <= 0:
                st["harvest.negative_ev"] += 1
                continue
            ai_note, ai_p = "", None
            a = (assessments or {}).get(m.condition_id)
            if a and a.p_yes is not None:
                ai_p = a.p_yes if idx == 0 else 1.0 - a.p_yes
                if ai_p < cfg.ai_gate_min_p:
                    st["harvest.ai_vetoed_p"] += 1
                    continue
                if a.risk is not None and a.risk > cfg.ai_gate_max_risk:
                    st["harvest.ai_vetoed_risk"] += 1
                    continue
                ai_note = f", jev p={ai_p:.2f} risk={a.risk if a.risk is not None else -1:.1f}"
            st["harvest.candidate"] += 1
            out.append(Opportunity(
                "harvest", m, [Leg(m.token_ids[idx], "BUY", worst, size, m.outcomes[idx])],
                cost, ev, 1.0 - worst,
                note=f"{m.outcomes[idx]} @ {ask:.3f}, {hrs:.1f}h to end, {gross*100:.1f}% net if right, fee {b.fee_bps}bps, liq ${m.liquidity:,.0f}{ai_note}",
            ))
            out[-1].ai_p = ai_p
    # Prefer the favourites Jev is surest about, then soonest resolution, then liquidity
    out.sort(key=lambda o: (-(o.ai_p or 0.0), o.market.hours_to_end(now) or 1e9, -o.market.liquidity))
    return out


# --------------------------------------------------------------------------- AI edge finder
def find_ai_edges(markets: list[Market], books: dict[str, Book], assessments: dict[str, Assessment],
                  cfg: Config, now: datetime, per_position_budget: float, held: set[str],
                  stats: Counter | None = None) -> list[Opportunity]:
    """Buy a side priced well below what Jev, judging without seeing the price, thinks it is worth.

    Jev only saw the question, rules and timing (no price), so agreement is independent evidence.
    Only extreme answers are acted on (cfg.ai_edge_min_p), and the price must already be leaning
    the same way (cfg.ai_edge_min_price): a market at 30c that Jev calls 97% usually means the
    market knows something the rules do not say, so that is skipped rather than bought.
    """
    out: list[Opportunity] = []
    st = stats if stats is not None else Counter()
    for m in markets:
        a = assessments.get(m.condition_id)
        if not a or a.p_yes is None or a.with_prices or m.condition_id in held or not m.is_binary:
            continue
        hrs = m.hours_to_end(now)
        if hrs is None or hrs <= 0.5 or hrs > cfg.ai_edge_max_hours:
            continue
        # which side does Jev favour?
        if a.p_yes >= cfg.ai_edge_min_p:
            idx, p = 0, a.p_yes
        elif a.p_yes <= 1.0 - cfg.ai_edge_min_p:
            idx, p = 1, 1.0 - a.p_yes
        else:
            st["ai_edge.not_extreme"] += 1
            continue
        if a.risk is not None and a.risk > cfg.ai_gate_max_risk:
            st["ai_edge.risky_rules"] += 1
            continue
        b = books.get(m.token_ids[idx])
        if not b or b.best_ask is None or b.best_bid is None:
            st["ai_edge.no_book"] += 1
            continue
        ask = b.best_ask
        if ask < cfg.ai_edge_min_price or ask > cfg.ai_edge_max_price:
            st["ai_edge.price_out_of_band"] += 1
            continue
        if p - ask < cfg.ai_edge_min_gap:
            st["ai_edge.gap_too_small"] += 1
            continue
        if ask - b.best_bid > 0.06:
            st["ai_edge.wide_spread"] += 1
            continue
        size = _round_down(per_position_budget / ask, 1.0)
        if size < m.min_order_size:
            st["ai_edge.below_min_order"] += 1
            continue
        filled = b.cost_to_buy(size)
        if not filled:
            st["ai_edge.no_depth"] += 1
            continue
        cost, worst = filled
        cost += est_fee(b.fee_bps, size, worst)
        # discount Jev's number toward the market before computing EV: it is a score, not gospel
        p_used = 0.5 * p + 0.5 * ask
        ev = size * p_used - cost
        if ev <= 0:
            st["ai_edge.negative_ev"] += 1
            continue
        st["ai_edge.candidate"] += 1
        o = Opportunity("ai_edge", m, [Leg(m.token_ids[idx], "BUY", worst, size, m.outcomes[idx])],
                        cost, ev, p - worst,
                        note=f"{m.outcomes[idx]} @ {ask:.3f} vs jev {p:.2f} (risk {a.risk if a.risk is not None else -1:.1f}), {hrs:.0f}h to end, liq ${m.liquidity:,.0f}")
        o.ai_p = p
        out.append(o)
    out.sort(key=lambda o: -o.edge)
    return out
