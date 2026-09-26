"""Sports mode: ESPN parsing, market matching, model edges, exits, and a full cycle."""
from collections import Counter
from datetime import timedelta

from polybot.config import Config
from polybot.espn import Game, Team, parse_event
from polybot.run import run_once
from polybot.sports import find_sports_edges, match_markets, sports_exits
from tests.fakes import NOW, FakeClob, FakeGamma, mk_book, mk_market


def espn_event(eid, home, away, state="pre", hs=None, aws=None, ml_home=None, ml_away=None, wp=None,
               completed=False, winner_home=None, start=None, abbrs=None):
    abbrs = abbrs or {}

    def comp(team, ha, score, winner):
        loc, nick = team
        d = {"homeAway": ha, "team": {"displayName": f"{loc} {nick}", "shortDisplayName": nick if len(nick) > 3 else loc,
                                      "name": nick, "location": loc, "abbreviation": abbrs.get(ha, nick[:3].upper())}}
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
    assert opps[0].legs[0].size == int(c.sports_kelly_frac * f_star * 60 / 0.55) and c.sports_kelly_frac == 1.0
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
    def __init__(self, games, unstable_ids=()):
        self._games = games
        self._unstable = set(unstable_ids)
        self.stability_calls = 0
    def games(self, league, now, days_ahead=7):
        return [g for g in self._games if g.league == league]
    def live_wp(self, game):
        return None
    def stability_check(self, games, wait_seconds=20.0):
        self.stability_calls += 1
        for g in games:
            g.stable = g.id not in self._unstable


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


def test_game_window_flag(tmp_path):
    c = cfg(tmp_path)
    g_live = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), state="in", hs=7, aws=3, wp=0.6), "nfl")
    m = mk_market("c1", "hou", "ind", hours=2)
    m.question, m.outcomes, m.sports_type = "Texans vs. Colts", ["Texans", "Colts"], "moneyline"
    books = {"hou": mk_book("hou", [(0.60, 100)], [(0.58, 100)], fee=1000), "ind": mk_book("ind", [(0.41, 100)], [(0.39, 100)], fee=1000)}
    led = run_once(c, FakeGamma([m]), FakeClob(books), now=NOW, espn=FakeEspn([g_live]))
    assert led.game_window is True
    g_far = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-150, ml_away=130,
                                   start=NOW + timedelta(hours=5)), "nfl")
    led = run_once(c, FakeGamma([m]), FakeClob(books), now=NOW, espn=FakeEspn([g_far]))
    assert led.game_window is False


def test_match_by_alias_when_title_is_truncated():
    g = parse_event(espn_event("1", ("Buffalo", "Bills"), ("Los Angeles", "Chargers"), ml_home=-345, ml_away=275,
                               abbrs={"home": "BUF", "away": "LAC"}), "nfl")
    m = mk_market("c1", "us:x-lac", "us:x-buf", hours=6)
    m.outcomes, m.sports_type = ["Los Angeles C", "Buffalo"], "moneyline"      # truncated title
    m.outcome_aliases = [["lac", "Los Angeles C"], ["buf", "Buffalo"]]
    matched = match_markets([m], [g], NOW)
    assert len(matched) == 1 and matched[0].sides == ["away", "home"]


def test_books_only_for_games_near_kickoff(tmp_path):
    c = cfg(tmp_path)
    soon = mk_market("s", "sy", "sn", hours=6)
    soon.question, soon.outcomes, soon.sports_type, soon.game_start = "Texans vs. Colts", ["Texans", "Colts"], "moneyline", NOW + timedelta(hours=6)
    far = mk_market("f", "fy", "fn", hours=100)
    far.question, far.outcomes, far.sports_type, far.game_start = "Bills vs. Jets", ["Bills", "Jets"], "moneyline", NOW + timedelta(hours=100)
    g1 = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), ml_home=-150, ml_away=130), "nfl")
    g2 = parse_event(espn_event("2", ("Buffalo", "Bills"), ("New York", "Jets"), ml_home=-150, ml_away=130, start=NOW + timedelta(hours=100)), "nfl")
    clob = FakeClob({t: mk_book(t, [(0.5, 10)], [(0.48, 10)]) for t in ("sy", "sn", "fy", "fn")})
    led = run_once(c, FakeGamma([soon, far]), clob, now=NOW, espn=FakeEspn([g1, g2]))
    assert led.scan["books"] == 2 and led.scan["sports.matched"] == 2


def test_live_guards_from_research(tmp_path):
    """Stable-snapshot requirement, late-game ramp, and contested final-minutes skip."""
    c = cfg(tmp_path)
    m = mk_market("c1", "hou", "ind", hours=2)
    m.question, m.outcomes, m.sports_type = "Texans vs. Colts", ["Texans", "Colts"], "moneyline"
    books = {"hou": mk_book("hou", [(0.78, 100)], [(0.76, 100)], fee=600), "ind": mk_book("ind", [(0.23, 100)], [(0.21, 100)], fee=600)}
    # Q2, Texans 88% live vs 78c ask: edge ~9c after fee, ramp small early -> trade
    live = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), state="in", hs=14, aws=0, wp=0.88), "nfl")
    live.period, live.clock = 2, "10:00"
    assert abs(live.elapsed_fraction() - (20 / 60)) < 1e-9 and abs(live.minutes_left() - 40) < 1e-9
    opps = find_sports_edges(match_markets([m], [live], NOW), books, c, NOW, 60.0, 30.0, set())
    assert len(opps) == 1 and opps[0].model_src == "live_wp"
    # same numbers but the snapshot was unstable (a play just happened): no trade
    live.stable = False
    st = Counter()
    assert find_sports_edges(match_markets([m], [live], NOW), books, c, NOW, 60.0, 30.0, set(), st) == [] and st["sports.unstable"] == 2
    live.stable = True
    # Q4 3:00 left, still contested (88% < 90% decided): blocked in the final minutes
    live.period, live.clock = 4, "3:00"
    st = Counter()
    assert find_sports_edges(match_markets([m], [live], NOW), books, c, NOW, 60.0, 30.0, set(), st) == [] and st["sports.late_contested"] == 2
    # Q4 8:00 left, contested: margin has ramped (5c + 7c * 0.87^2 ~ 10.3c) so a 9c edge no longer qualifies
    live.period, live.clock = 4, "8:00"
    st = Counter()
    assert find_sports_edges(match_markets([m], [live], NOW), books, c, NOW, 60.0, 30.0, set(), st) == [] and st["sports.no_edge"] >= 1
    # decided game (95%) late: the 3c bar applies and the final-minutes block does not
    live.home_wp, live.clock = 0.95, "2:00"
    books["hou"] = mk_book("hou", [(0.90, 100)], [(0.89, 100)], fee=600)
    assert len(find_sports_edges(match_markets([m], [live], NOW), books, c, NOW, 60.0, 30.0, set())) == 1


def test_stability_check_runs_when_a_matched_game_is_live(tmp_path):
    c = cfg(tmp_path)
    m = mk_market("c1", "hou", "ind", hours=2)
    m.question, m.outcomes, m.sports_type = "Texans vs. Colts", ["Texans", "Colts"], "moneyline"
    live = parse_event(espn_event("1", ("Houston", "Texans"), ("Indianapolis", "Colts"), state="in", hs=14, aws=0, wp=0.88), "nfl")
    live.period, live.clock = 2, "10:00"
    books = {"hou": mk_book("hou", [(0.78, 100)], [(0.76, 100)], fee=600), "ind": mk_book("ind", [(0.23, 100)], [(0.21, 100)], fee=600)}
    espn = FakeEspn([live], unstable_ids={"1"})
    led = run_once(c, FakeGamma([m]), FakeClob(books), now=NOW, espn=espn)
    assert espn.stability_calls == 1 and not led.positions and led.scan.get("sports.unstable") == 2
    espn = FakeEspn([live])
    led = run_once(c, FakeGamma([m]), FakeClob(books), now=NOW, espn=espn)
    assert len(led.positions) == 1 and led.trades[-1]["model_p"] == 0.88 and led.trades[-1]["model_src"] == "live_wp"
