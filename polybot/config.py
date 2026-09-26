"""Configuration, read from environment variables so it runs unattended in CI."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # --- credentials (absent => paper mode) ---
    private_key: str = field(default_factory=lambda: os.environ.get("POLYMARKET_PRIVATE_KEY", ""))
    funder: str = field(default_factory=lambda: os.environ.get("POLYMARKET_FUNDER", ""))
    # 0 = EOA (browser wallet), 1 = Polymarket email/Magic proxy, 2 = Gnosis safe proxy
    signature_type: int = field(default_factory=lambda: _i("POLYMARKET_SIGNATURE_TYPE", 1))
    live_enabled: bool = field(default_factory=lambda: _b("LIVE", True))

    # --- endpoints ---
    clob_host: str = field(default_factory=lambda: os.environ.get("CLOB_HOST", "https://clob.polymarket.com"))
    gamma_host: str = field(default_factory=lambda: os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com"))
    data_host: str = field(default_factory=lambda: os.environ.get("DATA_HOST", "https://data-api.polymarket.com"))
    chain_id: int = 137

    # --- bankroll & risk (the "balance spend and make money" knobs) ---
    starting_bankroll: float = field(default_factory=lambda: _f("BANKROLL_USD", 25.0))
    cash_reserve: float = field(default_factory=lambda: _f("CASH_RESERVE_USD", 2.0))
    max_position_frac: float = field(default_factory=lambda: _f("MAX_POSITION_FRAC", 0.20))
    max_deployed_frac: float = field(default_factory=lambda: _f("MAX_DEPLOYED_FRAC", 0.85))
    max_spend_per_run: float = field(default_factory=lambda: _f("MAX_SPEND_PER_RUN", 12.0))
    max_open_positions: int = field(default_factory=lambda: _i("MAX_OPEN_POSITIONS", 8))
    daily_loss_limit_frac: float = field(default_factory=lambda: _f("DAILY_LOSS_LIMIT_FRAC", 0.10))

    # --- strategy: pair arbitrage (YES ask + NO ask < 1) ---
    arb_min_edge: float = field(default_factory=lambda: _f("ARB_MIN_EDGE", 0.006))
    arb_min_profit_usd: float = field(default_factory=lambda: _f("ARB_MIN_PROFIT_USD", 0.02))
    arb_max_days_to_resolution: float = field(default_factory=lambda: _f("ARB_MAX_DAYS", 21.0))
    enable_negrisk_arb: bool = field(default_factory=lambda: _b("ENABLE_NEGRISK_ARB", False))

    # --- strategy: near-resolution favorite harvesting ---
    harvest_enabled: bool = field(default_factory=lambda: _b("HARVEST_ENABLED", True))
    harvest_min_price: float = field(default_factory=lambda: _f("HARVEST_MIN_PRICE", 0.94))
    harvest_max_price: float = field(default_factory=lambda: _f("HARVEST_MAX_PRICE", 0.985))
    harvest_max_hours: float = field(default_factory=lambda: _f("HARVEST_MAX_HOURS", 72.0))
    harvest_min_liquidity: float = field(default_factory=lambda: _f("HARVEST_MIN_LIQUIDITY", 3000.0))
    harvest_min_volume24h: float = field(default_factory=lambda: _f("HARVEST_MIN_VOLUME24H", 500.0))
    harvest_max_spread: float = field(default_factory=lambda: _f("HARVEST_MAX_SPREAD", 0.03))
    harvest_min_annualized: float = field(default_factory=lambda: _f("HARVEST_MIN_ANNUALIZED", 1.5))
    # assumed underpricing of heavy favourites (favourite-longshot bias), net of resolution risk
    harvest_assumed_edge: float = field(default_factory=lambda: _f("HARVEST_ASSUMED_EDGE", 0.015))

    # --- scanning ---
    scan_limit: int = field(default_factory=lambda: _i("SCAN_LIMIT", 600))
    book_batch: int = 40
    max_orders_per_run: int = field(default_factory=lambda: _i("MAX_ORDERS_PER_RUN", 6))

    # --- files ---
    state_dir: str = field(default_factory=lambda: os.environ.get("STATE_DIR", "state"))
    kill_switch_file: str = field(default_factory=lambda: os.environ.get("KILL_SWITCH_FILE", "state/STOP"))

    @property
    def is_live(self) -> bool:
        return bool(self.private_key and self.funder and self.live_enabled)
