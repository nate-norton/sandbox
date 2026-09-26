"""Sports mode: ESPN parsing, market matching, model edges, exits, and a full cycle."""
from datetime import timedelta

from polybot.config import Config
from polybot.espn import Game, Team, parse_event
from polybot.run import run_once
from polybot.sports import find_sports_edges, match_markets, sports_exits
from tests.fakes import NOW, FakeClob, FakeGamma, mk_book, mk_market


def espn_event(eid, home, away, state="pre", hs=None, aws=None, ml_home=None, ml_away=None, wp=None,
               completed=False, winner_home=None, start=None):
    def comp(team, ha, score, winner):
        loc, nick = team
        d = {"homeAway": ha, "team": {"displayName": f"{loc} {nick}", "shortDisplayName": nick if len(nick) > 3 else loc,
                                      "name": nick, "location": loc, "abbreviation": nick[:3].upper()}}
        if score is not None:
            d["score"] = str(score)
        if winner is not None:
            d["winner"] = winner
        return d
    ev = {"id": eid, "date": (start or NOW + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%MZ"),
          "name": f"{away[0]} {away[1]} at {home[0]} {home[1]}",
          "status": {"type": {"state": state, "completed": completed}, "period": 3 if state == "in" else 0, "displayClock": "7:12"},
          "competitions": [{"competitors": [comp(home, "home", hs, winner_home if completed else None),
                                            comp(away, "away", aws, (not winner_home) if completed and winner_home is not None else None)]}]}
    c = ev["competitions"][0]
    if ml_home is not None:
        c["odds"] = [{"provider": {"name": "ESPN BET"}, "details": "X -3.5", "overUnder": 44.5, "spread": -3.5,
                      "homeTeamOdds": {"moneyLine": ml_home}, "awayTeamOdds": {"moneyLine": ml_away}}]
    if wp is not None:
        c["situation"] = {"lastPlay": {"probability": {"homeWinPercentage": wp}}}
    return ev


def test_parse_event_pregame_and_live():
    g = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-150, ml_away=130), "nfl")
    assert g.home.matches("Texans") and g.away.matches("Colts") and g.home.matches("Houston Texans")
    p_home = g.pregame_p_home()
    assert abs(p_home - (0.6 / (0.6 + 100 / 230))) < 1e-9
    assert g.model_p("home") == (p_home, "moneyline") and abs(g.model_p("away")[0] - (1 - p_home)) < 1e-9

    live = parse_event(espn_event("2", ("Ohio State", "Buckeyes"), ("Illinois", "Fighting Illini"), state="in",
                                  hs=28, aws=10, wp=94.5), "cfb")
    assert live.state == "in" and live.home_wp == 0.945 and live.home.score == 28
    assert live.model_p("home") == (0.945, "live_wp") and live.home.matches("Ohio State")

    done = parse_event(espn_event("3", ("A", "Aces"), ("B", "Bees"), state="post", hs=21, aws=20, completed=True, winner_home=True), "nfl")
    assert done.model_p("home") == (1.0, "final") and done.model_p("away") == (0.0, "final")
    assert parse_event({"id": "x"}, "nfl") is None


def test_parse_current_espn_odds_shape():
    """Exact structure seen in the CI log on 2026-09-26 (DraftKings provider)."""
    ev = espn_event("9", ("Buffalo", "Bills"), ("Los Angeles", "Chargers"))
    ev["competitions"][0]["odds"] = [{
        "provider": {"id": "100", "name": "DraftKings"}, "details": "BUF -7", "overUnder": 50.5, "spread": -7.0,
        "awayTeamOdds": {"favorite": False, "underdog": True, "team": {"abbreviation": "LAC"}},
        "homeTeamOdds": {"favorite": True, "underdog": False, "team": {"abbreviation": "BUF"}},
        "moneyline": {"displayName": "Moneyline", "home": {"close": {"odds": "-345"}, "open": {"odds": "-330"}},
                      "away": {"close": {"odds": "+270"}, "open": {"odds": "+260"}}},
    }]
    g = parse_event(ev, "nfl")
    assert g.ml_home == -345 and g.ml_away == 270 and g.spread == -7.0 and g.over_under == 50.5
    p = g.pregame_p_home()
    assert abs(p - (345 / 445) / (345 / 445 + 100 / 370)) < 1e-9
    ev["competitions"][0]["odds"][0]["moneyline"]["home"]["current"] = {"odds": "EVEN"}
    assert parse_event(ev, "nfl").ml_home == 100


def test_match_markets_by_outcomes_and_timing():
    g = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-150, ml_away=130), "nfl")
    m = mk_market("c1", "ty", "tn", hours=6)
    m.question, m.outcomes, m.sports_type = "Texans vs. Colts", ["Colts", "Texans"], "moneyline"
    spread = mk_market("c2", "sy", "sn", hours=6)
    spread.question, spread.outcomes, spread.sports_type = "Spread: Texans (-3.5)", ["Texans", "Colts"], "spreads"
    far = mk_market("c3", "fy", "fn", hours=24 * 10)
    far.question, far.outcomes, far.sports_type = "Texans vs. Colts", ["Texans", "Colts"], "moneyline"
    matched = match_markets([m, spread, far], [g], NOW)
    assert len(matched) == 1 and matched[0].market is m and matched[0].sides == ["away", "home"]


def cfg(tmp_path):
    c = Config(profile="aggressive")
    c.state_dir = str(tmp_path)
    c.kill_switch_file = str(tmp_path / "STOP")
    c.private_key = c.funder = c.openrouter_api_key = ""
    c.sports_only = True
    return c


def test_sports_edges_pregame_and_live(tmp_path):
    c = cfg(tmp_path)
    g = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-200, ml_away=170), "nfl")
    m = mk_market("c1", "hou", "ind", hours=6)
    m.question, m.outcomes, m.sports_type = "Texans vs. Colts", ["Texans", "Colts"], "moneyline"
    # model says Texans ~0.65; market sells Texans at 0.55 -> edge ~0.09 after fee
    books = {"hou": mk_book("hou", [(0.55, 100)], [(0.53, 100)], fee=1000), "ind": mk_book("ind", [(0.46, 100)], [(0.44, 100)], fee=1000)}
    matched = match_markets([m], [g], NOW)
    opps = find_sports_edges(matched, books, c, NOW, equity=60.0, per_position_cap=30.0, held=set())
    assert len(opps) == 1 and opps[0].legs[0].outcome == "Texans" and opps[0].kind == "sports_edge"
    p = g.model_p("home")[0]
    f_star = (p - 0.55) / 0.45
    assert opps[0].legs[0].size == int(0.5 * f_star * 60 / 0.55)
    # no edge when the market agrees
    books["hou"] = mk_book("hou", [(0.66, 100)], [(0.64, 100)], fee=1000)
    assert find_sports_edges(matched, books, c, NOW, 60.0, 30.0, set()) == []
    # live: 94.5% win prob, market at 0.90 -> edge 0.045 - fee(0.009) = 0.036 >= 0.03 "sure" margin
    live = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), state="in", hs=28, aws=10, wp=0.945), "nfl")
    books["hou"] = mk_book("hou", [(0.90, 100)], [(0.88, 100)], fee=1000)
    opps = find_sports_edges(match_markets([m], [live], NOW), books, c, NOW, 60.0, 30.0, set())
    assert len(opps) == 1 and "live_wp" in opps[0].note


def test_sports_exit_when_market_overprices_our_side(tmp_path):
    from polybot.ledger import Ledger
    c = cfg(tmp_path)
    live = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), state="in", hs=10, aws=28, wp=0.20), "nfl")
    m = mk_market("c1", "hou", "ind", hours=2)
    m.question, m.outcomes, m.sports_type = "Texans vs. Colts", ["Texans", "Colts"], "moneyline"
    led = Ledger.load(str(tmp_path / "l.json"), 60.0, "paper")
    led.record_buy("hou", "c1", m.question, "Texans", 20, 0.60, None, "sports_edge", False)
    books = {"hou": mk_book("hou", [(0.32, 100)], [(0.30, 100)], fee=1000)}
    exits = sports_exits(match_markets([m], [live], NOW), books, led, c)
    assert len(exits) == 1 and exits[0][0] == "hou" and exits[0][2] == 0.30       # bid 0.30 - fee > model 0.20 + 0.05
    books["hou"] = mk_book("hou", [(0.24, 100)], [(0.22, 100)], fee=1000)
    assert sports_exits(match_markets([m], [live], NOW), books, led, c) == []


class FakeEspn:
    def __init__(self, games):
        self._games = games
    def games(self, league, now, days_ahead=7):
        return [g for g in self._games if g.league == league]
    def live_wp(self, game):
        return None


def test_full_sports_cycle_ignores_non_sports_markets(tmp_path):
    c = cfg(tmp_path)
    g = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-200, ml_away=170), "nfl")
    nfl = mk_market("c1", "hou", "ind", hours=6)
    nfl.question, nfl.outcomes, nfl.sports_type = "Texans vs. Colts", ["Texans", "Colts"], "moneyline"
    soccer = mk_market("c2", "sy", "sn", hours=6)              # a 96c favourite the old harvest would buy
    soccer.question = "Exact Score: Team A 0 - 2 Team B?"
    books = {"hou": mk_book("hou", [(0.55, 100)], [(0.53, 100)], fee=1000), "ind": mk_book("ind", [(0.46, 100)], [(0.44, 100)], fee=1000),
             "sy": mk_book("sy", [(0.96, 100)], [(0.955, 100)]), "sn": mk_book("sn", [(0.05, 100)], [(0.04, 100)])}
    led = run_once(c, FakeGamma([nfl, soccer]), FakeClob(books), now=NOW, espn=FakeEspn([g]))
    kinds = {p.condition_id: p.kind for p in led.positions.values()}
    assert kinds == {"c1": "sports_edge"}
    assert led.scan["sports.matched"] == 1 and led.scan["sports.candidate"] == 1
    report = open(tmp_path / "report.md").read()
    assert "sports_edge" in report and "Texans" in report
