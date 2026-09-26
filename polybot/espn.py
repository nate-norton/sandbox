"""Free live football data from ESPN's public scoreboard API (no key required).

For each NFL / FBS college game we read: teams, start time, state (pre/in/post), score,
period and clock, ESPN BET's pre-game moneyline (turned into a no-vig probability), and,
during a game, ESPN's live win probability. This is the model the sports strategy trades
against; Polymarket's price is compared to it.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from .models import parse_iso

log = logging.getLogger(__name__)

BASE = "https://site.api.espn.com/apis/site/v2/sports/football"
LEAGUE_PATH = {"nfl": "nfl", "cfb": "college-football"}


def _norm(s: str) -> str:
    s = s.lower().replace("&", "and").replace("st.", "state").replace("st ", "state ")
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


@dataclass
class Team:
    names: set[str]                 # normalised aliases: "ohio state", "buckeyes", "osu", ...
    display: str
    score: Optional[int] = None

    def matches(self, label: str) -> bool:
        n = _norm(label)
        return n in self.names


@dataclass
class Game:
    id: str
    league: str
    start: Optional[datetime]
    state: str                      # pre | in | post
    completed: bool
    home: Team
    away: Team
    period: int = 0
    clock: str = ""
    home_wp: Optional[float] = None         # live win probability for home (0..1)
    ml_home: Optional[int] = None           # American odds
    ml_away: Optional[int] = None
    spread: Optional[float] = None
    over_under: Optional[float] = None
    winner_home: Optional[bool] = None
    raw_name: str = ""

    def team_for(self, label: str) -> Optional[str]:
        """'home' / 'away' if the label names one of the teams."""
        if self.home.matches(label):
            return "home"
        if self.away.matches(label):
            return "away"
        return None

    def pregame_p_home(self) -> Optional[float]:
        if self.ml_home is None or self.ml_away is None:
            return None
        ih, ia = _implied(self.ml_home), _implied(self.ml_away)
        if ih is None or ia is None or ih + ia <= 0:
            return None
        return ih / (ih + ia)

    def model_p(self, side: str) -> tuple[Optional[float], str]:
        """Model probability that `side` ('home'/'away') wins, and where it came from."""
        p, src = None, ""
        if self.completed and self.winner_home is not None:
            p, src = (1.0 if self.winner_home else 0.0), "final"
        elif self.state == "in" and self.home_wp is not None:
            p, src = self.home_wp, "live_wp"
        elif self.state == "pre":
            p, src = self.pregame_p_home(), "moneyline"
        if p is None:
            return None, src
        return (p if side == "home" else 1.0 - p), src

    @property
    def summary(self) -> str:
        sc = f"{self.away.display} {self.away.score if self.away.score is not None else '-'} @ " \
             f"{self.home.display} {self.home.score if self.home.score is not None else '-'}"
        if self.state == "in":
            return f"{sc}, Q{self.period} {self.clock}, home wp {self.home_wp}"
        if self.state == "post":
            return f"{sc}, final" if self.completed else f"{sc}, ended (not final)"
        return f"{sc}, starts {self.start.isoformat() if self.start else '?'}, ML {self.ml_away}/{self.ml_home}"


def _implied(american: Optional[int]) -> Optional[float]:
    if american is None:
        return None
    a = float(american)
    if a > 0:
        return 100.0 / (a + 100.0)
    if a < 0:
        return -a / (-a + 100.0)
    return None


def _team(c: dict) -> Team:
    t = c.get("team") or {}
    names = set()
    for k in ("displayName", "shortDisplayName", "name", "location", "abbreviation", "nickname"):
        v = t.get(k)
        if v:
            names.add(_norm(str(v)))
    loc, nick = t.get("location"), t.get("name")
    if loc and nick:
        names.add(_norm(f"{loc} {nick}"))
    score = c.get("score")
    try:
        score_i = int(float(score)) if score not in (None, "") else None
    except (TypeError, ValueError):
        score_i = None
    return Team(names=names, display=str(t.get("shortDisplayName") or t.get("displayName") or "?"), score=score_i)


def parse_event(ev: dict, league: str) -> Optional[Game]:
    comps = ev.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    home = away = None
    winner_home = None
    for c in comp.get("competitors") or []:
        t = _team(c)
        if c.get("homeAway") == "home":
            home = t
            if c.get("winner") is True:
                winner_home = True
        elif c.get("homeAway") == "away":
            away = t
            if c.get("winner") is True:
                winner_home = False
    if not home or not away:
        return None
    st = (ev.get("status") or comp.get("status") or {})
    stype = st.get("type") or {}
    state = str(stype.get("state") or "pre")
    completed = bool(stype.get("completed"))
    g = Game(id=str(ev.get("id")), league=league, start=parse_iso(ev.get("date") or comp.get("date")),
             state=state, completed=completed, home=home, away=away,
             period=int(st.get("period") or 0), clock=str(st.get("displayClock") or ""),
             winner_home=winner_home if completed else None, raw_name=str(ev.get("name") or ""))
    odds = comp.get("odds") or []
    if odds:
        o = odds[0]
        ho, ao = o.get("homeTeamOdds") or {}, o.get("awayTeamOdds") or {}
        # older shape: homeTeamOdds.moneyLine; current shape: moneyline.home.{current|close|open}.odds
        g.ml_home = _int_or_none(ho.get("moneyLine"))
        g.ml_away = _int_or_none(ao.get("moneyLine"))
        ml = o.get("moneyline") or {}
        if g.ml_home is None:
            g.ml_home = _line_odds(ml.get("home"))
        if g.ml_away is None:
            g.ml_away = _line_odds(ml.get("away"))
        g.spread = _float_or_none(o.get("spread"))
        g.over_under = _float_or_none(o.get("overUnder"))
    sit = comp.get("situation") or {}
    prob = ((sit.get("lastPlay") or {}).get("probability")) or {}
    wp = prob.get("homeWinPercentage")
    if wp is None:
        wp = sit.get("homeWinPercentage")
    g.home_wp = _prob_or_none(wp)
    return g


_SAMPLED: set[str] = set()


def _debug_sample(ev: dict, g: Game) -> None:
    """Log the raw odds/situation objects once per game state so parser gaps are visible in CI logs."""
    key = g.state
    if key in _SAMPLED:
        return
    _SAMPLED.add(key)
    comp = (ev.get("competitions") or [{}])[0]
    raw = {"odds": comp.get("odds"), "situation": comp.get("situation"), "status": ev.get("status")}
    log.info("espn raw sample (%s, %s): %s", g.state, g.raw_name, json.dumps(raw, default=str)[:1500])


def _line_odds(side: Optional[dict]) -> Optional[int]:
    """American odds from ESPN's {current|close|open: {odds: "-345"}} structure."""
    if not isinstance(side, dict):
        return None
    for k in ("current", "close", "open"):
        v = (side.get(k) or {}).get("odds") if isinstance(side.get(k), dict) else None
        if v not in (None, "", "EVEN", "even"):
            return _int_or_none(str(v).replace("+", ""))
        if v in ("EVEN", "even"):
            return 100
    return None


def _int_or_none(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _prob_or_none(v):
    f = _float_or_none(v)
    if f is None:
        return None
    if f > 1.0:
        f /= 100.0
    return max(0.0, min(1.0, f))


class Espn:
    def __init__(self, session: requests.Session | None = None, timeout: float = 20.0):
        self.s = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, **params) -> dict:
        r = self.s.get(f"{BASE}{path}", params=params, timeout=self.timeout,
                       headers={"User-Agent": "polybot/0.1 (+github.com/nate-norton/sandbox)"})
        r.raise_for_status()
        return r.json()

    def games(self, league: str, now: datetime, days_ahead: int = 7) -> list[Game]:
        """One request per calendar day (ESPN's scoreboard rejects date ranges for football)."""
        path = f"/{LEAGUE_PATH[league]}/scoreboard"
        out: list[Game] = []
        seen: set[str] = set()
        failures = 0
        for d in range(-1, days_ahead + 1):
            params: dict = {"dates": (now + timedelta(days=d)).strftime("%Y%m%d")}
            if league == "cfb":
                params["groups"] = 80                   # FBS
                params["limit"] = 300
            try:
                data = self._get(path, **params)
            except (requests.RequestException, ValueError) as e:
                failures += 1
                log.warning("espn %s scoreboard %s failed: %s", league, params["dates"], e)
                if failures >= 3:
                    break
                continue
            for ev in data.get("events") or []:
                g = parse_event(ev, league)
                if g and g.id not in seen:
                    seen.add(g.id)
                    out.append(g)
                    _debug_sample(ev, g)
        log.info("espn %s: %d games (%d live, %d pre, %d final)%s", league, len(out),
                 sum(g.state == "in" for g in out), sum(g.state == "pre" for g in out), sum(g.completed for g in out),
                 f"; e.g. {out[0].summary}" if out else "")
        return out

    def live_wp(self, game: Game) -> Optional[float]:
        """Fallback when the scoreboard carries no live probability: read the game summary."""
        try:
            data = self._get(f"/{LEAGUE_PATH[game.league]}/summary", event=game.id)
        except (requests.RequestException, ValueError) as e:
            log.warning("espn summary failed for %s: %s", game.id, e)
            return None
        wps = data.get("winprobability") or []
        if wps:
            return _prob_or_none(wps[-1].get("homeWinPercentage"))
        return None
