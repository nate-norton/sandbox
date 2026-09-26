"""One bot cycle. Designed to be run by cron/CI every ~30 minutes and to be safe to re-run.

    python -m polybot.run          # paper mode unless POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER are set
"""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from .ai_cache import AiCache
from .clob import DataApi, LiveClob, PublicClob
from .config import Config
from .decider import Assessment, JevDecider
from .executor import Executor
from .gamma import Gamma
from .ledger import Ledger, Position
from .models import Book, Market, parse_iso
from .risk import RiskManager, RiskState, state_from_ledger
from .strategies import find_ai_edges, find_harvests, find_negrisk_arbs, find_pair_arbs

log = logging.getLogger("polybot")

def run_once(cfg: Config, gamma: Gamma, clob: PublicClob, live: Optional[LiveClob] = None,
             data: Optional[DataApi] = None, now: Optional[datetime] = None,
             decider: Optional[JevDecider] = None) -> Ledger:
    now = now or datetime.now(timezone.utc)
    mode = "live" if live else "paper"
    path = os.path.join(cfg.state_dir, "ledger.json")
    led = Ledger.load(path, cfg.starting_bankroll, mode)
    led.runs += 1
    led.last_run = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("=== polybot run %d (%s mode, %s profile) ===", led.runs, mode, cfg.profile)

    if live:
        _sync_live(led, live, data, cfg)

    markets = gamma.active_markets(limit=cfg.scan_limit)
    by_cid = {m.condition_id: m for m in markets}

    # 1) settle / mark held positions, and grade past Jev calls against resolved markets
    if not live:
        _settle_paper(led, gamma, by_cid)
    _grade_ai_log(led, gamma, by_cid, now)
    held_tokens = list(led.positions)
    # 2) candidate universe for books: binary markets inside either strategy window, plus holdings
    cands = []
    for m in markets:
        h = m.hours_to_end(now)
        if m.is_binary and h is not None and 0 < h <= cfg.arb_max_days_to_resolution * 24:
            cands.append(m)
    tokens = list(dict.fromkeys([t for m in cands for t in m.token_ids] + held_tokens))
    books = clob.books(tokens, batch=cfg.book_batch)
    log.info("books: %d of %d tokens", len(books), len(tokens))

    ex = Executor(led, books, live)
    _mark_and_stop(led, books, ex, now, cfg.stop_loss_drop)

    # 3) fee lookups only for candidates that pass a cheap price prefilter (keeps request count small)
    _prefetch_fees(cands, books, clob, cfg)

    rm = RiskManager(cfg)
    st = state_from_ledger(led)
    halt = rm.halted(st)
    if halt:
        log.warning("trading halted: %s", halt)
        led.notes.append(f"{led.last_run} halted: {halt}")
        _finish(led, path, cfg, st, [], halt)
        return led

    held_cids = {p.condition_id for p in led.positions.values()}
    stats: Counter = Counter({"markets": len(markets), "candidates": len(cands), "books": len(books)})
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
    log.info("opportunities: %d (spendable $%.2f)", len(opps), rm.spendable(st))
    log.info("scan stats: %s", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    led.scan = dict(sorted(stats.items()))
    _log_near_misses(cands, books, cfg, now)

    done = []
    seen_cids: set[str] = set()
    for o in opps:
        if o.market.condition_id in seen_cids:
            continue
        why = rm.approve(o, st)
        if why:
            log.info("skip %s %s: %s", o.kind, o.market.question[:60], why)
            continue
        log.info("TRADE %s: %s | %s | cost $%.2f, +$%.3f exp", o.kind, o.market.question[:70], o.note, o.cost, o.expected_profit)
        spent = ex.buy(o)
        rm.commit(o, st, spent)
        seen_cids.add(o.market.condition_id)
        if spent > 0:
            done.append(o)
    _finish(led, path, cfg, st, done, None)
    return led


# ----------------------------------------------------------------------------- helpers
def _sync_live(led: Ledger, live: LiveClob, data: Optional[DataApi], cfg: Config) -> None:
    """Live truth comes from the exchange: cash from the CLOB, positions from the data API."""
    try:
        led.cash = live.usdc_balance()
    except Exception as e:
        log.error("balance lookup failed: %s", e)
        raise SystemExit(2)
    if data:
        rows = data.positions(live.funder)
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
        led.positions = pos
    if led.runs == 1 or led.starting_bankroll <= 0:
        led.starting_bankroll = led.equity
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
                     decider: Optional[JevDecider], led: Ledger, stats: Counter, held: set[str]
                     ) -> tuple[dict[str, Assessment], dict[str, Assessment], dict]:
    """Two assessment sets: favourites WITH prices (gate) and mid-priced markets WITHOUT (edge)."""
    info: dict = {"active": decider is not None}
    if decider is None:
        return {}, {}, info
    cache = AiCache(os.path.join(cfg.state_dir, "ai_cache.json"), cfg.ai_cache_hours)

    # (a) favourites the harvest strategy could buy: ask Jev with the prices visible
    gate_ms: list[Market] = []
    for m in cands:
        h = m.hours_to_end(now)
        if h is None or h <= 0.5 or h > cfg.harvest_max_hours or m.condition_id in held:
            continue
        if m.liquidity < cfg.harvest_min_liquidity or m.volume24h < cfg.harvest_min_volume24h:
            continue
        asks = [books[t].best_ask for t in m.token_ids if t in books and books[t].best_ask is not None]
        if any(cfg.harvest_min_price <= a <= cfg.harvest_max_price for a in asks):
            gate_ms.append(m)
    gate_ms.sort(key=lambda m: m.hours_to_end(now) or 1e9)

    # (b) liquid markets leaning one way but not settled: ask Jev blind, compare to the price
    edge_ms: list[Market] = []
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
        return {o: books[t].best_ask for o, t in zip(m.outcomes, m.token_ids) if t in books and books[t].best_ask is not None}

    todo: list[tuple[Market, Optional[dict[str, float]]]] = []
    gate: dict[str, Assessment] = {}
    edge: dict[str, Assessment] = {}
    for m in gate_ms:
        a = cache.get(m.condition_id, True, now)
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
             f"- mode: **{led.mode}**  |  profile: **{cfg.profile}**  |  runs: {led.runs}  |  last run: {led.last_run}",
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
    if led.scan:
        lines += ["## Last scan", "", ", ".join(f"{k}={v}" for k, v in led.scan.items()), ""]
    if led.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in led.notes[-10:]]
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(os.path.join(cfg.state_dir, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    cfg = Config()
    gamma = Gamma(cfg.gamma_host)
    clob = PublicClob(cfg.clob_host)
    live = data = decider = None
    if cfg.ai_active:
        decider = JevDecider(cfg.openrouter_api_key, cfg.ai_model)
        log.info("AI decider: %s via OpenRouter", cfg.ai_model)
    else:
        log.info("AI decider off (set OPENROUTER_API_KEY to enable Jev)")
    if cfg.is_live:
        live = LiveClob(cfg.clob_host, cfg.chain_id, cfg.private_key, cfg.funder, cfg.signature_type)
        data = DataApi(cfg.data_host)
        log.info("LIVE mode: funder %s…%s", cfg.funder[:6], cfg.funder[-4:])
    else:
        log.info("PAPER mode (set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER to go live)")
    run_once(cfg, gamma, clob, live, data, decider=decider)
    return 0


if __name__ == "__main__":
    sys.exit(main())
