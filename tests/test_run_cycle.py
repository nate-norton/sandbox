"""End-to-end paper cycles against fake APIs: trades, settlement, stop-loss, halts."""
import json
import os
from datetime import timedelta

from polybot.config import Config
from polybot.run import run_once
from tests.fakes import NOW, FakeClob, FakeGamma, mk_book, mk_market


def make_cfg(tmp_path):
    c = Config()
    c.state_dir = str(tmp_path)
    c.kill_switch_file = str(tmp_path / "STOP")
    c.private_key = c.funder = ""
    return c


def test_paper_cycle_trades_then_settles(tmp_path):
    c = make_cfg(tmp_path)
    arb = mk_market("arb", "ay", "an", hours=30)
    fav = mk_market("fav", "fy", "fn", hours=6)
    books = {
        "ay": mk_book("ay", [(0.55, 10)], [(0.54, 10)]), "an": mk_book("an", [(0.43, 10)], [(0.42, 10)]),
        "fy": mk_book("fy", [(0.96, 40)], [(0.955, 40)]), "fn": mk_book("fn", [(0.045, 40)], [(0.04, 40)]),
    }
    c.max_spend_per_run = 20.0       # default $12/run would (correctly) block the harvest after the arb
    led = run_once(c, FakeGamma([arb, fav]), FakeClob(books), now=NOW)
    kinds = sorted(p.kind for p in led.positions.values())
    assert kinds == ["harvest", "pair_arb", "pair_arb"]
    assert abs(led.cash - (25 - 10 * 0.98 - 5 * 0.96)) < 1e-6
    assert os.path.exists(tmp_path / "ledger.json") and os.path.exists(tmp_path / "report.md")

    # next run: both markets resolved (Yes won), so positions settle for $1/share on Yes, $0 on No
    arb2 = mk_market("arb", "ay", "an", hours=-1, closed=True, prices=(1.0, 0.0))
    fav2 = mk_market("fav", "fy", "fn", hours=-1, closed=True, prices=(1.0, 0.0))
    led = run_once(c, FakeGamma([arb2, fav2]), FakeClob({}), now=NOW + timedelta(days=2))
    assert not led.positions
    assert abs(led.cash - (25 + 10 * 0.02 + 5 * 0.04)) < 1e-6
    assert led.realized_pnl > 0
    d = json.load(open(tmp_path / "ledger.json"))
    assert d["runs"] == 2 and d["mode"] == "paper"


def test_stop_loss_and_kill_switch(tmp_path):
    c = make_cfg(tmp_path)
    fav = mk_market("fav", "fy", "fn", hours=30)
    books = {"fy": mk_book("fy", [(0.96, 40)], [(0.955, 40)]), "fn": mk_book("fn", [(0.045, 40)], [(0.04, 40)])}
    led = run_once(c, FakeGamma([fav]), FakeClob(books), now=NOW)
    assert len(led.positions) == 1
    # price collapses: stop-loss sells at the bid
    books2 = {"fy": mk_book("fy", [(0.60, 40)], [(0.55, 40)]), "fn": mk_book("fn", [(0.45, 40)], [(0.40, 40)])}
    led = run_once(c, FakeGamma([fav]), FakeClob(books2), now=NOW + timedelta(hours=1))
    assert not led.positions and led.realized_pnl < 0
    # daily loss limit (10% of $25 = $2.50; we lost 5 * 0.41 = $2.05) not hit, so kill switch instead
    open(c.kill_switch_file, "w").close()
    led = run_once(c, FakeGamma([fav]), FakeClob(books), now=NOW + timedelta(hours=2))
    assert any("kill switch" in n for n in led.notes) and not led.positions


def test_partial_arb_is_unwound(tmp_path, monkeypatch):
    c = make_cfg(tmp_path)
    arb = mk_market("arb", "ay", "an", hours=30)
    books = {"ay": mk_book("ay", [(0.55, 10)], [(0.54, 10)]), "an": mk_book("an", [(0.43, 10)], [(0.42, 10)])}
    # second leg vanishes between scan and fill
    from polybot import executor as ex_mod
    real = ex_mod.Executor._fill_buy
    def flaky(self, leg, tick, neg_risk):
        return (0.0, 0.0, None) if leg.token_id == "an" else real(self, leg, tick, neg_risk)
    monkeypatch.setattr(ex_mod.Executor, "_fill_buy", flaky)
    led = run_once(c, FakeGamma([arb]), FakeClob(books), now=NOW)
    assert not led.positions                                    # YES leg sold back at the bid
    assert abs(led.realized_pnl - (-0.01 * 10)) < 1e-9          # paid 0.55, sold 0.54
    assert abs(led.cash - 24.9) < 1e-9
