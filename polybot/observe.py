"""Append-only observation log: what the model and the market said for every game side, every cycle.

This is the dataset that self-calibration needs: not just the trades we took, but every quote we
saw next to the model's number, plus each game's final outcome. Daily JSON-lines files under
state/obs/ are committed by the live workflow, so the history lives in git.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from typing import Iterable

from .models import Book
from .sports import Matched, _margin


def snapshot_rows(matched: list[Matched], books: dict[str, Book], cfg, now: datetime) -> list[dict]:
    rows = []
    t = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for mm in matched:
        m, g = mm.market, mm.game
        for idx, side in enumerate(mm.sides):
            p, src = g.model_p(side)
            b = books.get(m.token_ids[idx])
            if p is None or not b or (b.best_ask is None and b.best_bid is None):
                continue
            rows.append({
                "t": t, "event": m.condition_id, "game": g.id, "league": g.league, "state": g.state,
                "period": g.period, "clock": g.clock, "stable": g.stable,
                "side": side, "team": m.outcomes[idx], "token": m.token_ids[idx],
                "p": round(p, 4), "src": src, "ask": b.best_ask, "bid": b.best_bid,
                "ask_qty": round(b.asks[0].size, 1) if b.asks else 0.0,
                "margin": round(_margin(cfg, g, p, src), 4),
                "home_score": g.home.score, "away_score": g.away.score,
            })
    return rows


def outcome_rows(matched: list[Matched], already: set[str], now: datetime) -> list[dict]:
    rows = []
    t = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for mm in matched:
        g = mm.game
        if g.completed and g.winner_home is not None and mm.market.condition_id not in already:
            rows.append({"t": t, "event": mm.market.condition_id, "game": g.id, "league": g.league,
                         "winner_home": g.winner_home, "home": g.home.display, "away": g.away.display,
                         "home_score": g.home.score, "away_score": g.away.score,
                         "sides": mm.sides, "teams": mm.market.outcomes})
    return rows


def append(state_dir: str, name: str, rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    d = os.path.join(state_dir, "obs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "a") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
    return len(rows)


def summarize(state_dir: str) -> Counter:
    """Cheap summary for the report: how many snapshot rows and outcomes are on disk."""
    c: Counter = Counter()
    d = os.path.join(state_dir, "obs")
    if not os.path.isdir(d):
        return c
    for fn in os.listdir(d):
        path = os.path.join(d, fn)
        try:
            with open(path) as f:
                n = sum(1 for _ in f)
        except OSError:
            continue
        c["outcomes" if fn.startswith("outcomes") else "snapshots"] += n
        c["files"] += 1
    return c
