"""Polymarket US adapter against a fake SDK client, plus a full paper cycle in US mode."""
from datetime import timedelta

from polybot.config import Config
from polybot.models import Leg
from polybot.pmus import PolymarketUS, slug_of, token_id
from polybot.run import run_once
from tests.fakes import NOW
from tests.test_sports import FakeEspn, espn_event
from polybot.espn import parse_event


def team_market(slug, ev, team_id, name, league, title, outcome=None, liq=5000):
    return {"id": hash(slug) % 10000, "slug": slug, "title": title, "outcome": outcome or name, "eventSlug": ev,
            "active": True, "closed": False, "liquidity": liq, "volume": 1000,
            "team": {"id": team_id, "name": name, "abbreviation": name[:3].upper(), "league": league}}


class FakeSDK:
    """Mimics polymarket_us.PolymarketUS: resources with dict responses."""
    def __init__(self, markets, books, start="2026-09-27T17:00:00Z", balance=60.0):
        self._markets, self._books, self._start, self._balance = markets, books, start, balance
        self.created, self.previews = [], []
        self.positions_rows = {}
        self.settlements = {}
        outer = self

        class Markets:
            def list(self, params=None):
                off = params.get("offset", 0) if params else 0
                lim = params.get("limit", 100) if params else 100
                return {"markets": outer._markets[off:off + lim]}
            def book(self, slug):
                b = outer._books.get(slug, {"bids": [], "offers": []})
                return {"marketData": {"marketSlug": slug, "state": "MARKET_STATE_OPEN",
                                       "bids": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in b["bids"]],
                                       "offers": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in b["offers"]]}}
            def settlement(self, slug):
                if slug not in outer.settlements:
                    raise RuntimeError("not settled")
                return {"slug": slug, "settlement": outer.settlements[slug]}

        class Events:
            def retrieve_by_slug(self, slug):
                return {"event": {"slug": slug, "startTime": outer._start, "title": "Texans vs Colts"}}
            def list(self, params=None):
                off = params.get("offset", 0) if params else 0
                groups = {}
                for m in outer._markets:
                    groups.setdefault(m["eventSlug"], []).append(m)
                evs = [{"slug": ev, "title": ms[0]["title"], "startTime": outer._start, "active": True, "closed": False,
                        "liquidity": sum(m["liquidity"] for m in ms), "volume": 1000,
                        "tags": [{"slug": ms[0]["team"]["league"].lower(), "label": ms[0]["team"]["league"]}],
                        "markets": [{"slug": m["slug"], "title": m["title"] if "Spread" in m["title"] else m["team"]["name"],
                                     "outcome": m["outcome"], "active": True, "closed": False} for m in ms]}
                       for ev, ms in groups.items()]
                return {"events": evs[off:off + 100]}

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
                req = params["request"]
                return {"order": {"commissionsBasisPoints": "250", "quantity": req["quantity"]}}
            def create(self, params):
                outer.created.append(params)
                qty = params["quantity"]
                return {"id": "ord-1", "executions": [{"type": "EXECUTION_TYPE_FILL", "lastShares": str(qty),
                                                       "lastPx": params["price"],
                                                       "commissionNotionalCollected": {"value": "0.05", "currency": "USD"}}]}

        self.markets, self.events, self.account, self.portfolio, self.orders = Markets(), Events(), Account(), Portfolio(), Orders()


def two_team_markets():
    return [
        team_market("nfl-hou-ind-hou", "nfl-hou-ind", 1, "Texans", "NFL", "Texans vs Colts"),
        team_market("nfl-hou-ind-ind", "nfl-hou-ind", 2, "Colts", "NFL", "Texans vs Colts"),
        team_market("nfl-hou-ind-spread", "nfl-hou-ind", 1, "Texans", "NFL", "Spread: Texans -3.5"),   # not a moneyline
        team_market("nba-x-y", "nba-x-y", 9, "Lakers", "NBA", "Lakers vs Celtics"),                    # other sport
        team_market("cfb-osu-ill-osu", "cfb-osu-ill", 3, "Ohio State", "NCAAF", "Illinois vs Ohio State"),
    ]


def test_football_markets_groups_team_markets_into_games():
    us = PolymarketUS(FakeSDK(two_team_markets(), {}))
    ms = us.football_markets(("nfl", "cfb"))
    assert len(ms) == 1                                     # the CFB event has only one team market -> skipped
    m = ms[0]
    assert m.condition_id == "nfl-hou-ind" and m.outcomes == ["Texans", "Colts"]
    assert m.token_ids == ["us:nfl-hou-ind-hou", "us:nfl-hou-ind-ind"] and m.sports_type == "moneyline"
    assert m.game_start.isoformat().startswith("2026-09-27T17:00")


def test_books_positions_balance_and_orders():
    sdk = FakeSDK(two_team_markets(), {"nfl-hou-ind-hou": {"bids": [(0.53, 40)], "offers": [(0.55, 100), (0.56, 50)]}})
    us = PolymarketUS(sdk, key_id="abcdef12-0000")
    us.football_markets()
    books = us.books(["us:nfl-hou-ind-hou"])
    b = books["us:nfl-hou-ind-hou"]
    assert b.best_ask == 0.55 and b.best_bid == 0.53 and b.ask_depth_at_or_below(0.55) == 100 and b.fee_bps == 500
    assert us.usdc_balance() == 60.0
    sdk.positions_rows = {"nfl-hou-ind-hou": {"netPosition": "20", "cost": {"value": "11.00", "currency": "USD"},
                                             "cashValue": {"value": "12.00", "currency": "USD"},
                                             "marketMetadata": {"eventSlug": "nfl-hou-ind", "title": "Texans vs Colts", "outcome": "Texans"}}}
    rows = us.positions()
    assert rows[0]["asset"] == "us:nfl-hou-ind-hou" and rows[0]["size"] == 20 and abs(rows[0]["avgPrice"] - 0.55) < 1e-9
    assert us.preview_fee("nfl-hou-ind-hou", "ORDER_INTENT_BUY_LONG", 0.55, 20) == 250 / 10000 * 0.55 * 20
    filled, raw = us.fill_or_kill(Leg("us:nfl-hou-ind-hou", "BUY", 0.55, 20, "Texans"))
    assert filled == 20 and raw["status"] == "matched" and raw["fee"] == 0.05
    req = sdk.created[-1]
    assert req["tif"] == "TIME_IN_FORCE_FILL_OR_KILL" and req["intent"] == "ORDER_INTENT_BUY_LONG"
    assert req["price"] == {"value": "0.55", "currency": "USD"} and req["quantity"] == 20
    us.fill_or_kill(Leg("us:nfl-hou-ind-hou", "SELL", 0.53, 20, "Texans"))
    assert sdk.created[-1]["intent"] == "ORDER_INTENT_SELL_LONG"
    sdk.settlements = {"nfl-hou-ind-hou": 1.0, "nfl-hou-ind-ind": 0.0}
    assert us.resolved({"nfl-hou-ind"}) == {"nfl-hou-ind": [1.0, 0.0]}


def test_us_paper_cycle_trades_then_settles(tmp_path):
    c = Config(profile="aggressive")
    c.state_dir, c.kill_switch_file = str(tmp_path), str(tmp_path / "STOP")
    c.openrouter_api_key = ""
    books = {"nfl-hou-ind-hou": {"bids": [(0.53, 100)], "offers": [(0.55, 100)]},
             "nfl-hou-ind-ind": {"bids": [(0.44, 100)], "offers": [(0.46, 100)]}}
    sdk = FakeSDK(two_team_markets(), books, start=(NOW + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    us = PolymarketUS(sdk)
    g = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-200, ml_away=170,
                               start=NOW + timedelta(hours=6)), "nfl")
    led = run_once(c, None, None, now=NOW, espn=FakeEspn([g]), us=us)
    assert len(led.positions) == 1
    pos = next(iter(led.positions.values()))
    assert pos.outcome == "Texans" and pos.kind == "sports_edge" and pos.token_id == "us:nfl-hou-ind-hou"
    assert led.scan["sports.matched"] == 1
    # game over, Texans win: settlement pays $1
    sdk.settlements = {"nfl-hou-ind-hou": 1.0, "nfl-hou-ind-ind": 0.0}
    led = run_once(c, None, None, now=NOW + timedelta(days=1), espn=FakeEspn([g]), us=us)
    assert not led.positions and led.realized_pnl > 0


def test_us_live_cycle_uses_exchange_balance_and_fee_preview(tmp_path):
    c = Config(profile="aggressive")
    c.state_dir, c.kill_switch_file = str(tmp_path), str(tmp_path / "STOP")
    c.openrouter_api_key = ""
    books = {"nfl-hou-ind-hou": {"bids": [(0.53, 100)], "offers": [(0.55, 100)]},
             "nfl-hou-ind-ind": {"bids": [(0.44, 100)], "offers": [(0.46, 100)]}}
    sdk = FakeSDK(two_team_markets(), books, start=(NOW + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"), balance=60.0)
    us = PolymarketUS(sdk, key_id="k")
    g = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-200, ml_away=170,
                               start=NOW + timedelta(hours=6)), "nfl")
    led = run_once(c, None, None, live=us, now=NOW, espn=FakeEspn([g]), us=us)
    assert led.mode == "live" and led.starting_bankroll == 60.0
    assert sdk.previews and sdk.created and sdk.created[-1]["tif"] == "TIME_IN_FORCE_FILL_OR_KILL"
    assert len(led.positions) == 1 and abs(led.positions["us:nfl-hou-ind-hou"].avg_price - 0.55) < 1e-9
