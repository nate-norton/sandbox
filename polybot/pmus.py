"""Polymarket US (polymarket.us) exchange adapter.

Polymarket US is the CFTC-regulated venue: USD accounts, Ed25519 API keys from polymarket.us/developer,
and one market per outcome (e.g. a "Texans" market you go LONG on). This adapter presents each football
game as one binary Market (outcomes = the two teams, token ids = "us:<slug>" for each team's market)
so the ESPN sports strategy runs unchanged. Public data needs no key; trading does.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Book, Leg, Market, parse_iso

log = logging.getLogger(__name__)

LEAGUE_ALIASES = {
    "nfl": {"nfl"},
    "cfb": {"ncaaf", "cfb", "college football", "ncaa football", "ncaa-football", "college-football"},
}
NON_MONEYLINE = ("spread", "o/u", "over", "under", "total", "points", "1h", "first half", "1st half", "quarter", "prop")


def token_id(slug: str) -> str:
    return f"us:{slug}"


def slug_of(token: str) -> str:
    return token[3:] if token.startswith("us:") else token


def _amount(a) -> Optional[float]:
    if isinstance(a, dict):
        a = a.get("value")
    try:
        return float(a) if a not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _league_key(team: dict) -> Optional[str]:
    lg = str((team or {}).get("league") or "").strip().lower()
    for key, names in LEAGUE_ALIASES.items():
        if lg in names:
            return key
    return None


def _event_league(ev: dict) -> Optional[str]:
    """League from the event slug tokens or its tags: 'nfl', or 'cfb' for college football."""
    texts = [str(ev.get("slug") or "").lower()]
    for t in ev.get("tags") or []:
        texts.append(str(t.get("slug") or "").lower())
        texts.append(str(t.get("label") or "").lower())
    series = ev.get("series") or {}
    texts.append(str(series.get("slug") or "").lower())
    tokens = set()
    for t in texts:
        tokens.update(x for x in t.replace("_", "-").split("-") if x)
        tokens.add(t)
    if "nfl" in tokens:
        return "nfl"
    if tokens & {"cfb", "ncaaf", "ncaa-football", "college-football", "college football", "ncaa football"}:
        return "cfb"
    return None


def _is_moneyline(m: dict) -> bool:
    t = f"{m.get('title', '')} {m.get('outcome', '')}".lower()
    return not any(x in t for x in NON_MONEYLINE)


class PolymarketUS:
    """Thin wrapper over the official SDK. `client` is a polymarket_us.PolymarketUS (sync)."""

    def __init__(self, client, key_id: str = ""):
        self.c = client
        self.key_id = key_id
        self.funder = f"polymarket.us key {key_id[:8]}…" if key_id else "polymarket.us (public)"
        self.signature_type = -1
        self._market_meta: dict[str, dict] = {}       # slug -> raw market
        self._event_cache: dict[str, Optional[datetime]] = {}

    # ------------------------------------------------------------------ discovery
    def football_markets(self, leagues: tuple = ("nfl", "cfb"), page: int = 100, max_pages: int = 40,
                         days_ahead: int = 8) -> list[Market]:
        """One binary Market per game: the two team markets of a moneyline event."""
        now = datetime.now(timezone.utc)
        events: list[dict] = []
        offset = 0
        params_base = {"active": True, "closed": False, "limit": page,
                       "startTimeMax": (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for _ in range(max_pages):
            try:
                res = self.c.events.list({**params_base, "offset": offset})
            except Exception as e:
                log.warning("polymarket.us events.list failed at offset %d: %s", offset, e)
                if offset == 0 and "startTimeMax" in params_base:      # unsupported filter? retry without it
                    params_base.pop("startTimeMax")
                    continue
                break
            batch = res.get("events") or []
            events += batch
            offset += len(batch)
            if len(batch) < page:
                break
        out: list[Market] = []
        samples = 0
        for ev in events:
            lg = _event_league(ev)
            if lg is None or lg not in leagues:
                continue
            ms = [m for m in ev.get("markets") or [] if _is_moneyline(m)]
            if samples < 2:
                samples += 1
                log.info("polymarket.us football event sample: %s", {k: ev.get(k) for k in ("slug", "title", "startTime", "tags")}
                         | {"markets": [(m.get("slug"), m.get("title"), m.get("outcome")) for m in (ev.get("markets") or [])][:6]})
            if len(ms) != 2:
                continue
            a, b = ms
            for m in (a, b):
                self._market_meta[m["slug"]] = {**m, "eventSlug": ev.get("slug")}
            start = parse_iso(ev.get("startTime")) or parse_iso(ev.get("endTime"))
            out.append(Market(
                condition_id=str(ev.get("slug")),
                question=str(ev.get("title") or ev.get("slug")),
                token_ids=[token_id(a["slug"]), token_id(b["slug"])],
                outcomes=[_team_label(a), _team_label(b)],
                outcome_prices=[],
                end_date=parse_iso(ev.get("endTime")) or start,
                neg_risk=False,
                liquidity=float(ev.get("liquidity") or 0),
                volume24h=float(ev.get("volume") or 0),
                spread=0.0, min_order_size=1.0, tick_size=0.01,
                accepting_orders=bool(a.get("active", True)) and bool(b.get("active", True)),
                closed=bool(ev.get("closed") or a.get("closed") or b.get("closed")),
                event_id=str(ev.get("slug")), event_title=str(ev.get("title") or ""), slug=a["slug"],
                description=str(ev.get("description") or "")[:2000], event_slug=str(ev.get("slug")),
                sports_type="moneyline", game_start=start,
            ))
        log.info("polymarket.us: %d football games from %d events", len(out), len(events))
        if events and not out:
            ev = events[0]
            log.info("polymarket.us sample event: %s", {k: ev.get(k) for k in ("slug", "title", "startTime", "tags")}
                     | {"markets": [(m.get("slug"), m.get("title")) for m in (ev.get("markets") or [])][:4]})
        return out

    def _event_start(self, ev_slug: str, m: dict) -> Optional[datetime]:
        if ev_slug in self._event_cache:
            return self._event_cache[ev_slug]
        start = None
        try:
            ev = (self.c.events.retrieve_by_slug(ev_slug) or {}).get("event") or {}
            start = parse_iso(ev.get("startTime")) or parse_iso(ev.get("endTime"))
        except Exception as e:
            log.debug("event lookup failed for %s: %s", ev_slug, e)
        self._event_cache[ev_slug] = start
        return start

    # ------------------------------------------------------------------ market data
    def books(self, token_ids: list[str], batch: int = 40) -> dict[str, Book]:
        out: dict[str, Book] = {}
        for tok in token_ids:
            slug = slug_of(tok)
            try:
                md = (self.c.markets.book(slug) or {}).get("marketData") or {}
            except Exception as e:
                log.warning("book failed for %s: %s", slug, e)
                continue
            bids = [{"price": _amount(l.get("px")), "size": float(l.get("qty") or 0)} for l in md.get("bids") or []]
            asks = [{"price": _amount(l.get("px")), "size": float(l.get("qty") or 0)} for l in md.get("offers") or []]
            b = Book.from_clob({"asset_id": tok, "bids": [x for x in bids if x["price"] is not None],
                                "asks": [x for x in asks if x["price"] is not None]}, fee_bps=self.fee_bps(tok))
            if md.get("state") not in (None, "MARKET_STATE_OPEN"):
                b.asks, b.bids = [], []               # not tradeable right now
            out[tok] = b
        return out

    def fee_bps(self, token: str) -> int:
        """Screening estimate; the exact commission comes from the order preview at execution time."""
        return 500

    def resolved(self, condition_ids: set[str]) -> dict[str, list[float]]:
        """event slug -> [payout per outcome] for games whose markets have settled."""
        out = {}
        for ev in condition_ids:
            slugs = [s for s, m in self._market_meta.items() if m.get("eventSlug") == ev]
            if len(slugs) != 2:
                continue
            pays = []
            for s in slugs:
                try:
                    st = self.c.markets.settlement(s) or {}
                    pays.append(float(st.get("settlement")))
                except Exception:
                    pays = []
                    break
            if len(pays) == 2 and all(p in (0.0, 1.0) for p in pays):
                out[ev] = pays
        return out

    # ------------------------------------------------------------------ account (authenticated)
    def usdc_balance(self) -> float:
        res = self.c.account.balances() or {}
        bals = res.get("balances") or []
        if not bals:
            return 0.0
        b = bals[0]
        bp = b.get("buyingPower")
        return float(bp if bp is not None else b.get("currentBalance") or 0.0)

    def positions(self) -> list[dict]:
        """Rows shaped like the global data-api so the ledger sync is shared."""
        rows = []
        try:
            res = self.c.portfolio.positions({"limit": 200}) or {}
        except Exception as e:
            log.warning("positions failed: %s", e)
            return rows
        for slug, p in (res.get("positions") or {}).items():
            net = float(p.get("netPosition") or 0)
            if net <= 0:
                continue
            cost = _amount(p.get("cost")) or 0.0
            meta = p.get("marketMetadata") or {}
            cash = _amount(p.get("cashValue"))
            rows.append({"asset": token_id(slug), "size": net, "avgPrice": cost / net if net else 0.0,
                         "curPrice": (cash / net) if (cash is not None and net) else 0.0,
                         "conditionId": meta.get("eventSlug") or slug, "title": meta.get("title") or slug,
                         "outcome": meta.get("outcome") or "", "endDate": None, "redeemable": False})
        return rows

    def open_orders(self) -> list[dict]:
        return (self.c.orders.list() or {}).get("orders") or []

    def cancel_all(self) -> None:
        self.c.orders.cancel_all()

    def preview_fee(self, slug: str, intent: str, price: float, qty: int) -> Optional[float]:
        try:
            res = self.c.orders.preview({"request": _order_params(slug, intent, price, qty)}) or {}
            o = res.get("order") or {}
            fee = _amount(o.get("commissionNotionalTotalCollected"))
            if fee is None and o.get("commissionsBasisPoints"):
                fee = float(o["commissionsBasisPoints"]) / 10_000 * price * qty
            return fee
        except Exception as e:
            log.info("preview failed for %s: %s", slug, e)
            return None

    def fill_or_kill(self, leg: Leg, tick_size=None, neg_risk=None) -> tuple[float, dict]:
        slug = slug_of(leg.token_id)
        intent = "ORDER_INTENT_BUY_LONG" if leg.side.upper() == "BUY" else "ORDER_INTENT_SELL_LONG"
        qty = int(leg.size)
        if qty <= 0:
            return 0.0, {"errorMsg": "zero quantity"}
        params = _order_params(slug, intent, leg.price, qty)
        params["tif"] = "TIME_IN_FORCE_FILL_OR_KILL"
        params["synchronousExecution"] = True
        params["manualOrderIndicator"] = "MANUAL_ORDER_INDICATOR_AUTOMATIC"
        res = self.c.orders.create(params) or {}
        filled = 0.0
        fee = 0.0
        for ex in res.get("executions") or []:
            if ex.get("type") in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"):
                filled += float(ex.get("lastShares") or 0)
                fee += _amount(ex.get("commissionNotionalCollected")) or 0.0
        raw = {"orderID": res.get("id"), "status": "matched" if filled > 0 else "unmatched",
               "success": True, "fee": round(fee, 4)}
        return min(filled, leg.size), raw


def _order_params(slug: str, intent: str, price: float, qty: int) -> dict:
    return {"marketSlug": slug, "intent": intent, "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.2f}", "currency": "USD"}, "quantity": int(qty),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL"}


def _team_label(m: dict) -> str:
    t = m.get("team") or {}
    return str(t.get("name") or m.get("title") or m.get("outcome") or "?")


def make_client(key_id: str = "", secret: str = ""):
    from polymarket_us import PolymarketUS as SDK
    if key_id and secret:
        return SDK(key_id=key_id, secret_key=secret)
    return SDK()
