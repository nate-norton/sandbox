"""One bot cycle. Designed to be run by cron/CI every ~30 minutes and to be safe to re-run.

    python -m polybot.run          # paper mode unless POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER are set
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from .ai_cache import AiCache
from .clob import DataApi, LiveClob, PublicClob
from .config import Config
from .decider import Assessment, JevDecider
from .espn import Espn, Game
from .executor import Executor
from .gamma import Gamma
from .ledger import Ledger, Position
from .models import Book, Market, parse_iso
from . import observe
from .pmus import PolymarketUS, make_client
from .risk import RiskManager, RiskState, state_from_ledger
from .sports import Matched, find_sports_edges, match_markets, near_misses, sports_exits
from .strategies import find_ai_edges, find_harvests, find_negrisk_arbs, find_pair_arbs
from .wallet import candidate_funders, normalize_private_key

log = logging.getLogger("polybot")

def run_once(cfg: Config, gamma: Optional[Gamma], clob, live=None,
             data: Optional[DataApi] = None, now: Optional[datetime] = None,
             decider: Optional[JevDecider] = None, espn: Optional[Espn] = None,
             us: Optional[PolymarketUS] = None) -> Ledger:
    """One cycle. On the global exchange `gamma`/`clob` supply markets and books; on Polymarket US
    the `us` adapter supplies both (and, when authenticated, is also the `live` executor)."""
    now = now or datetime.now(timezone.utc)
    mode = "live" if live else "paper"
    path = os.path.join(cfg.state_dir, "ledger.json")
    led = Ledger.load(path, cfg.starting_bankroll, mode)
    led.runs += 1
    led.last_run = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    problem = os.environ.get("POLYBOT_KEY_PROBLEM")
    if problem and not any(problem in n for n in led.notes[-3:]):
        led.notes.append(f"{led.last_run} {problem}")
    log.info("=== polybot run %d (%s mode, %s profile) ===", led.runs, mode, cfg.profile)

    if live:
        _sync_live(led, live, data, cfg)

    if us is not None:
        markets = us.football_markets(cfg.sports_leagues)
    else:
        markets = gamma.active_markets(limit=cfg.scan_limit)
    by_cid = {m.condition_id: m for m in markets}

    # sports mode: only NFL / CFB games that ESPN knows about
    games: list[Game] = []
    matched: list[Matched] = []
    if cfg.sports_only:
        if espn is None:
            espn = Espn()
        for lg in cfg.sports_leagues:
            games += espn.games(lg, now)
        for g in games:
            if g.state == "in" and g.home_wp is None:
                g.home_wp = espn.live_wp(g)
        matched = match_markets(markets, games, now)
        matched_cids = {mm.market.condition_id for mm in matched}
        led.game_window = any(
            g.state == "in" or (g.state == "pre" and g.start and 0 <= (g.start - now).total_seconds() <= 45 * 60)
            for g in games if g.id in {mm.game.id for mm in matched})
        held_cids_now = {p.condition_id for p in led.positions.values()}
        markets = [m for m in markets if m.condition_id in matched_cids or m.condition_id in held_cids_now]
        log.info("sports: %d games, %d moneyline markets matched", len(games), len(matched))

    # 1) settle / mark held positions, and grade past Jev calls against resolved markets
    if not live:
        if us is not None:
            _settle_paper_us(led, us, by_cid)
        else:
            _settle_paper(led, gamma, by_cid)
    if gamma is not None:
        _grade_ai_log(led, gamma, by_cid, now)
    held_tokens = list(led.positions)
    # 2) candidate universe for books: binary markets inside either strategy window, plus holdings
    cands = []
    for m in markets:
        h = m.hours_to_end(now)
        if m.is_binary and h is not None and 0 < h <= cfg.arb_max_days_to_resolution * 24:
            cands.append(m)
    if cfg.sports_only:
        # books are the expensive, rate-limited call: only games close to kickoff (or under way) matter
        cands = [m for m in cands if m.game_start is None
                 or (m.game_start - now).total_seconds() <= cfg.sports_book_hours_ahead * 3600]
    tokens = list(dict.fromkeys([t for m in cands for t in m.token_ids] + held_tokens))
    t_books = time.monotonic()
    books = (us or clob).books(tokens, batch=cfg.book_batch)
    book_secs = time.monotonic() - t_books
    log.info("books: %d of %d tokens in %.0fs", len(books), len(tokens), book_secs)
    if cfg.sports_only and espn is not None and cfg.sports_stability_wait > 0 \
            and any(mm.game.state == "in" for mm in matched):
        # re-read ESPN *after* the (slow, rate-limited) book fetch so decisions use fresh scores and win
        # probabilities, and any game where a play landed while books were loading is skipped this cycle
        espn.stability_check([mm.game for mm in matched], max(0.0, cfg.sports_stability_wait - book_secs))

    ex = Executor(led, books, live)
    _mark_and_stop(led, books, ex, now, cfg.stop_loss_drop)
    if cfg.sports_only:
        if led.book_seen and not _BOOK_SEEN:
            for tid, (sig, n, at) in led.book_seen.items():          # restarts must not forget a frozen quote
                _BOOK_SEEN[tid] = (tuple(sig), int(n), parse_iso(at) or now)
        stale = _stale_books(books, matched, now)
        led.book_seen = {tid: [list(sig), n, at.isoformat()] for tid, (sig, n, at) in _BOOK_SEEN.items()}
        for tid in stale:
            books.pop(tid, None)            # a frozen quote is not a price: no entries or exits on it
    if cfg.sports_only:
        for tid, size, bid, note in sports_exits(matched, books, led, cfg):
            log.info("EXIT %s", note)
            got = ex.sell(tid, size, bid, "sports_exit")
            log.info("  sold for $%.2f", got)

    # 3) fee lookups only for candidates that pass a cheap price prefilter (keeps request count small)
    if us is None:
        _prefetch_fees(cands, books, clob, cfg)

    rm = RiskManager(cfg)
    st = state_from_ledger(led)
    halt = rm.halted(st)
    if halt:
        log.warning("trading halted: %s", halt)
        if not any(halt in n for n in led.notes[-3:]):
            led.notes.append(f"{led.last_run} halted: {halt}")

    held_cids = {p.condition_id for p in led.positions.values()}
    stats: Counter = Counter({"markets": len(markets), "candidates": len(cands), "books": len(books)})
    if cfg.sports_only:
        stats["sports.games"] = len(games)
        match_markets([mm.market for mm in matched], games, now, stats)   # recount for the report
        stats["sports.stale_book"] = sum(1 for t in _BOOK_SEEN.values() if t[1] >= STALE_AFTER)
        opps = find_sports_edges(matched, books, cfg, now, st.equity, rm.per_position_budget(st), held_cids, stats)
        # Jev is asked only about the markets we are about to buy, and always afresh for a game in
        # progress: a cached pregame opinion (Wisconsin 5%) vetoed a 98% fourth-quarter lead
        live_cids = {mm.market.condition_id for mm in matched if mm.game.state == "in"}
        gate, edge, ai_info = _assess_with_jev([o.market for o in opps], books, cfg, now, decider, led, stats, held_cids,
                                               game_notes={mm.market.condition_id: mm.game.summary for mm in matched},
                                               blind=False, no_cache=live_cids)
        led.ai_info = ai_info
        opps = _apply_gate(opps, gate, cfg, stats, led)
        # observation log: every quote next to the model, and each game's final outcome (once)
        stats["obs.rows"] = observe.append(cfg.state_dir, f"{now:%Y-%m-%d}.jsonl", observe.snapshot_rows(matched, books, cfg, now))
        outs = observe.outcome_rows(matched, set(led.resolved_events), now, games)
        if outs:
            observe.append(cfg.state_dir, "outcomes.jsonl", outs)
            led.resolved_events = (led.resolved_events + [o["event"] or f"game:{o['game']}" for o in outs])[-2000:]
            stats["obs.outcomes"] = len(outs)
        if us is None:
            opps += find_pair_arbs(cands, books, cfg, now, rm.spendable(st), stats)   # ties break the pair on US markets
        for line in near_misses(matched, books, now):
            log.info("  sports near: %s", line)
    else:
        gate, edge, ai_info = _assess_with_jev(cands, books, cfg, now, decider, led, stats, held_cids)
        led.ai_info = ai_info
        opps = find_pair_arbs(cands, books, cfg, now, rm.spendable(st), stats)
        if cfg.enable_negrisk_arb:
            opps += find_negrisk_arbs(cands, books, cfg, now, rm.spendable(st))
        if cfg.harvest_enabled:
            opps += find_harvests(cands, books, cfg, now, rm.per_position_budget(st), held_cids, stats, gate)
        if edge and not ai_info.get("breaker"):
            # AI edges first: they carry the largest edge per share, harvest fills the rest
            opps = find_ai_edges(cands, books, edge, cfg, now, rm.ai_edge_budget(st), held_cids, stats) + opps
        _log_near_misses(cands, books, cfg, now)
    log.info("opportunities: %d (spendable $%.2f)", len(opps), rm.spendable(st))
    log.info("scan stats: %s", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    led.scan = dict(sorted(stats.items()))

    done = []
    seen_cids: set[str] = set()
    if halt and opps:
        _decide(led, f"{len(opps)} opportunit{'y' if len(opps) == 1 else 'ies'} not taken: {halt}")
    for o in opps if not halt else []:
        if o.market.condition_id in seen_cids:
            continue
        leg = o.legs[0]
        what = f"{leg.outcome} @ {leg.price:.3f} x{leg.size:.0f} (model {o.ai_p if o.ai_p is not None else 0:.2f}, {o.model_src or o.kind})"
        why = rm.approve(o, st)
        if why:
            log.info("skip %s %s: %s", o.kind, o.market.question[:60], why)
            _decide(led, f"skip {what}: {why}")
            continue
        log.info("TRADE %s: %s | %s | cost $%.2f, +$%.3f exp", o.kind, o.market.question[:70], o.note, o.cost, o.expected_profit)
        if us is not None and live is not None:
            moved = _refresh_live_price(us, o, cfg)
            if moved:
                _decide(led, f"skip {what}: {moved}")
                continue
            fee_why = _us_fee_ok(us, o, cfg)
            if fee_why:
                _decide(led, f"skip {what}: {fee_why}")
                continue
        spent = ex.buy(o)
        rm.commit(o, st, spent)
        seen_cids.add(o.market.condition_id)
        if spent > 0:
            done.append(o)
            _decide(led, f"BOUGHT {what} for ${spent:.2f}")
        else:
            fresh = o.note[o.note.rfind("; fresh ") + 2:] if "; fresh " in o.note else ""
            _decide(led, f"NOT FILLED {what}: {ex.last_error or 'order returned no fill'}{' | ' + fresh if fresh else ''}")
    _finish(led, path, cfg, st, done, halt)
    return led


# ----------------------------------------------------------------------------- helpers
def resolve_wallet(cfg: Config, make_client=None, data: Optional[DataApi] = None) -> LiveClob:
    """Build the live client. With no POLYMARKET_FUNDER, derive the wallet from the key and pick
    the candidate that actually holds USDC (or open positions): proxy (email login), Safe
    (browser-wallet login), then the bare signer."""
    make = make_client or (lambda funder, sig: LiveClob(cfg.clob_host, cfg.chain_id, cfg.private_key, funder, sig))
    if cfg.funder:
        return make(cfg.funder, cfg.signature_type)
    cands = candidate_funders(cfg.private_key)
    order = [cfg.signature_type] + [t for t in (1, 2, 0) if t != cfg.signature_type]
    first = None
    for sig in order:
        addr = cands[sig]
        try:
            client = make(addr, sig)
            bal = client.usdc_balance()
        except Exception as e:
            log.warning("wallet candidate type %d %s failed: %s", sig, addr, e)
            continue
        first = first or client
        has_positions = bool(data and data.positions(addr))
        log.info("wallet candidate type %d %s: $%.2f USDC%s", sig, addr, bal, ", has positions" if has_positions else "")
        if bal > 0 or has_positions:
            log.info("using wallet %s (signature type %d)", addr, sig)
            return client
    if first is None:
        raise SystemExit("could not reach the CLOB with any derived wallet; check POLYMARKET_PRIVATE_KEY")
    log.warning("no derived wallet holds USDC yet; defaulting to %s (type %d). Candidates: %s",
                first.funder, first.signature_type, ", ".join(f"type {k}: {v}" for k, v in cands.items()))
    return first


def _us_fee_ok(us: PolymarketUS, o, cfg: Config) -> str:
    """Re-check the edge with the exchange's exact commission before spending real money.
    Returns an empty string when the trade still clears, else the reason to skip it."""
    leg = o.legs[0]
    fee = us.preview_fee(leg.token_id, "BUY", leg.price, int(leg.size))
    if fee is None:
        return ""
    p = o.ai_p or 0.0
    edge = p - leg.price - fee / max(leg.size, 1)
    if edge < cfg.sports_margin_final:
        log.info("  skipped after fee preview: fee $%.3f leaves edge %.3f", fee, edge)
        return f"exchange fee preview ${fee:.3f} leaves edge {edge:.3f}"
    return ""


_BOOK_SEEN: dict[str, tuple[tuple, int, datetime]] = {}
STALE_AFTER = 3          # identical top-of-book on this many consecutive reads of a live game
STALE_MIN_GAP = 60.0     # seconds between reads for a repeat to count (a re-read seconds later proves nothing)


def _stale_books(books: dict, matched: list, now: datetime) -> set[str]:
    """Tokens whose top of book has not moved at all across STALE_AFTER consecutive reads while the
    game is live. The public gateway kept serving one frozen Boston College quote for 20 minutes
    while the score changed; orders against it just expire, so treat it as no quote."""
    stale: set[str] = set()
    live_tokens = {t for mm in matched if mm.game.state == "in" for t in mm.market.token_ids}
    for tid, b in books.items():
        if tid not in live_tokens:
            _BOOK_SEEN.pop(tid, None)
            continue
        sig = (b.best_ask, b.best_bid, b.asks[0].size if b.asks else 0.0, b.bids[0].size if b.bids else 0.0)
        prev, n, at = _BOOK_SEEN.get(tid, (None, 0, now))
        if sig != prev:
            n, at = 1, now
        elif (now - at).total_seconds() >= STALE_MIN_GAP:
            n, at = n + 1, now
        _BOOK_SEEN[tid] = (sig, n, at)
        if n >= STALE_AFTER:
            stale.add(tid)
    if stale:
        log.info("stale books (frozen %d+ reads while live): %s", STALE_AFTER, ", ".join(sorted(stale)))
    return stale


def _refresh_live_price(us: PolymarketUS, o, cfg: Config) -> str:
    """Re-read this one market's book seconds before ordering. The scan's book is ~30s old by now
    (ESPN re-read, Jev calls) and in a live game that is a lifetime. Lift the limit one tick above
    the fresh ask so an immediate-or-cancel order takes the top of book; give up if the ask ran
    more than the slippage allowance above the price the edge was computed on."""
    leg = o.legs[0]
    try:
        b = us.books([leg.token_id], fresh=True).get(leg.token_id)
    except Exception as e:
        log.info("  fresh book failed for %s: %s", leg.outcome, e)
        return ""
    if not b or b.best_ask is None:
        return "no ask on the fresh book"
    o.note += f"; fresh {getattr(us, 'fresh_source', '') or 'gateway'} book {b.best_bid:.3f}/{b.best_ask:.3f} x{b.asks[0].size:.0f}"
    tick = o.market.tick_size or 0.005
    if b.best_ask > leg.price + cfg.sports_max_slippage + 1e-9:
        return f"ask moved {leg.price:.3f} -> {b.best_ask:.3f} before the order"
    new_price = round(max(leg.price, b.best_ask) + tick, 4)
    if new_price != leg.price:
        log.info("  limit %.3f -> %.3f (fresh ask %.3f + 1 tick)", leg.price, new_price, b.best_ask)
        leg.price = new_price
        o.cost = round(leg.size * new_price, 4)
    return ""


def _decide(led: Ledger, msg: str) -> None:
    """Persist a one-line trade decision so the report shows why money did or did not move."""
    led.decisions.append(f"{led.last_run} {msg}")
    led.decisions = led.decisions[-60:]


def _settle_paper_us(led: Ledger, us: PolymarketUS, by_cid: dict[str, Market]) -> None:
    held = {p.condition_id for p in led.positions.values()}
    if not held:
        return
    done = us.resolved(held)
    for tid, p in list(led.positions.items()):
        pays = done.get(p.condition_id)
        m = by_cid.get(p.condition_id)
        if pays and m and tid in m.token_ids:
            payout = pays[m.token_ids.index(tid)]
            pnl = led.settle(tid, payout)
            log.info("settled %s %s -> %.0f (pnl %+.3f)", p.question[:50], p.outcome, payout, pnl)


def _sync_live(led: Ledger, live, data: Optional[DataApi], cfg: Config) -> None:
    """Live truth comes from the exchange: cash and positions from the venue's account endpoints."""
    try:
        led.cash = live.usdc_balance()
    except Exception as e:
        log.error("balance lookup failed: %s", e)
        raise SystemExit(2)
    led.wallet = live.funder
    if led.cash <= 0 and not led.positions:
        msg = (f"{led.last_run} live wallet {live.funder} shows $0 USDC. If you have deposited, compare this "
               "with the address on your Polymarket profile and set POLYMARKET_FUNDER to that one.")
        log.warning(msg)
        if not any("shows $0 USDC" in n for n in led.notes[-3:]):
            led.notes.append(msg)
    rows = None
    if data:
        rows = data.positions(live.funder)
    elif hasattr(live, "positions"):
        rows = live.positions()
    if rows is not None:
        pos: dict[str, Position] = {}
        for r in rows:
            tid = str(r.get("asset", ""))
            size = float(r.get("size") or 0)
            if not tid or size <= 0:
                continue
            old = led.positions.get(tid)
            pos[tid] = Position(
                token_id=tid, condition_id=str(r.get("conditionId", "")), question=str(r.get("title", "")),
                outcome=str(r.get("outcome", "")), size=size, avg_price=float(r.get("avgPrice") or 0),
                end_date=r.get("endDate"), kind=old.kind if old else "external",
                opened_at=old.opened_at if old else led.last_run, neg_risk=bool(r.get("negativeRisk", False)),
                mark=float(r.get("curPrice") or 0),
            )
            if r.get("redeemable"):
                led.notes.append(f"{led.last_run} redeemable: {pos[tid].question[:60]} ({size:.1f} {pos[tid].outcome})")
        for tid, p in list(led.positions.items()):
            if tid in pos:
                continue
            pay = live.payout(tid) if hasattr(live, "payout") else None
            if pay is not None:
                pnl = led.record_settlement(tid, pay)
                note = (f"{led.last_run} settled {p.question[:50]}: {p.outcome} paid {pay:.2f}/share on "
                        f"{p.size:.0f} @ {p.avg_price:.3f} -> {pnl:+.2f}")
            else:
                note = (f"{led.last_run} position gone from the exchange (sold by hand, or settlement not posted yet): "
                        f"{p.question[:50]} {p.outcome} {p.size:.0f} @ {p.avg_price:.3f}")
            log.info(note)
            led.notes.append(note)
        led.positions = pos
    if led.equity <= 0 and not led.positions:
        led.starting_bankroll = 0.0                  # nothing has arrived yet; measure from the first funded run
    elif led.starting_bankroll <= 0:
        led.starting_bankroll = led.equity           # first live run with money in the wallet
    try:
        if live.open_orders():
            live.cancel_all()          # we only ever use fill-or-kill; anything resting is stale
    except Exception as e:
        log.warning("open-order check failed: %s", e)


def _settle_paper(led: Ledger, gamma: Gamma, by_cid: dict[str, Market]) -> None:
    missing = {p.condition_id for p in led.positions.values() if p.condition_id not in by_cid}
    found = gamma.markets_by_condition(missing) if missing else {}
    for tid, p in list(led.positions.items()):
        m = by_cid.get(p.condition_id) or found.get(p.condition_id)
        if not m:
            continue
        rp = m.resolved_prices
        if rp is not None and tid in m.token_ids:
            payout = rp[m.token_ids.index(tid)]
            pnl = led.settle(tid, payout)
            log.info("settled %s %s -> %.0f (pnl %+.3f)", p.question[:50], p.outcome, payout, pnl)


def _mark_and_stop(led: Ledger, books: dict[str, Book], ex: Executor, now: datetime, stop_drop: float) -> None:
    for tid, p in list(led.positions.items()):
        b = books.get(tid)
        if not b or b.best_bid is None:
            continue
        p.mark = b.best_bid
        if p.kind != "harvest" or stop_drop <= 0:
            continue
        end = parse_iso(p.end_date)
        hrs_left = (end - now).total_seconds() / 3600 if end else 999
        if p.mark < p.avg_price - stop_drop and hrs_left > 1:
            log.warning("stop-loss: %s %s mark %.3f vs entry %.3f", p.question[:50], p.outcome, p.mark, p.avg_price)
            ex.sell(tid, p.size, p.mark, "harvest_stop")


def _prefetch_fees(cands: list[Market], books: dict[str, Book], clob: PublicClob, cfg: Config) -> None:
    for m in cands:
        bs = [books.get(t) for t in m.token_ids]
        if any(b is None or b.best_ask is None for b in bs):
            continue
        pair = bs[0].best_ask + bs[1].best_ask
        near_arb = pair < 1.0 + 0.01
        near_harvest = any(cfg.harvest_min_price <= b.best_ask <= cfg.harvest_max_price for b in bs)
        if near_arb or near_harvest:
            for b in bs:
                b.fee_bps = clob.fee_bps(b.token_id)


def _assess_with_jev(cands: list[Market], books: dict[str, Book], cfg: Config, now: datetime,
                     decider: Optional[JevDecider], led: Ledger, stats: Counter, held: set[str],
                     game_notes: Optional[dict[str, str]] = None, blind: bool = True,
                     no_cache: Optional[set[str]] = None,
                     ) -> tuple[dict[str, Assessment], dict[str, Assessment], dict]:
    """Two assessment sets: favourites WITH prices (gate) and mid-priced markets WITHOUT (edge)."""
    info: dict = {"active": decider is not None}
    if decider is None:
        return {}, {}, info
    cache = AiCache(os.path.join(cfg.state_dir, "ai_cache.json"), cfg.ai_cache_hours)

    # (a) markets we might buy: ask Jev with the prices (and, in sports mode, the game state) visible
    gate_ms: list[Market] = []
    for m in cands:
        h = m.hours_to_end(now)
        if h is None or h <= 0.5 or m.condition_id in held:
            continue
        if game_notes is not None:
            if m.condition_id in game_notes:
                gate_ms.append(m)
            continue
        if h > cfg.harvest_max_hours:
            continue
        if m.liquidity < cfg.harvest_min_liquidity or m.volume24h < cfg.harvest_min_volume24h:
            continue
        asks = [books[t].best_ask for t in m.token_ids if t in books and books[t].best_ask is not None]
        if any(cfg.harvest_min_price <= a <= cfg.harvest_max_price for a in asks):
            gate_ms.append(m)
    gate_ms.sort(key=lambda m: m.hours_to_end(now) or 1e9)

    # (b) liquid markets leaning one way but not settled: ask Jev blind, compare to the price
    edge_ms: list[Market] = []
    if blind:
        for m in cands:
            h = m.hours_to_end(now)
            if h is None or h <= 0.5 or h > cfg.ai_edge_max_hours or m.condition_id in held:
                continue
            if m.liquidity < cfg.ai_edge_min_liquidity or not m.description:
                continue
            asks = [books[t].best_ask for t in m.token_ids if t in books and books[t].best_ask is not None]
            if len(asks) == 2 and cfg.ai_edge_min_price <= max(asks) <= cfg.ai_edge_max_price:
                edge_ms.append(m)
        edge_ms.sort(key=lambda m: -m.volume24h)

    budget = cfg.ai_max_markets_per_run
    gate_ms = gate_ms[: max(0, budget // 2)]
    edge_ms = edge_ms[: max(0, budget - len(gate_ms))]

    def prices_of(m: Market) -> dict[str, float]:
        d = {o: books[t].best_ask for o, t in zip(m.outcomes, m.token_ids) if t in books and books[t].best_ask is not None}
        if game_notes and m.condition_id in game_notes:
            d["_game_state"] = game_notes[m.condition_id]        # carried into Jev's state by market_state()
        return d

    todo: list[tuple[Market, Optional[dict[str, float]]]] = []
    gate: dict[str, Assessment] = {}
    edge: dict[str, Assessment] = {}
    no_cache = no_cache or set()
    for m in gate_ms:
        a = None if m.condition_id in no_cache else cache.get(m.condition_id, True, now)
        if a:
            gate[m.condition_id] = a
        else:
            todo.append((m, prices_of(m)))
    for m in edge_ms:
        a = cache.get(m.condition_id, False, now)
        if a:
            edge[m.condition_id] = a
        else:
            todo.append((m, None))
    fresh = decider.assess_many(todo, now) if todo else {}
    for m, pr in todo:
        a = fresh.get(m.condition_id)
        if not a:
            continue
        if a.error:
            stats["ai.errors"] += 1
            continue
        if m.condition_id not in no_cache:
            cache.put(a)
        (gate if pr is not None else edge)[m.condition_id] = a
    cache.save(now)

    by_cid = {m.condition_id: m for m in cands}
    for cid, a in edge.items():
        m = by_cid[cid]
        yes_price = books[m.token_ids[0]].best_ask if m.token_ids[0] in books else None
        if a.p_yes is not None:
            led.log_ai(cid, m.question, a.p_yes, yes_price, a.risk, False, a.at)

    cal = led.ai_calibration()
    extreme_n = cal[">=0.95"]["n"] + cal["<=0.05"]["n"]
    extreme_hits = sum((cal[k]["acc"] or 0) * cal[k]["n"] for k in (">=0.95", "<=0.05"))
    extreme_acc = extreme_hits / extreme_n if extreme_n else None
    breaker = bool(extreme_n >= cfg.ai_min_samples_for_breaker and extreme_acc is not None
                   and extreme_acc < cfg.ai_breaker_min_accuracy)
    if breaker:
        log.warning("AI circuit breaker ON: extreme-answer accuracy %.2f over %d resolved calls", extreme_acc, extreme_n)
    info.update({"model": cfg.ai_model, "gate_assessed": len(gate), "edge_assessed": len(edge),
                 "calls": decider.calls, "cache_hits": len(gate) + len(edge) - len(todo) + stats["ai.errors"],
                 "errors": stats["ai.errors"], "breaker": breaker,
                 "extreme_resolved": extreme_n, "extreme_accuracy": extreme_acc})
    stats["ai.gate_assessed"] = len(gate)
    stats["ai.edge_assessed"] = len(edge)
    log.info("jev: %d favourites gated, %d markets judged blind, %d API calls, %d errors%s",
             len(gate), len(edge), decider.calls, stats["ai.errors"], " [BREAKER ON]" if breaker else "")
    return gate, edge, info


def _apply_gate(opps: list, gate: dict[str, Assessment], cfg: Config, stats: Counter, led: Optional[Ledger] = None) -> list:
    """Drop sports trades where Jev, shown the game state and prices, leans the other way."""
    out = []
    for o in opps:
        a = gate.get(o.market.condition_id)
        if a and a.p_yes is not None:
            idx = o.market.token_ids.index(o.legs[0].token_id)
            ai_p = a.p_yes if idx == 0 else 1.0 - a.p_yes
            # favourites: Jev must not lean against them (< 0.5); underdogs: Jev must at least see
            # value at the price, since a 30% dog Jev also puts at 30% is not a veto
            if ai_p < min(cfg.ai_gate_min_p, o.legs[0].price):
                stats["sports.ai_vetoed"] += 1
                log.info("  jev veto: %s (%s @ %.2f, jev %.2f)", o.market.question[:50], o.legs[0].outcome, o.legs[0].price, ai_p)
                if led is not None:
                    _decide(led, f"jev veto {o.legs[0].outcome} @ {o.legs[0].price:.3f} (model {o.ai_p or 0:.2f}, jev {ai_p:.2f})")
                continue
            o.note += f", jev {ai_p:.2f}"
        out.append(o)
    return out


def _grade_ai_log(led: Ledger, gamma: Gamma, by_cid: dict[str, Market], now: datetime, limit: int = 25) -> None:
    """Record real outcomes for past blind Jev calls so calibration is measured, not assumed."""
    pending = [cid for cid, e in led.ai_log.items() if e.get("outcome") is None]
    if not pending:
        return
    missing = [c for c in pending if c not in by_cid][:limit]
    found = gamma.markets_by_condition(missing) if missing else {}
    for cid in pending:
        m = by_cid.get(cid) or found.get(cid)
        if not m:
            continue
        rp = m.resolved_prices
        if rp is not None:
            led.resolve_ai(cid, yes_won=(rp[0] == 1.0))
    # forget very old unresolved entries so the log stays bounded
    for cid in list(led.ai_log):
        e = led.ai_log[cid]
        if e.get("outcome") is None and (parse_iso(e.get("at")) or now) < now.replace(year=now.year - 1):
            del led.ai_log[cid]


def _log_near_misses(cands: list[Market], books: dict[str, Book], cfg: Config, now: datetime, n: int = 8) -> None:
    """Show the closest favourites that did not qualify, so thresholds can be tuned from the logs."""
    rows = []
    for m in cands:
        h = m.hours_to_end(now)
        if h is None or h > cfg.harvest_max_hours:
            continue
        for idx in (0, 1):
            b = books.get(m.token_ids[idx])
            if b and b.best_ask is not None and b.best_bid is not None and 0.85 <= b.best_ask <= 0.995:
                rows.append((h, m.question[:55], m.outcomes[idx], b.best_ask, b.best_bid, b.fee_bps, m.liquidity, m.volume24h, m.min_order_size))
    rows.sort()
    for h, q, o, a, bd, fee, liq, vol, mos in rows[:n]:
        log.info("  near: %.1fh | %s | %s ask %.3f bid %.3f fee %d liq %.0f vol24 %.0f min %.0f", h, q, o, a, bd, fee, liq, vol, mos)


def _finish(led: Ledger, path: str, cfg: Config, st: RiskState, done: list, halt: Optional[str]) -> None:
    led.save(path)
    _write_report(led, cfg, st, done, halt)
    log.info("equity $%.2f | cash $%.2f | deployed $%.2f | realized %+.2f | positions %d | trades this run %d",
             led.equity, led.cash, led.deployed, led.realized_pnl, len(led.positions), len(done))


def _write_report(led: Ledger, cfg: Config, st: RiskState, done: list, halt: Optional[str]) -> None:
    lines = [f"# polybot report", "",
             f"- mode: **{led.mode}**  |  profile: **{cfg.profile}**  |  runs: {led.runs}  |  last run: {led.last_run}"
             + (f"  |  wallet `{led.wallet}`" if led.wallet else ""),
             f"- equity: **${led.equity:.2f}** (started ${led.starting_bankroll:.2f}, realized {led.realized_pnl:+.2f})",
             f"- cash: ${led.cash:.2f}  |  deployed: ${led.deployed:.2f}  |  open positions: {len(led.positions)}",
             f"- P&L today: {led.pnl_today():+.2f}" + (f"  |  **HALTED: {halt}**" if halt else ""), ""]
    if done:
        lines += ["## Trades this run", ""] + [f"- {o.kind}: {o.market.question[:80]} — {o.note} (cost ${o.cost:.2f})" for o in done] + [""]
    if led.positions:
        lines += ["## Open positions", "", "| market | side | size | entry | mark | ends | kind |", "|---|---|---|---|---|---|---|"]
        for p in sorted(led.positions.values(), key=lambda p: p.end_date or ""):
            lines.append(f"| {p.question[:60]} | {p.outcome} | {p.size:.1f} | {p.avg_price:.3f} | {p.mark:.3f} | {(p.end_date or '')[:16]} | {p.kind} |")
        lines.append("")
    if led.trades:
        lines += ["## Recent trades", ""] + [
            f"- {t['t']} {t['side']} {t['size']:.1f} {t['outcome']} @ {t['price']:.3f} ({t['kind']}) {t.get('q','')}" +
            (f" pnl {t['pnl']:+.3f}" if 'pnl' in t else "") for t in led.trades[-15:]] + [""]
    ai = led.ai_info or {}
    if ai.get("active"):
        cal = led.ai_calibration()
        lines += ["## Jev (AI decider)", "",
                  f"- model `{ai.get('model')}`: {ai.get('gate_assessed', 0)} favourites gated, {ai.get('edge_assessed', 0)} markets judged blind, "
                  f"{ai.get('calls', 0)} API calls, {ai.get('cache_hits', 0)} cache hits, {ai.get('errors', 0)} errors"
                  + ("  |  **CIRCUIT BREAKER ON** (edge trades paused)" if ai.get("breaker") else ""),
                  "- blind-call accuracy by stated probability (resolved markets only): "
                  + ", ".join(f"{k}: {v['n']} calls" + (f", {v['acc']*100:.0f}% right" if v['acc'] is not None else "") for k, v in cal.items()),
                  ""]
    elif cfg.ai_enabled:
        lines += ["## Jev (AI decider)", "", "- inactive: add the `OPENROUTER_API_KEY` secret to enable it", ""]
    obs = observe.summarize(cfg.state_dir)
    if obs:
        lines += ["## Observation log", "", f"- {obs['snapshots']} quote/model snapshots and {obs['outcomes']} game outcomes across {obs['files']} files in `state/obs/`", ""]
    if led.scan:
        lines += ["## Last scan", "", ", ".join(f"{k}={v}" for k, v in led.scan.items()), ""]
    if led.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in led.notes[-10:]]
    if led.decisions:
        lines += ["", "## Recent decisions", ""] + [f"- {n}" for n in led.decisions[-12:]]
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(os.path.join(cfg.state_dir, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _commit_state(state_dir: str) -> None:
    """Commit and push the state directory from inside a long-running loop (best effort)."""
    import subprocess
    try:
        subprocess.run(["git", "add", state_dir], check=True, capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", f"polybot: state {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ}"],
                       check=True, capture_output=True)
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "pull", "--rebase", "-q", "origin", branch], check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", f"HEAD:{branch}"], check=True, capture_output=True)
        log.info("state committed and pushed")
    except subprocess.CalledProcessError as e:
        log.warning("state commit failed: %s", (e.stderr or b"").decode()[:200])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="polybot trading cycle")
    ap.add_argument("--loop-minutes", type=float, default=0.0,
                    help="keep cycling for this long while matched games are live or about to start")
    ap.add_argument("--interval-seconds", type=float, default=120.0, help="pause between cycles when looping")
    ap.add_argument("--commit-every-minutes", type=float, default=0.0,
                    help="while looping, commit and push state/ this often (0 = never; needs a git checkout with push rights)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config()
    gamma = clob = live = data = decider = us = None
    if cfg.ai_active:
        decider = JevDecider(cfg.openrouter_api_key, cfg.ai_model)
        log.info("AI decider: %s via OpenRouter", cfg.ai_model)
    else:
        log.info("AI decider off (set OPENROUTER_API_KEY to enable Jev)")
    if cfg.sports_only:
        log.info("sports mode: %s moneylines only", ", ".join(cfg.sports_leagues).upper())
    key_problem = ""
    if cfg.exchange == "us":
        us = PolymarketUS(make_client(cfg.us_key_id, cfg.us_secret), cfg.us_key_id,
                          cache_path=os.path.join(cfg.state_dir, "us_markets.json"))
        if cfg.is_live:
            live = us
            log.info("LIVE mode on polymarket.us (%s)", us.funder)
        else:
            log.info("PAPER mode on polymarket.us (set POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET to go live)")
    else:
        gamma = Gamma(cfg.gamma_host)
        clob = PublicClob(cfg.clob_host)
        if cfg.is_live:
            try:
                cfg.private_key = normalize_private_key(cfg.private_key)
            except ValueError as e:
                key_problem = f"POLYMARKET_PRIVATE_KEY rejected: {e}"
                log.error("%s; running in PAPER mode", key_problem)
        if cfg.is_live and not key_problem:
            data = DataApi(cfg.data_host)
            live = resolve_wallet(cfg, data=data)
            log.info("LIVE mode: wallet %s (signature type %d)", live.funder, live.signature_type)
        else:
            log.info("PAPER mode on polymarket.com (set POLYMARKET_PRIVATE_KEY to go live)")
    if key_problem:
        os.environ["POLYBOT_KEY_PROBLEM"] = key_problem

    if os.path.exists(cfg.kill_switch_file):
        # paused by hand: no cycles, no orders of any kind. Open positions settle on the exchange.
        log.warning("PAUSED: %s exists; remove it (or run again without it) to resume trading", cfg.kill_switch_file)
        return 0
    deadline = time.monotonic() + args.loop_minutes * 60
    espn = Espn() if cfg.sports_only else None
    cycles = 0
    last_commit = time.monotonic()
    while True:
        cycles += 1
        try:
            led = run_once(cfg, gamma, clob, live, data, decider=decider, us=us, espn=espn)
        except Exception:
            log.exception("cycle %d failed", cycles)
            if args.loop_minutes <= 0:
                raise
            led = None
        if args.commit_every_minutes > 0 and time.monotonic() - last_commit >= args.commit_every_minutes * 60:
            _commit_state(cfg.state_dir)
            last_commit = time.monotonic()
        remaining = deadline - time.monotonic()
        if args.loop_minutes <= 0 or remaining <= args.interval_seconds:
            break
        if led is not None and not led.game_window:
            log.info("no matched game live or starting within 45 minutes; ending loop after %d cycle(s)", cycles)
            break
        log.info("game window open: next cycle in %.0fs (%.0f min left)", args.interval_seconds, remaining / 60)
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
