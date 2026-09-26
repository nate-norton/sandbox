import os

from polybot.config import Config
from polybot.ledger import Ledger
from polybot.models import Leg, Opportunity
from polybot.risk import RiskManager, RiskState, state_from_ledger
from tests.fakes import mk_market


def opp(kind, cost, profit=0.1, legs=1):
    m = mk_market("x", "y", "n", 10)
    return Opportunity(kind, m, [Leg("y", "BUY", 0.5, cost / 0.5 / legs)] * legs, cost, profit, 0.02)


def test_spendable_respects_reserve_deploy_cap_and_run_cap():
    c = Config()
    rm = RiskManager(c)
    st = RiskState(cash=25, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.spendable(st) == 12.0                     # MAX_SPEND_PER_RUN wins
    st = RiskState(cash=3, deployed=22, equity=25, open_positions=0, pnl_today=0)
    assert rm.spendable(st) == max(0.0, 0.85 * 25 - 22)  # deploy cap binds (0.25)
    st = RiskState(cash=2.5, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.spendable(st) == 0.5                       # cash reserve binds


def test_approve_caps_directional_but_lets_arb_use_more():
    c = Config()
    rm = RiskManager(c)
    st = RiskState(cash=25, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.approve(opp("harvest", 5.0), st) is None
    assert "per-position" in rm.approve(opp("harvest", 6.0), st)
    assert rm.approve(opp("pair_arb", 10.0, legs=2), st) is None
    assert "exceeds spendable" in rm.approve(opp("pair_arb", 13.0, legs=2), st)
    st.open_positions = c.max_open_positions
    assert rm.approve(opp("harvest", 5.0), st) == "max open positions"


def test_halts(tmp_path):
    c = Config()
    c.kill_switch_file = str(tmp_path / "STOP")
    rm = RiskManager(c)
    ok = RiskState(cash=25, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.halted(ok) is None
    assert "daily loss" in rm.halted(RiskState(25, 0, 25, 0, pnl_today=-3.0))
    assert "half" in rm.halted(RiskState(10, 0, 10, 0, 0))
    open(c.kill_switch_file, "w").close()
    assert "kill switch" in rm.halted(ok)


def test_ledger_roundtrip_and_accounting(tmp_path):
    p = str(tmp_path / "ledger.json")
    led = Ledger.load(p, 25.0, "paper")
    led.record_buy("y", "c", "Q", "Yes", 5, 0.96, None, "harvest", False)
    assert abs(led.cash - 20.2) < 1e-9 and abs(led.deployed - 4.8) < 1e-9
    led.record_buy("y", "c", "Q", "Yes", 5, 0.94, None, "harvest", False)
    assert abs(led.positions["y"].avg_price - 0.95) < 1e-9 and led.positions["y"].size == 10
    pnl = led.settle("y", 1.0)
    assert abs(pnl - 0.5) < 1e-9 and abs(led.cash - 25.5) < 1e-9 and not led.positions
    assert abs(led.pnl_today() - 0.5) < 1e-9
    led.save(p)
    led2 = Ledger.load(p, 25.0, "paper")
    assert abs(led2.cash - 25.5) < 1e-9 and len(led2.trades) == 3
    st = state_from_ledger(led2)
    assert st.equity == led2.cash
    # switching to live drops simulated state but keeps history
    led3 = Ledger.load(p, 25.0, "live")
    assert led3.mode == "live" and led3.cash == 25.0 and len(led3.trades) == 3 and led3.notes


def test_partial_sell_books_pnl(tmp_path):
    led = Ledger.load(str(tmp_path / "l.json"), 25.0, "paper")
    led.record_buy("y", "c", "Q", "Yes", 10, 0.50, None, "pair_arb", False)
    pnl = led.record_sell("y", 4, 0.60, "unwind")
    assert abs(pnl - 0.4) < 1e-9 and led.positions["y"].size == 6


def test_filled_from_response_by_side():
    from polybot.clob import _filled_from_response as f
    ok = {"success": True, "status": "matched", "makingAmount": "4.8", "takingAmount": "5"}
    assert f(ok, 5, "BUY") == 5 and f(ok, 5, "SELL") == 4.8
    assert f({"success": True, "status": "unmatched"}, 5, "BUY") == 0
    assert f({"success": False, "errorMsg": "not enough balance"}, 5, "BUY") == 0
    assert f({"success": True, "status": "matched"}, 5, "BUY") == 5
