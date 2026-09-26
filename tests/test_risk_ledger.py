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
    c = Config(profile="conservative")
    rm = RiskManager(c)
    st = RiskState(cash=25, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.spendable(st) == 12.0                     # MAX_SPEND_PER_RUN wins
    st = RiskState(cash=3, deployed=22, equity=25, open_positions=0, pnl_today=0)
    assert rm.spendable(st) == max(0.0, 0.85 * 25 - 22)  # deploy cap binds (0.25)
    st = RiskState(cash=2.5, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.spendable(st) == 0.5                       # cash reserve binds


def test_approve_caps_directional_but_lets_arb_use_more():
    c = Config(profile="conservative")
    rm = RiskManager(c)
    st = RiskState(cash=25, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.approve(opp("harvest", 5.0), st) is None
    assert "per-position" in rm.approve(opp("harvest", 6.0), st)
    assert rm.approve(opp("pair_arb", 10.0, legs=2), st) is None
    assert "exceeds spendable" in rm.approve(opp("pair_arb", 13.0, legs=2), st)
    st.open_positions = c.max_open_positions
    assert rm.approve(opp("harvest", 5.0), st) == "max open positions"


def test_halts(tmp_path):
    c = Config(profile="conservative")
    c.kill_switch_file = str(tmp_path / "STOP")
    rm = RiskManager(c)
    ok = RiskState(cash=25, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.halted(ok) is None
    assert "daily loss" in rm.halted(RiskState(25, 0, 25, 0, pnl_today=-3.0))
    assert "floor" in rm.halted(RiskState(10, 0, 10, 0, 0))
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


def test_profiles(monkeypatch):
    monkeypatch.delenv("MAX_POSITION_FRAC", raising=False)
    a = Config(profile="aggressive")
    assert a.max_position_frac == 0.5 and a.cash_reserve == 0 and a.stop_loss_drop == 0 and a.harvest_min_price == 0.80
    c = Config(profile="conservative")
    assert c.max_position_frac == 0.2 and c.stop_loss_drop == 0.25 and c.min_equity_frac == 0.5
    monkeypatch.setenv("MAX_POSITION_FRAC", "0.3")       # explicit env var beats the profile
    assert Config(profile="aggressive").max_position_frac == 0.3
    monkeypatch.setenv("RISK_PROFILE", "aggressive")
    assert Config().profile == "aggressive"
    import pytest
    with pytest.raises(ValueError):
        Config(profile="yolo")


def test_aggressive_sizing_and_halts():
    a = Config(profile="aggressive")
    rm = RiskManager(a)
    st = RiskState(cash=25, deployed=0, equity=25, open_positions=0, pnl_today=0)
    assert rm.spendable(st) == 25 and rm.per_position_budget(st) == 12.5
    assert rm.approve(opp("harvest", 12.5), st) is None
    assert rm.halted(RiskState(cash=13, deployed=0, equity=13, open_positions=0, pnl_today=-12)) is None   # one loss doesn't freeze it
    assert "floor" in rm.halted(RiskState(cash=4, deployed=0, equity=4, open_positions=0, pnl_today=0))


def test_wallet_derivation_is_deterministic_and_well_formed():
    from polybot.wallet import candidate_funders, derive_proxy_wallet, derive_safe_wallet, signer_address
    key = "0x" + "11" * 32
    s = signer_address(key)
    assert s == signer_address("11" * 32) == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    c = candidate_funders(key)
    assert c[0] == s and c[1] == derive_proxy_wallet(s) and c[2] == derive_safe_wallet(s)
    assert len({c[0], c[1], c[2]}) == 3 and all(len(a) == 42 and a.startswith("0x") for a in c.values())


def test_create2_matches_eip1014_vectors():
    from eth_utils import keccak
    from polybot.wallet import _create2
    # Examples from EIP-1014
    assert _create2("0x0000000000000000000000000000000000000000", bytes(32), keccak(bytes.fromhex("00"))) == \
        "0x4D1A2e2bB4F88F0250f26Ffff098B0b30B26BF38"
    assert _create2("0xdeadbeef00000000000000000000000000000000", bytes(32), keccak(bytes.fromhex("00"))) == \
        "0xB928f69Bb1D91Cd65274e3c79d8986362984fDA3"
    salt = bytes.fromhex("000000000000000000000000feed000000000000000000000000000000000000")
    assert _create2("0xdeadbeef00000000000000000000000000000000", salt, keccak(bytes.fromhex("00"))) == \
        "0xD04116cDd17beBE565EB2422F2497E06cC1C9833"


def test_private_key_normalization_messages():
    import pytest
    from polybot.wallet import normalize_private_key
    assert normalize_private_key("  AB" * 32 + " ") == "0x" + "ab" * 32
    assert normalize_private_key("0x" + "ab" * 32) == "0x" + "ab" * 32
    with pytest.raises(ValueError, match="API key"):
        normalize_private_key("019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8")
    with pytest.raises(ValueError, match="seed phrase"):
        normalize_private_key("word " * 12)
    with pytest.raises(ValueError, match="64 hex"):
        normalize_private_key("deadbeef")
