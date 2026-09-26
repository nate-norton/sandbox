"""Polymarket US adapter against a fake SDK client, plus paper and live cycles in US mode."""
from datetime import timedelta

from polybot.config import Config
from polybot.models import Leg
from polybot.pmus import PolymarketUS, _order_params, intent_for, split_token, token_id
from polybot.run import run_once
from tests.fakes import NOW
from tests.test_sports import FakeEspn, espn_event
from polybot.espn import parse_event


def game_event(ev_slug, title, long_team, short_team, start, league="nfl", extra_markets=()):
    """An event as events.list returns it (embedded markets are minimal) plus the detail the adapter fetches."""
    aec = f"aec-{ev_slug}"
    event = {"slug": ev_slug, "title": title, "startTime": start, "endTime": None, "active": True, "closed": False,
             "liquidity": 20000, "volume": 5000, "tags": [{"slug": league}, {"slug": "games"}],
             "markets": [{"slug": aec, "title": None}] + [{"slug": s, "title": t} for s, t in extra_markets]}
    detail = {"market": {"slug": aec, "orderPriceMinTickSize": 0.005, "gameStartTime": start,
                         "description": f"Settles to the winner of {title}. Ties settle at $0.50.",
                         "marketSides": [{"long": True, "description": long_team["alias"], "team": long_team},
                                         {"long": False, "description": short_team["alias"], "team": short_team}]}}
    return event, detail


TEXANS = {"id": 1, "name": "Houston Texans", "abbreviation": "hou", "alias": "Texans", "safeName": "HOU Texans", "league": "nfl"}
COLTS = {"id": 2, "name": "Indianapolis Colts", "abbreviation": "ind", "alias": "Colts", "safeName": "IND Colts", "league": "nfl"}
OSU = {"id": 3, "name": "Ohio State Buckeyes", "abbreviation": "osu", "alias": "Buckeyes", "safeName": "Ohio State", "league": "cfb"}
ILL = {"id": 4, "name": "Illinois Fighting Illini", "abbreviation": "ill", "alias": "Fighting Illini", "safeName": "Illinois", "league": "cfb"}


class FakeSDK:
    def __init__(self, events, details, books, balance=60.0):
        self._events, self._details, self._books, self._balance = events, details, books, balance
        self.created, self.previews, self.detail_calls = [], [], 0
        self.positions_rows, self.settlements = {}, {}
        outer = self

        class Events:
            def list(self, params=None):
                tag = (params or {}).get("tagSlug")
                off = (params or {}).get("offset", 0)
                evs = [e for e in outer._events if not tag or any(t["slug"] == tag for t in e["tags"])]
                return {"events": evs[off:off + 100]}

        class Markets:
            def retrieve_by_slug(self, slug):
                outer.detail_calls += 1
                if slug not in outer._details:
                    raise RuntimeError("not found")
                return outer._details[slug]
            def book(self, slug):
                b = outer._books.get(slug, {"bids": [], "offers": []})
                return {"marketData": {"marketSlug": slug, "state": "MARKET_STATE_OPEN",
                                       "bids": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in b["bids"]],
                                       "offers": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in b["offers"]]}}
            def settlement(self, slug):
                if slug not in outer.settlements:
                    raise RuntimeError("not settled")
                return {"slug": slug, "settlement": outer.settlements[slug]}

        class Account:
            def balances(self):
                return {"balances": [{"currentBalance": outer._balance, "buyingPower": outer._balance, "currency": "USD"}]}

        class Portfolio:
            def positions(self, params=None):
                return {"positions": outer.positions_rows, "eof": True}

        class Orders:
            def list(self, params=None):
                return {"orders": []}
            def cancel_all(self, params=None):
                return {"canceledOrderIds": []}
            def preview(self, params):
                outer.previews.append(params)
                return {"order": {"commissionsBasisPoints": "250", "quantity": params["request"]["quantity"]}}
            def create(self, params):
                outer.created.append(params)
                qty = params["quantity"]
                return {"id": "ord-1", "executions": [{"type": "EXECUTION_TYPE_FILL", "lastShares": str(qty), "lastPx": params["price"],
                                                       "commissionNotionalCollected": {"value": "0.05", "currency": "USD"}}]}

        self.events, self.markets, self.account, self.portfolio, self.orders = Events(), Markets(), Account(), Portfolio(), Orders()


def fixture(start=None):
    start = (start or NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")   # inside the 2h book window
    e1, d1 = game_event("nfl-hou-ind-2026-09-27", "HOU Texans vs IND Colts", TEXANS, COLTS, start,
                        extra_markets=[("asc-nfl-hou-ind-2026-09-27-pos-3pt5", "Houston Texans wins by over 3.5"),
                                       ("atc-nfl-hou-ind-2026-09-27-winner-1h-hou", "Houston Texans wins 1st half")])
    e2, d2 = game_event("cfb-ill-osu-2026-09-26", "Illinois vs. Ohio State", OSU, ILL, start, league="cfb")
    futures = {"slug": "nfl-afcsouth-2027-01-10-w", "title": "AFC South Division Winner", "startTime": start, "active": True,
               "closed": False, "tags": [{"slug": "nfl"}], "markets": [{"slug": "tec-nfl-afcsouth-2027-01-10-w-hou"}, {"slug": "tec-nfl-afcsouth-2027-01-10-w-ind"}]}
    return [e1, e2, futures], {d1["market"]["slug"]: d1, d2["market"]["slug"]: d2}


def test_discovery_builds_one_binary_market_per_game(tmp_path):
    events, details = fixture()
    sdk = FakeSDK(events, details, {})
    us = PolymarketUS(sdk, cache_path=str(tmp_path / "us_markets.json"))
    ms = us.football_markets(("nfl", "cfb"))
    assert [m.condition_id for m in ms] == ["nfl-hou-ind-2026-09-27", "cfb-ill-osu-2026-09-26"]
    m = ms[0]
    assert m.token_ids == ["us:aec-nfl-hou-ind-2026-09-27:L", "us:aec-nfl-hou-ind-2026-09-27:S"]
    assert m.outcomes == ["Houston Texans", "Indianapolis Colts"] and m.tick_size == 0.005
    assert "hou" in m.outcome_aliases[0] and "Texans" in m.outcome_aliases[0] and "ind" in m.outcome_aliases[1]
    # no endTime from the venue: the market must stay a candidate through the whole game, not drop at kickoff
    assert m.end_date == m.game_start + timedelta(hours=6)
    assert m.hours_to_end(m.game_start + timedelta(hours=2)) > 0
    # second construction uses the on-disk side cache instead of refetching
    us2 = PolymarketUS(sdk, cache_path=str(tmp_path / "us_markets.json"))
    calls = sdk.detail_calls
    us2.football_markets(("nfl", "cfb"))
    assert sdk.detail_calls == calls


def test_books_mirror_short_side_and_orders_use_the_right_intent(tmp_path):
    events, details = fixture()
    slug = "aec-nfl-hou-ind-2026-09-27"
    sdk = FakeSDK(events, details, {slug: {"bids": [(0.53, 40)], "offers": [(0.55, 100), (0.56, 50)]}})
    us = PolymarketUS(sdk, key_id="abcdef12-0000")
    us.book_delay = 0.0
    us.football_markets()
    books = us.books([token_id(slug, "L"), token_id(slug, "S")])
    L, S = books[token_id(slug, "L")], books[token_id(slug, "S")]
    assert L.best_ask == 0.55 and L.best_bid == 0.53 and L.fee_bps == 600
    assert abs(S.best_ask - 0.47) < 1e-9 and abs(S.best_bid - 0.45) < 1e-9 and S.ask_depth_at_or_below(0.47) == 40
    assert intent_for(token_id(slug, "S"), "BUY") == "ORDER_INTENT_BUY_SHORT" and intent_for(token_id(slug, "L"), "SELL") == "ORDER_INTENT_SELL_LONG"
    assert split_token("us:aec-x:S") == ("aec-x", "S") and split_token("us:aec-x") == ("aec-x", "L")
    filled, raw = us.fill_or_kill(Leg(token_id(slug, "S"), "BUY", 0.47, 20, "Colts"))
    assert filled == 20 and raw["status"] == "matched"
    req = sdk.created[-1]
    assert req["intent"] == "ORDER_INTENT_BUY_SHORT" and req["tif"] == "TIME_IN_FORCE_FILL_OR_KILL"
    assert req["price"] == {"value": "0.470", "currency": "USD"} and req["quantity"] == 20
    assert _order_params("x", "ORDER_INTENT_BUY_LONG", 0.5525, 5, 0.005)["price"]["value"] == "0.555"
    assert _order_params("x", "ORDER_INTENT_SELL_LONG", 0.5525, 5, 0.005)["price"]["value"] == "0.550"
    assert _order_params("x", "ORDER_INTENT_BUY_LONG", 0.55, 5, 0.01)["price"]["value"] == "0.55"
    assert us.preview_fee(token_id(slug, "L"), "BUY", 0.55, 20) == 250 / 10000 * 0.55 * 20
    # positions: negative net position is a short
    sdk.positions_rows = {slug: {"netPosition": "-20", "cost": {"value": "-9.40", "currency": "USD"},
                                 "cashValue": {"value": "-10.00", "currency": "USD"},
                                 "marketMetadata": {"eventSlug": "nfl-hou-ind-2026-09-27", "title": "Texans vs Colts", "outcome": "Colts"}}}
    rows = us.positions()
    assert rows[0]["asset"] == token_id(slug, "S") and rows[0]["size"] == 20 and abs(rows[0]["avgPrice"] - 0.47) < 1e-9
    sdk.settlements = {slug: 0.0}
    assert us.resolved({"nfl-hou-ind-2026-09-27"}) == {"nfl-hou-ind-2026-09-27": [0.0, 1.0]}


def us_cfg(tmp_path):
    c = Config(profile="aggressive")
    c.state_dir, c.kill_switch_file = str(tmp_path), str(tmp_path / "STOP")
    c.openrouter_api_key = ""
    return c


def test_us_paper_cycle_trades_short_side_then_settles(tmp_path):
    c = us_cfg(tmp_path)
    events, details = fixture()
    slug = "aec-nfl-hou-ind-2026-09-27"
    # long (Texans) bid 0.45 => Colts (short) offered at 0.55 while the sportsbook makes the Colts ~0.64: buy SHORT
    sdk = FakeSDK(events, details, {slug: {"bids": [(0.45, 100)], "offers": [(0.47, 100)]}})
    us = PolymarketUS(sdk)
    g = parse_event(espn_event("1", ("Indianapolis", "Colts"), ("Houston", "Texans"), ml_home=-200, ml_away=170,
                               start=NOW + timedelta(hours=6)), "nfl")
    led = run_once(c, None, None, now=NOW, espn=FakeEspn([g]), us=us)
    assert len(led.positions) == 1
    pos = next(iter(led.positions.values()))
    assert pos.token_id == token_id(slug, "S") and pos.outcome == "Indianapolis Colts" and pos.kind == "sports_edge"
    assert abs(pos.avg_price - 0.55) < 1e-9
    sdk.settlements = {slug: 0.0}                              # Colts win: long pays 0, short pays 1
    led = run_once(c, None, None, now=NOW + timedelta(days=1), espn=FakeEspn([g]), us=us)
    assert not led.positions and led.realized_pnl > 0


def test_us_live_cycle_uses_exchange_balance_and_fee_preview(tmp_path):
    c = us_cfg(tmp_path)
    events, details = fixture()
    slug = "aec-nfl-hou-ind-2026-09-27"
    sdk = FakeSDK(events, details, {slug: {"bids": [(0.53, 100)], "offers": [(0.55, 100)]}}, balance=60.0)
    us = PolymarketUS(sdk, key_id="k")
    g = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-200, ml_away=170,
                               start=NOW + timedelta(hours=6)), "nfl")
    led = run_once(c, None, None, live=us, now=NOW, espn=FakeEspn([g]), us=us)
    assert led.mode == "live" and led.starting_bankroll == 60.0
    assert sdk.previews and sdk.created[-1]["intent"] == "ORDER_INTENT_BUY_LONG" and sdk.created[-1]["tif"] == "TIME_IN_FORCE_FILL_OR_KILL"
    assert len(led.positions) == 1 and abs(led.positions[token_id(slug, "L")].avg_price - 0.55) < 1e-9
