"""Jev integration, tested with a fake decider (no network)."""
from datetime import timedelta

from polybot.ai_cache import AiCache
from polybot.config import Config
from polybot.decider import Assessment, _answers, _noul, _score, market_state, questions_for
from polybot.run import run_once
from polybot.strategies import find_ai_edges, find_harvests
from tests.fakes import NOW, FakeClob, FakeGamma, mk_book, mk_market


class FakeDecider:
    """Answers from a table: condition_id -> (p_yes, risk)."""
    def __init__(self, table):
        self.table = table
        self.calls = 0
        self.seen = []

    def assess_many(self, items, now):
        out = {}
        for m, prices in items:
            self.calls += 1
            self.seen.append((m.condition_id, prices is not None))
            p, r = self.table.get(m.condition_id, (None, None))
            out[m.condition_id] = Assessment(m.condition_id, p, r, 0.9, prices is not None,
                                             now.strftime("%Y-%m-%dT%H:%M:%SZ"), error="" if p is not None else "boom")
        return out


def cfg(tmp_path, **kw):
    c = Config(profile="aggressive")
    c.state_dir = str(tmp_path)
    c.kill_switch_file = str(tmp_path / "STOP")
    c.private_key = c.funder = ""
    c.openrouter_api_key = "sk-or-test"
    c.sports_only = False
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_response_parsing_variants():
    q = {"p_yes": {}, "risk": {}}
    direct = {"p_yes": {"type": "noul", "noul": 0.97}, "risk": {"type": "score", "score": 0.2, "confidence": 0.9}}
    assert _answers(direct, q) is direct
    wrapped = {"answers": direct}
    assert _answers(wrapped, q) is direct
    assert _noul({"noul": 0.97}) == 0.97 and _noul({"probabilities": {"true": 0.2}}) == 0.2 and _noul(None) is None
    assert _score({"score": 1.05, "confidence": 0.92}) == (1.05, 0.92)
    assert _score({"probabilities": {"0": 0.5, "1": 0.5}}) == (0.5, None)
    try:
        _answers({"error": {"message": "bad key"}}, q)
        assert False
    except ValueError as e:
        assert "bad key" in str(e)


def test_state_and_questions_shape():
    m = mk_market("c", "y", "n", 10)
    m.description = "Resolves YES if X."
    st = market_state(m, NOW, {"Yes": 0.7, "No": 0.3})
    assert st["current_market_prices"] == {"Yes": 0.7, "No": 0.3} and st["resolution_rules"] == "Resolves YES if X."
    assert "current_market_prices" not in market_state(m, NOW, None)
    qs = questions_for(m)
    assert qs["p_yes"]["type"] == "noul" and set(qs["p_yes"]["criteria"]) == {"true", "false"}
    assert qs["risk"]["type"] == "score" and len(qs["risk"]["criteria"]) == 3


def test_harvest_gate_vetoes_and_ranks(tmp_path):
    c = cfg(tmp_path)
    a = mk_market("a", "ay", "an", 10)
    b = mk_market("b", "by", "bn", 10)
    books = {t: mk_book(t, [(0.96, 50)], [(0.955, 50)]) for t in ("ay", "by")}
    books.update({t: mk_book(t, [(0.05, 50)], [(0.04, 50)]) for t in ("an", "bn")})
    gate = {"a": Assessment("a", 0.93, 0.3, 0.9, True, "t"), "b": Assessment("b", 0.99, 0.2, 0.9, True, "t")}
    opps = find_harvests([a, b], books, c, NOW, 5.0, set(), assessments=gate)
    assert [o.market.condition_id for o in opps] == ["b", "a"]            # surest first
    gate["a"] = Assessment("a", 0.4, 0.3, 0.9, True, "t")                  # Jev leans the other way
    assert [o.market.condition_id for o in find_harvests([a, b], books, c, NOW, 5.0, set(), assessments=gate)] == ["b"]
    gate["b"] = Assessment("b", 0.99, 1.9, 0.9, True, "t")                 # ambiguous rules
    assert find_harvests([a, b], books, c, NOW, 5.0, set(), assessments=gate) == []
    # no assessment at all -> unchanged behaviour
    assert len(find_harvests([a, b], books, c, NOW, 5.0, set(), assessments={})) == 2


def test_ai_edge_finder_rules(tmp_path):
    c = cfg(tmp_path)
    m = mk_market("m", "my", "mn", 48)
    books = {"my": mk_book("my", [(0.70, 100)], [(0.68, 100)]), "mn": mk_book("mn", [(0.31, 100)], [(0.29, 100)])}
    blind = {"m": Assessment("m", 0.97, 0.5, 0.9, False, "t")}
    opps = find_ai_edges([m], books, blind, c, NOW, 8.0, set())
    assert len(opps) == 1 and opps[0].kind == "ai_edge" and opps[0].legs[0].outcome == "Yes"
    assert opps[0].legs[0].size == 11 and abs(opps[0].edge - 0.27) < 1e-9
    # Jev favours NO strongly but NO is priced at 31c: below the min price band, skip
    assert find_ai_edges([m], books, {"m": Assessment("m", 0.02, 0.5, 0.9, False, "t")}, c, NOW, 8.0, set()) == []
    # mid-range answers are ignored; assessments that saw the price are ignored
    assert find_ai_edges([m], books, {"m": Assessment("m", 0.75, 0.5, 0.9, False, "t")}, c, NOW, 8.0, set()) == []
    assert find_ai_edges([m], books, {"m": Assessment("m", 0.97, 0.5, 0.9, True, "t")}, c, NOW, 8.0, set()) == []
    # ambiguous rules
    assert find_ai_edges([m], books, {"m": Assessment("m", 0.97, 1.8, 0.9, False, "t")}, c, NOW, 8.0, set()) == []


def test_cache_roundtrip_and_ttl(tmp_path):
    p = str(tmp_path / "ai_cache.json")
    cache = AiCache(p, ttl_hours=1.0)
    a = Assessment("c", 0.9, 0.1, 0.9, False, NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    cache.put(a)
    cache.put(Assessment("e", None, None, None, False, "2026-09-26T12:00:00Z", error="x"))   # errors not cached
    cache.save(NOW)
    c2 = AiCache(p, 1.0)
    assert c2.get("c", False, NOW + timedelta(minutes=30)).p_yes == 0.9
    assert c2.get("c", True, NOW) is None                      # different with_prices key
    assert c2.get("c", False, NOW + timedelta(hours=2)) is None
    assert c2.get("e", False, NOW) is None


def test_full_cycle_with_jev_and_grading(tmp_path):
    c = cfg(tmp_path, max_position_frac=0.5)      # leave room for both trades in one cycle
    fav = mk_market("fav", "fy", "fn", hours=6)                    # favourite Jev approves
    dud = mk_market("dud", "dy", "dn", hours=6)                    # favourite Jev rejects
    mid = mk_market("mid", "my", "mn", hours=48)                   # blind edge
    for m in (fav, dud, mid):
        m.description = "Rules text."
    books = {
        "fy": mk_book("fy", [(0.96, 50)], [(0.955, 50)]), "fn": mk_book("fn", [(0.05, 50)], [(0.04, 50)]),
        "dy": mk_book("dy", [(0.96, 50)], [(0.955, 50)]), "dn": mk_book("dn", [(0.05, 50)], [(0.04, 50)]),
        "my": mk_book("my", [(0.70, 100)], [(0.68, 100)]), "mn": mk_book("mn", [(0.31, 100)], [(0.29, 100)]),
    }
    dec = FakeDecider({"fav": (0.99, 0.2), "dud": (0.60, 0.3), "mid": (0.97, 0.4)})
    led = run_once(c, FakeGamma([fav, dud, mid]), FakeClob(books), now=NOW, decider=dec)
    kinds = {p.condition_id: p.kind for p in led.positions.values()}
    assert kinds.get("mid") == "ai_edge" and kinds.get("fav") == "harvest" and "dud" not in kinds
    assert dec.calls == 3 and sorted(dec.seen) == [("dud", True), ("fav", True), ("mid", False)]
    assert led.ai_log["mid"]["p_yes"] == 0.97 and led.ai_log["mid"]["outcome"] is None
    assert led.ai_info["edge_assessed"] == 1 and led.ai_info["gate_assessed"] == 2
    report = open(tmp_path / "report.md").read()
    assert "Jev (AI decider)" in report and "ai_cache.json" in str(list(tmp_path.iterdir()))

    # second run: cache serves the same markets, no new calls; then everything resolves YES
    led = run_once(c, FakeGamma([fav, dud, mid]), FakeClob(books), now=NOW + timedelta(minutes=20), decider=dec)
    assert dec.calls == 3
    resolved = [mk_market(cid, y, n, hours=-1, closed=True, prices=(1.0, 0.0)) for cid, y, n in
                (("fav", "fy", "fn"), ("dud", "dy", "dn"), ("mid", "my", "mn"))]
    led = run_once(c, FakeGamma(resolved), FakeClob({}), now=NOW + timedelta(days=3), decider=dec)
    assert not led.positions and led.realized_pnl > 0
    assert led.ai_log["mid"]["outcome"] == 1
    assert led.ai_calibration()[">=0.95"] == {"n": 1, "acc": 1.0}


def test_circuit_breaker_pauses_edge_trades(tmp_path):
    c = cfg(tmp_path, ai_min_samples_for_breaker=2)
    mid = mk_market("mid", "my", "mn", hours=48)
    mid.description = "Rules."
    books = {"my": mk_book("my", [(0.70, 100)], [(0.68, 100)]), "mn": mk_book("mn", [(0.31, 100)], [(0.29, 100)])}
    dec = FakeDecider({"mid": (0.97, 0.4)})
    # seed a bad track record: two extreme calls that were wrong
    from polybot.ledger import Ledger
    led = Ledger.load(str(tmp_path / "ledger.json"), 25.0, "paper")
    led.log_ai("x1", "q", 0.98, 0.7, 0.2, False, "t"); led.resolve_ai("x1", yes_won=False)
    led.log_ai("x2", "q", 0.03, 0.3, 0.2, False, "t"); led.resolve_ai("x2", yes_won=True)
    led.save(str(tmp_path / "ledger.json"))
    led = run_once(c, FakeGamma([mid]), FakeClob(books), now=NOW, decider=dec)
    assert led.ai_info["breaker"] is True and not led.positions


def test_live_game_gate_is_never_served_from_cache(tmp_path):
    """A cached pregame opinion must not veto a live game: Wisconsin was cached at 5% before taking the lead."""
    from collections import Counter
    from polybot.ai_cache import AiCache
    from polybot.ledger import Ledger
    from polybot.run import _assess_with_jev
    c = cfg(tmp_path)
    m = mk_market("g1", "gy", "gn", hours=1)
    m.game_start = NOW
    books = {"gy": mk_book("gy", [(0.92, 100)], [(0.90, 100)]), "gn": mk_book("gn", [(0.10, 100)], [(0.08, 100)])}
    stale = Assessment("g1", 0.05, 0.1, 0.9, True, (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    cache = AiCache(str(tmp_path / "ai_cache.json"), c.ai_cache_hours)
    cache.put(stale)
    cache.save(NOW)
    dec = FakeDecider({"g1": (0.97, 0.1)})
    led = Ledger()
    notes = {"g1": "Penn State 20 @ Wisconsin 24, Q4 1:13, home wp 0.98"}
    # pregame / not live: the cached answer is used, no call
    gate, _, _ = _assess_with_jev([m], books, c, NOW, dec, led, Counter(), set(), game_notes=notes, blind=False)
    assert dec.calls == 0 and gate["g1"].p_yes == 0.05
    # live: asked afresh, and the fresh answer is not written back over the cache for the next cycle
    gate, _, _ = _assess_with_jev([m], books, c, NOW, dec, led, Counter(), set(), game_notes=notes, blind=False, no_cache={"g1"})
    assert dec.calls == 1 and gate["g1"].p_yes == 0.97
    assert AiCache(str(tmp_path / "ai_cache.json"), c.ai_cache_hours).get("g1", True, NOW).p_yes == 0.05
