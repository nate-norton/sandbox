from polybot.config import Config
from polybot.strategies import find_harvests, find_negrisk_arbs, find_pair_arbs
from tests.fakes import NOW, mk_book, mk_market


def cfg(**kw):
    c = Config(profile="conservative")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_pair_arb_found_and_sized_to_depth_and_budget():
    m = mk_market("c1", "y", "n", hours=48)
    books = {"y": mk_book("y", asks=[(0.60, 30)], bids=[(0.59, 10)]),
             "n": mk_book("n", asks=[(0.38, 12)], bids=[(0.37, 10)])}
    opps = find_pair_arbs([m], books, cfg(), NOW, budget=100)
    assert len(opps) == 1
    o = opps[0]
    assert o.kind == "pair_arb"
    assert o.legs[0].size == o.legs[1].size == 12          # limited by NO depth
    assert abs(o.edge - 0.02) < 1e-9
    assert abs(o.expected_profit - 0.24) < 1e-9
    # budget-limited
    o2 = find_pair_arbs([m], books, cfg(), NOW, budget=5.0)[0]
    assert o2.legs[0].size == 5                             # floor(5 / 0.98)
    assert find_pair_arbs([m], books, cfg(), NOW, budget=4.0) == []   # below min order size


def test_pair_arb_rejects_when_no_edge_or_fees_or_too_far():
    m = mk_market("c1", "y", "n", hours=48)
    books = {"y": mk_book("y", asks=[(0.60, 30)], bids=[]), "n": mk_book("n", asks=[(0.40, 30)], bids=[])}
    assert find_pair_arbs([m], books, cfg(), NOW, 100) == []
    books["n"] = mk_book("n", asks=[(0.38, 30)], bids=[], fee=600)     # 6% taker fee wipes a 2c edge
    assert find_pair_arbs([m], books, cfg(), NOW, 100) == []
    far = mk_market("c2", "y", "n", hours=24 * 60)
    books["n"] = mk_book("n", asks=[(0.38, 30)], bids=[])
    assert find_pair_arbs([far], books, cfg(), NOW, 100) == []


def test_negrisk_arb_buys_every_outcome():
    a = mk_market("a", "ya", "na", 24, neg_risk=True, event_id="ev")
    b = mk_market("b", "yb", "nb", 24, neg_risk=True, event_id="ev")
    c = mk_market("c", "yc", "nc", 24, neg_risk=True, event_id="ev")
    books = {"ya": mk_book("ya", [(0.50, 20)], []), "yb": mk_book("yb", [(0.30, 20)], []), "yc": mk_book("yc", [(0.15, 20)], [])}
    opps = find_negrisk_arbs([a, b, c], books, cfg(), NOW, 100)
    assert len(opps) == 1 and len(opps[0].legs) == 3
    assert abs(opps[0].edge - 0.05) < 1e-9
    assert opps[0].legs[0].size == 20


def test_harvest_filters_and_expected_value():
    c = cfg()
    good = mk_market("g", "gy", "gn", hours=12)
    books = {"gy": mk_book("gy", asks=[(0.96, 50)], bids=[(0.95, 50)]), "gn": mk_book("gn", asks=[(0.05, 50)], bids=[(0.04, 50)])}
    opps = find_harvests([good], books, c, NOW, per_position_budget=5.0, held=set())
    assert len(opps) == 1
    o = opps[0]
    assert o.legs[0].outcome == "Yes" and o.legs[0].size == 5 and abs(o.cost - 4.8) < 1e-9
    assert o.expected_profit > 0
    # already held -> skipped
    assert find_harvests([good], books, c, NOW, 5.0, held={"g"}) == []
    # too far out
    assert find_harvests([mk_market("g", "gy", "gn", hours=200)], books, c, NOW, 5.0, set()) == []
    # illiquid
    assert find_harvests([mk_market("g", "gy", "gn", 12, liq=100)], books, c, NOW, 5.0, set()) == []
    # fee-bearing market: allowed up to the cap, fee comes out of the expected value
    books["gy"].fee_bps = 1000
    o_fee = find_harvests([good], books, c, NOW, 5.0, set())[0]
    assert abs(o_fee.cost - (4.8 + 0.1 * 5 * 0.96 * 0.04)) < 1e-9 and o_fee.expected_profit < o.expected_profit
    books["gy"].fee_bps = 2000
    assert find_harvests([good], books, c, NOW, 5.0, set()) == []
    books["gy"].fee_bps = 0
    # wide spread
    books["gy"] = mk_book("gy", asks=[(0.96, 50)], bids=[(0.90, 50)])
    assert find_harvests([good], books, c, NOW, 5.0, set()) == []
    # price outside window
    books["gy"] = mk_book("gy", asks=[(0.99, 50)], bids=[(0.985, 50)])
    assert find_harvests([good], books, c, NOW, 5.0, set()) == []


def test_harvest_prefers_soonest_resolution():
    c = cfg()
    m1 = mk_market("m1", "a", "b", hours=40)
    m2 = mk_market("m2", "c", "d", hours=5)
    books = {t: mk_book(t, asks=[(0.96, 50)], bids=[(0.955, 50)]) for t in "ac"}
    books.update({t: mk_book(t, asks=[(0.05, 50)], bids=[(0.04, 50)]) for t in "bd"})
    opps = find_harvests([m1, m2], books, c, NOW, 5.0, set())
    assert [o.market.condition_id for o in opps] == ["m2", "m1"]


def test_fee_estimate_uses_cheaper_side():
    from polybot.strategies import est_fee
    assert abs(est_fee(1000, 100, 0.96) - 0.384) < 1e-9     # ~0.4% of $96 notional
    assert abs(est_fee(1000, 100, 0.04) - 0.384) < 1e-9
    assert abs(est_fee(1000, 100, 0.50) - 2.5) < 1e-9
    assert est_fee(0, 100, 0.5) == 0
