"""Jev (TypeSafe's System One decision model) via OpenRouter's Decisions API.

Jev does not generate text. It takes `state` plus typed questions and returns probabilities:

    POST https://openrouter.ai/api/alpha/decisions
    {"model": "~typesafe/jev-latest", "state": {...},
     "questions": {"k": {"type": "noul", "instructions": "...", "criteria": {"true": "...", "false": "..."}}}}
    -> {"k": {"type": "noul", "noul": 0.98}}            (answers keyed as asked, possibly under "answers")

Question types: noul (yes/no -> P(yes)), choice (P per label + confidence), score (ordered scale).
Independent calibration tests (AnthusAI/Jev-Calibration) found answers above 0.95 highly reliable
and mid-range answers close to coin flips, so callers only act on extreme probabilities and every
answer is logged against the real outcome so the thresholds can be checked from data.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

import requests

from .models import Market

log = logging.getLogger(__name__)

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

RISK_SCALE = [
    "The resolution rules are clear and, given the state, the likely outcome is essentially already determined",
    "The resolution rules are clear but the outcome is still genuinely uncertain",
    "The rules are ambiguous, depend on a contested source, or resolution could plausibly be disputed or delayed",
]


@dataclass
class Assessment:
    condition_id: str
    p_yes: Optional[float]          # Jev's probability that outcome[0] (usually "Yes") occurs
    risk: Optional[float]           # 0..2 on RISK_SCALE (probability-weighted position)
    risk_confidence: Optional[float]
    with_prices: bool               # whether the market price was in the state Jev saw
    at: str                         # ISO timestamp
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Assessment":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


def market_state(m: Market, now: datetime, prices: Optional[dict[str, float]]) -> dict:
    hrs = m.hours_to_end(now)
    st = {
        "question": m.question,
        "event": m.event_title,
        "outcomes": m.outcomes,
        "resolution_rules": m.description,
        "market_end_date_utc": m.end_date.isoformat() if m.end_date else None,
        "current_time_utc": now.isoformat(),
        "hours_until_end": round(hrs, 1) if hrs is not None else None,
    }
    if prices:
        st["current_market_prices"] = {k: round(v, 3) for k, v in prices.items()}
        st["note"] = "Prices are what traders currently pay per share that pays $1 if that outcome occurs."
    return st


def questions_for(m: Market) -> dict:
    yes = m.outcomes[0] if m.outcomes else "Yes"
    no = m.outcomes[1] if len(m.outcomes) > 1 else "No"
    return {
        "p_yes": {
            "type": "noul",
            "instructions": f"Will this market resolve to '{yes}'? Judge from the question, the resolution rules "
                            f"and the state. Answer true if '{yes}' will be the resolved outcome, false if '{no}' will.",
            "criteria": {"true": f"The market resolves '{yes}'", "false": f"The market resolves '{no}'"},
        },
        "risk": {
            "type": "score",
            "instructions": "How clear and settled is the resolution of this market, given its rules and the state?",
            "criteria": RISK_SCALE,
        },
    }


class JevDecider:
    def __init__(self, api_key: str, model: str = "~typesafe/jev-latest", timeout: float = 20.0,
                 session: Optional[requests.Session] = None, workers: int = 6):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.s = session or requests.Session()
        self.workers = workers
        self.calls = 0
        self.failures = 0

    # ------------------------------------------------------------------ transport
    def decide(self, state: dict | str, questions: dict) -> dict:
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://github.com/nate-norton/sandbox", "X-Title": "polybot"}
        last = None
        for attempt in range(3):
            try:
                r = self.s.post(DECISIONS_URL, json=body, headers=headers, timeout=self.timeout)
                self.calls += 1
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"http {r.status_code}: {r.text[:200]}"
                    continue
                r.raise_for_status()
                return _answers(r.json(), questions)
            except (requests.RequestException, ValueError) as e:
                last = str(e)[:200]
        self.failures += 1
        raise RuntimeError(last or "decision failed")

    # ------------------------------------------------------------------ market assessment
    def assess(self, m: Market, now: datetime, prices: Optional[dict[str, float]]) -> Assessment:
        at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            ans = self.decide(market_state(m, now, prices), questions_for(m))
        except Exception as e:
            return Assessment(m.condition_id, None, None, None, prices is not None, at, error=str(e)[:200])
        p = _noul(ans.get("p_yes"))
        risk, conf = _score(ans.get("risk"))
        return Assessment(m.condition_id, p, risk, conf, prices is not None, at)

    def assess_many(self, items: list[tuple[Market, Optional[dict[str, float]]]], now: datetime) -> dict[str, Assessment]:
        out: dict[str, Assessment] = {}
        if not items:
            return out
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for a in pool.map(lambda it: self.assess(it[0], now, it[1]), items):
                out[a.condition_id] = a
        return out


# ---------------------------------------------------------------------- response parsing
def _answers(resp: dict, questions: dict) -> dict:
    """The API keys answers as asked; tolerate a wrapper object around them."""
    if not isinstance(resp, dict):
        raise ValueError(f"unexpected response: {str(resp)[:200]}")
    if all(k in resp for k in questions):
        return resp
    for wrap in ("answers", "decisions", "results", "data", "output"):
        inner = resp.get(wrap)
        if isinstance(inner, dict) and all(k in inner for k in questions):
            return inner
    if resp.get("error"):
        raise ValueError(f"api error: {json.dumps(resp['error'])[:200]}")
    raise ValueError(f"answers missing: {json.dumps(resp)[:200]}")


def _noul(a) -> Optional[float]:
    if not isinstance(a, dict):
        return None
    for k in ("noul", "probability", "p", "value"):
        v = a.get(k)
        if isinstance(v, (int, float)):
            return max(0.0, min(1.0, float(v)))
    if isinstance(a.get("probabilities"), dict):
        v = a["probabilities"].get("true")
        if isinstance(v, (int, float)):
            return max(0.0, min(1.0, float(v)))
    return None


def _score(a) -> tuple[Optional[float], Optional[float]]:
    if not isinstance(a, dict):
        return None, None
    s = a.get("score")
    if not isinstance(s, (int, float)) and isinstance(a.get("probabilities"), dict):
        try:
            s = sum(int(k) * float(v) for k, v in a["probabilities"].items())
        except (TypeError, ValueError):
            s = None
    c = a.get("confidence")
    return (float(s) if isinstance(s, (int, float)) else None,
            float(c) if isinstance(c, (int, float)) else None)
