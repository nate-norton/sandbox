"""Polymarket US (polymarket.us) exchange adapter.

Polymarket US is the CFTC-regulated venue: USD accounts, Ed25519 API keys from polymarket.us/developer.
Each football game has ONE full-game market, `aec-<event slug>` (sportsMarketType
football_team_full_game_winner): its LONG side is one team, its SHORT side the other, and a tie
settles at $0.50. This adapter presents that market as a binary Market whose token ids are
"us:<slug>:L" and "us:<slug>:S", so the ESPN sports strategy runs unchanged. Public data needs
no key; trading does.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Book, Leg, Market, parse_iso

log = logging.getLogger(__name__)

INTENT = {("L", "BUY"): "ORDER_INTENT_BUY_LONG", ("L", "SELL"): "ORDER_INTENT_SELL_LONG",
          ("S", "BUY"): "ORDER_INTENT_BUY_SHORT", ("S", "SELL"): "ORDER_INTENT_SELL_SHORT"}


def token_id(slug: str, side: str) -> str:
    return f"us:{slug}:{side}"


def split_token(token: str) -> tuple[str, str]:
    """'us:<slug>:L' -> (slug, 'L'). Tolerates the older 'us:<slug>' form (long)."""
    body = token[3:] if token.startswith("us:") else token
    if body.endswith(":L") or body.endswith(":S"):
        return body[:-2], body[-1]
    return body, "L"


def intent_for(token: str, action: str) -> str:
    return INTENT[(split_token(token)[1], action.upper())]


def _amount(a) -> Optional[float]:
    if isinstance(a, dict):
        a = a.get("value")
    try:
        return float(a) if a not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _split_title(title: str) -> list[str]:
    parts = re.split(r"\s+(?:vs\.?|@|at)\s+", title or "", maxsplit=1, flags=re.I)
    return [p.strip() for p in parts] if len(parts) == 2 else []


def _strip_abbr(name: str) -> str:
    """'KC Chiefs' -> 'Chiefs' (the venue prefixes NFL titles with the abbreviation)."""
    toks = name.split()
    return " ".join(toks[1:]) if len(toks) > 1 and toks[0].isupper() and len(toks[0]) <= 4 else name


class PolymarketUS:
    """Thin wrapper over the official SDK. `client` is a polymarket_us.PolymarketUS (sync)."""

    DEFAULT_TAGS = {"nfl": "nfl", "cfb": "cfb"}
    CFB_TAG_CANDIDATES = ("cfb", "ncaaf", "college-football", "ncaa-football")

    def __init__(self, client, key_id: str = "", cache_path: str = ""):
        self.c = client
        self.key_id = key_id
        self.funder = f"polymarket.us key {key_id[:8]}…" if key_id else "polymarket.us (public)"
        self.signature_type = -1
        self._market_meta: dict[str, dict] = {}       # aec slug -> {event, sides, tick}
        self._cache_path = cache_path
        self._sides_cache: dict[str, dict] = self._load_cache()
        self.book_delay = 0.15                          # seconds between order-book requests (venue rate limit)

    # ------------------------------------------------------------------ discovery
    def football_markets(self, leagues: tuple = ("nfl", "cfb"), page: int = 100, max_pages: int = 20,
                         days_ahead: int = 8) -> list[Market]:
        now = datetime.now(timezone.utc)
        out: list[Market] = []
        n_events = 0
        for lg in leagues:
            tags = [self.DEFAULT_TAGS.get(lg)] if lg != "cfb" else list(self.CFB_TAG_CANDIDATES)
            events: list[dict] = []
            for tag in [t for t in tags if t]:
                events = self._events_for_tag(tag, now, page, max_pages, days_ahead)
                if events:
                    break
            n_events += len(events)
            for ev in events:
                aec = [m for m in ev.get("markets") or [] if str(m.get("slug") or "").startswith("aec-")]
                if len(aec) != 1:
                    continue
                m = self._describe(aec[0]["slug"], ev)
                if m:
                    out.append(m)
        self._save_cache()
        log.info("polymarket.us: %d football games from %d league events", len(out), n_events)
        if out:
            m = out[0]
            log.info("polymarket.us game sample: %s | long=%s short=%s aliases=%s", m.question, m.outcomes[0], m.outcomes[1], m.outcome_aliases)
        return out

    def _describe(self, slug: str, ev: dict) -> Optional[Market]:
        """Build the binary Market for a game from the event and the (cached) market sides."""
        sides = self._sides_cache.get(slug)
        if not sides:
            sides = self._fetch_sides(slug)
            if sides:
                self._sides_cache[slug] = sides
        ev_slug = str(ev.get("slug") or "")
        parts = ev_slug.split("-")
        abbrs = parts[1:3] if len(parts) >= 4 else []
        names = _split_title(str(ev.get("title") or ""))
        if not sides:
            if len(abbrs) != 2:
                return None
            sides = {"long": {"abbr": abbrs[0], "name": names[0] if names else abbrs[0]},
                     "short": {"abbr": abbrs[1], "name": names[1] if len(names) == 2 else abbrs[1]}, "tick": 0.005}
        long_t, short_t = sides["long"], sides["short"]

        def aliases(t: dict, idx: int) -> list[str]:
            al = [t.get("abbr", ""), t.get("name", ""), t.get("alias", ""), t.get("safeName", "")]
            if len(names) == 2:
                # title order may differ from long/short order: add the title name whose abbreviation matches
                for nm in names:
                    if nm.lower().startswith(str(t.get("abbr", "")).lower() + " ") or _strip_abbr(nm).lower() in str(t.get("name", "")).lower():
                        al += [nm, _strip_abbr(nm)]
            return [a for a in dict.fromkeys(al) if a]

        start = parse_iso(ev.get("startTime")) or parse_iso(sides.get("gameStartTime"))
        self._market_meta[slug] = {"event": ev_slug, "sides": sides}
        return Market(
            condition_id=ev_slug or slug,
            question=str(ev.get("title") or slug),
            token_ids=[token_id(slug, "L"), token_id(slug, "S")],
            outcomes=[str(long_t.get("name") or long_t.get("abbr")), str(short_t.get("name") or short_t.get("abbr"))],
            outcome_prices=[],
            # no endTime on game events: keep the market in play for the whole game (kickoff + 6h)
            end_date=parse_iso(ev.get("endTime")) or (start + timedelta(hours=6) if start else None),
            neg_risk=False,
            liquidity=0.0,                          # the venue's event liquidity is not comparable; depth decides
            volume24h=float(ev.get("volume") or 0),
            spread=0.0, min_order_size=1.0, tick_size=float(sides.get("tick") or 0.005),
            accepting_orders=bool(ev.get("active", True)),
            closed=bool(ev.get("closed")),
            event_id=ev_slug, event_title=str(ev.get("title") or ""), slug=slug,
            description=str(sides.get("description") or ev.get("description") or "")[:2000], event_slug=ev_slug,
            sports_type="moneyline", game_start=start,
            outcome_aliases=[aliases(long_t, 0), aliases(short_t, 1)],
        )

    def _fetch_sides(self, slug: str) -> Optional[dict]:
        try:
            res = self.c.markets.retrieve_by_slug(slug) or {}
        except Exception as e:
            log.info("market detail failed for %s: %s", slug, e)
            return None
        m = res.get("market") or res
        long_t = short_t = None
        for sd in m.get("marketSides") or []:
            t = sd.get("team") or {}
            info = {"abbr": str(t.get("abbreviation") or "").lower(), "name": str(t.get("name") or sd.get("description") or ""),
                    "alias": str(t.get("alias") or ""), "safeName": str(t.get("safeName") or ""), "teamId": t.get("id")}
            if sd.get("long"):
                long_t = info
            else:
                short_t = info
        if not long_t or not short_t:
            return None
        return {"long": long_t, "short": short_t, "tick": float(m.get("orderPriceMinTickSize") or 0.005),
                "description": str(m.get("description") or "")[:600], "gameStartTime": m.get("gameStartTime")}

    def _events_for_tag(self, tag: str, now: datetime, page: int, max_pages: int, days_ahead: int) -> list[dict]:
        events: list[dict] = []
        offset = 0
        params_base: dict = {"active": True, "closed": False, "limit": page, "tagSlug": tag,
                             "startTimeMin": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "startTimeMax": (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for _ in range(max_pages):
            try:
                res = self.c.events.list({**params_base, "offset": offset})
            except Exception as e:
                log.warning("polymarket.us events.list(%s) failed at offset %d: %s", tag, offset, e)
                if offset == 0 and "startTimeMin" in params_base:
                    params_base.pop("startTimeMin"); params_base.pop("startTimeMax", None)
                    continue
                break
            batch = res.get("events") or []
            events += batch
            offset += len(batch)
            if len(batch) < page:
                break
        if events:
            log.info("polymarket.us tag '%s' -> %d events", tag, len(events))
        return events

    # ------------------------------------------------------------------ side cache (static team data)
    def _load_cache(self) -> dict[str, dict]:
        if self._cache_path and os.path.exists(self._cache_path):
            try:
                with open(self._cache_path) as f:
                    return json.load(f)
            except (OSError, ValueError):
                return {}
        return {}

    def _save_cache(self) -> None:
        if not self._cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self._cache_path) or ".", exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._sides_cache, f, indent=0, sort_keys=True)
        except OSError as e:
            log.info("could not save sides cache: %s", e)

    # ------------------------------------------------------------------ market data
    def books(self, token_ids: list[str], batch: int = 40) -> dict[str, Book]:
        out: dict[str, Book] = {}
        by_slug: dict[str, list[str]] = {}
        for tok in token_ids:
            slug, _ = split_token(tok)
            by_slug.setdefault(slug, []).append(tok)
        errors: dict[str, int] = {}
        states: dict[str, int] = {}
        for slug, toks in by_slug.items():
            md = None
            for attempt in range(3):
                try:
                    md = (self.c.markets.book(slug) or {}).get("marketData") or {}
                    break
                except Exception as e:
                    name = type(e).__name__
                    if "RateLimit" in name or "429" in str(e):
                        time.sleep(self.book_delay * (4 ** (attempt + 1)))      # 0.6s, 2.4s, ...
                        continue
                    key = f"{name}: {str(e)[:80]}"
                    errors[key] = errors.get(key, 0) + 1
                    break
            if md is None:
                errors["RateLimitError"] = errors.get("RateLimitError", 0) + 1
                continue
            time.sleep(self.book_delay)
            states[str(md.get("state"))] = states.get(str(md.get("state")), 0) + 1
            open_ = md.get("state") in (None, "MARKET_STATE_OPEN")
            bids = [(p, float(l.get("qty") or 0)) for l in md.get("bids") or [] if (p := _amount(l.get("px"))) is not None]
            asks = [(p, float(l.get("qty") or 0)) for l in md.get("offers") or [] if (p := _amount(l.get("px"))) is not None]
            fee = self.fee_bps(slug)
            for tok in toks:
                _, side = split_token(tok)
                if side == "L":
                    b_bids, b_asks = bids, asks
                else:   # buying SHORT at x fills against a long bid at 1-x
                    b_bids = [(round(1.0 - p, 4), q) for p, q in asks]
                    b_asks = [(round(1.0 - p, 4), q) for p, q in bids]
                b = Book.from_clob({"asset_id": tok, "bids": [{"price": p, "size": q} for p, q in b_bids],
                                    "asks": [{"price": p, "size": q} for p, q in b_asks]}, fee_bps=fee)
                if not open_:
                    b.asks, b.bids = [], []
                out[tok] = b
        if errors or states:
            log.info("polymarket.us books: states=%s errors=%s", states, errors)
        return out

    def fee_bps(self, slug: str) -> int:
        """Polymarket US taker fee is 0.06 * C * p * (1-p) (docs.polymarket.us/fees); the exact
        commission still comes from the order preview at execution time."""
        return 600

    def resolved(self, condition_ids: set[str]) -> dict[str, list[float]]:
        """event slug -> [payout long, payout short] once the game's market has settled."""
        out = {}
        by_event = {meta["event"]: slug for slug, meta in self._market_meta.items()}
        for ev in condition_ids:
            slug = by_event.get(ev)
            if not slug:
                continue
            try:
                st = self.c.markets.settlement(slug) or {}
                s = float(st.get("settlement"))
            except Exception:
                continue
            if 0.0 <= s <= 1.0:
                out[ev] = [s, 1.0 - s]
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
            if net == 0:
                continue
            side = "L" if net > 0 else "S"
            size = abs(net)
            cost = abs(_amount(p.get("cost")) or 0.0)
            meta = p.get("marketMetadata") or {}
            cash = _amount(p.get("cashValue"))
            rows.append({"asset": token_id(slug, side), "size": size, "avgPrice": cost / size if size else 0.0,
                         "curPrice": (abs(cash) / size) if (cash is not None and size) else 0.0,
                         "conditionId": meta.get("eventSlug") or self._market_meta.get(slug, {}).get("event") or slug,
                         "title": meta.get("title") or slug, "outcome": meta.get("outcome") or side,
                         "endDate": None, "redeemable": False})
        return rows

    def open_orders(self) -> list[dict]:
        return (self.c.orders.list() or {}).get("orders") or []

    def cancel_all(self) -> None:
        self.c.orders.cancel_all()

    def _tick(self, slug: str) -> float:
        return float(self._market_meta.get(slug, {}).get("sides", {}).get("tick") or self._sides_cache.get(slug, {}).get("tick") or 0.005)

    def preview_fee(self, token: str, action: str, price: float, qty: int) -> Optional[float]:
        slug, _ = split_token(token)
        try:
            res = self.c.orders.preview({"request": _order_params(slug, intent_for(token, action), price, qty, self._tick(slug))}) or {}
            o = res.get("order") or {}
            fee = _amount(o.get("commissionNotionalTotalCollected"))
            if fee is None and o.get("commissionsBasisPoints"):
                fee = float(o["commissionsBasisPoints"]) / 10_000 * price * qty
            return fee
        except Exception as e:
            log.info("preview failed for %s: %s", slug, e)
            return None

    def fill_or_kill(self, leg: Leg, tick_size=None, neg_risk=None) -> tuple[float, dict]:
        slug, _ = split_token(leg.token_id)
        qty = int(leg.size)
        if qty <= 0:
            return 0.0, {"errorMsg": "zero quantity"}
        params = _order_params(slug, intent_for(leg.token_id, leg.side), leg.price, qty, self._tick(slug))
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


def _order_params(slug: str, intent: str, price: float, qty: int, tick: float = 0.005) -> dict:
    """Snap the price to the tick: buys round up so a fill-or-kill still crosses, sells round down."""
    buying = intent.endswith("BUY_LONG") or intent.endswith("BUY_SHORT")
    steps = math.ceil(price / tick - 1e-9) if buying else math.floor(price / tick + 1e-9)
    px = max(tick, min(1.0 - tick, steps * tick))
    decimals = max(2, len(f"{tick:.6f}".rstrip("0").split(".")[1]))
    return {"marketSlug": slug, "intent": intent, "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{px:.{decimals}f}", "currency": "USD"}, "quantity": int(qty),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL"}


def make_client(key_id: str = "", secret: str = ""):
    from polymarket_us import PolymarketUS as SDK
    if key_id and secret:
        return SDK(key_id=key_id, secret_key=secret)
    return SDK()
