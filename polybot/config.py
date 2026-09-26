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


# Risk profiles. Values apply unless the matching env var is set explicitly.
PROFILES: dict[str, dict] = {
    "conservative": {},
    "aggressive": {
        "cash_reserve": 0.0,
        "max_position_frac": 0.50,
        "max_deployed_frac": 1.0,
        "max_spend_per_run": 1000.0,
        "daily_loss_limit_frac": 0.60,
        "min_equity_frac": 0.20,
        "stop_loss_drop": 0.0,             # disabled: a favourite that breaks usually goes to zero anyway
        "harvest_min_price": 0.80,
        "harvest_max_spread": 0.04,
        "ai_edge_position_frac": 0.35,
        "sports_kelly_frac": 1.0,             # full Kelly: fastest long-run compounding the math allows
    },
}

ENV_NAMES = {
    "cash_reserve": "CASH_RESERVE_USD", "max_position_frac": "MAX_POSITION_FRAC",
    "max_deployed_frac": "MAX_DEPLOYED_FRAC", "max_spend_per_run": "MAX_SPEND_PER_RUN",
    "daily_loss_limit_frac": "DAILY_LOSS_LIMIT_FRAC", "min_equity_frac": "MIN_EQUITY_FRAC",
    "stop_loss_drop": "STOP_LOSS_DROP", "harvest_min_price": "HARVEST_MIN_PRICE",
    "harvest_max_spread": "HARVEST_MAX_SPREAD", "ai_edge_position_frac": "AI_EDGE_POSITION_FRAC",
    "sports_kelly_frac": "SPORTS_KELLY_FRAC",
}


@dataclass
class Config:
    # --- risk profile: "aggressive" (default) or "conservative" ---
    profile: str = field(default_factory=lambda: os.environ.get("RISK_PROFILE", "aggressive").strip().lower())

    # --- exchange: "us" (polymarket.us, USD account, Ed25519 API key) or "global" (polymarket.com CLOB) ---
    exchange: str = field(default_factory=lambda: os.environ.get("EXCHANGE", "us").strip().lower())
    us_key_id: str = field(default_factory=lambda: os.environ.get("POLYMARKET_US_KEY_ID", ""))
    us_secret: str = field(default_factory=lambda: os.environ.get("POLYMARKET_US_SECRET", ""))

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
    min_equity_frac: float = field(default_factory=lambda: _f("MIN_EQUITY_FRAC", 0.50))     # halt below this
    stop_loss_drop: float = field(default_factory=lambda: _f("STOP_LOSS_DROP", 0.25))       # 0 disables

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
    harvest_max_fee_bps: int = field(default_factory=lambda: _i("HARVEST_MAX_FEE_BPS", 1000))
    harvest_min_annualized: float = field(default_factory=lambda: _f("HARVEST_MIN_ANNUALIZED", 1.5))
    # assumed underpricing of heavy favourites (favourite-longshot bias), net of resolution risk
    harvest_assumed_edge: float = field(default_factory=lambda: _f("HARVEST_ASSUMED_EDGE", 0.015))

    # --- AI decider (Jev via OpenRouter); absent key => AI features off ---
    openrouter_api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    ai_model: str = field(default_factory=lambda: os.environ.get("OPENROUTER_MODEL", "~typesafe/jev-latest"))
    ai_enabled: bool = field(default_factory=lambda: _b("AI_ENABLED", True))
    ai_max_markets_per_run: int = field(default_factory=lambda: _i("AI_MAX_MARKETS_PER_RUN", 60))
    ai_cache_hours: float = field(default_factory=lambda: _f("AI_CACHE_HOURS", 1.0))
    # gate: veto a favourite only when Jev leans the other way (its probabilities hedge toward 0.5,
    # so 0.5 means "Jev disagrees with the market", not "coin flip"); rules must not be ambiguous
    ai_gate_min_p: float = field(default_factory=lambda: _f("AI_GATE_MIN_P", 0.50))
    ai_gate_max_risk: float = field(default_factory=lambda: _f("AI_GATE_MAX_RISK", 1.4))
    # edge finder: only Jev's rare strong answers count; blind answers cluster near 0.5 in practice
    ai_edge_min_p: float = field(default_factory=lambda: _f("AI_EDGE_MIN_P", 0.85))
    ai_edge_min_gap: float = field(default_factory=lambda: _f("AI_EDGE_MIN_GAP", 0.10))
    ai_edge_min_price: float = field(default_factory=lambda: _f("AI_EDGE_MIN_PRICE", 0.50))
    ai_edge_max_price: float = field(default_factory=lambda: _f("AI_EDGE_MAX_PRICE", 0.90))
    ai_edge_max_hours: float = field(default_factory=lambda: _f("AI_EDGE_MAX_HOURS", 168.0))
    ai_edge_min_liquidity: float = field(default_factory=lambda: _f("AI_EDGE_MIN_LIQUIDITY", 5000.0))
    ai_edge_position_frac: float = field(default_factory=lambda: _f("AI_EDGE_POSITION_FRAC", 0.15))
    # self-protection: once enough Jev calls have resolved, stop edge trades if its hit-rate is poor
    ai_min_samples_for_breaker: int = field(default_factory=lambda: _i("AI_MIN_SAMPLES", 30))
    ai_breaker_min_accuracy: float = field(default_factory=lambda: _f("AI_BREAKER_MIN_ACCURACY", 0.88))

    # --- sports mode: NFL / college football moneylines priced against ESPN data ---
    sports_only: bool = field(default_factory=lambda: _b("SPORTS_ONLY", True))
    sports_leagues: tuple = field(default_factory=lambda: tuple(x.strip() for x in os.environ.get("SPORTS_LEAGUES", "nfl,cfb").split(",") if x.strip()))
    sports_kelly_frac: float = field(default_factory=lambda: _f("SPORTS_KELLY_FRAC", 0.5))
    sports_margin_pre: float = field(default_factory=lambda: _f("SPORTS_MARGIN_PRE", 0.04))
    sports_margin_live: float = field(default_factory=lambda: _f("SPORTS_MARGIN_LIVE", 0.05))
    sports_margin_live_sure: float = field(default_factory=lambda: _f("SPORTS_MARGIN_LIVE_SURE", 0.03))
    sports_margin_final: float = field(default_factory=lambda: _f("SPORTS_MARGIN_FINAL", 0.01))
    sports_exit_margin: float = field(default_factory=lambda: _f("SPORTS_EXIT_MARGIN", 0.05))
    sports_max_hours_ahead: float = field(default_factory=lambda: _f("SPORTS_MAX_HOURS_AHEAD", 7 * 24.0))
    sports_min_liquidity: float = field(default_factory=lambda: _f("SPORTS_MIN_LIQUIDITY", 2000.0))
    sports_max_spread: float = field(default_factory=lambda: _f("SPORTS_MAX_SPREAD", 0.05))
    sports_book_hours_ahead: float = field(default_factory=lambda: _f("SPORTS_BOOK_HOURS_AHEAD", 36.0))   # fetch books this close to kickoff

    # --- scanning ---
    scan_limit: int = field(default_factory=lambda: _i("SCAN_LIMIT", 600))
    book_batch: int = 40
    max_orders_per_run: int = field(default_factory=lambda: _i("MAX_ORDERS_PER_RUN", 6))

    # --- files ---
    state_dir: str = field(default_factory=lambda: os.environ.get("STATE_DIR", "state"))
    kill_switch_file: str = field(default_factory=lambda: os.environ.get("KILL_SWITCH_FILE", "state/STOP"))

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown RISK_PROFILE {self.profile!r}; choose one of {sorted(PROFILES)}")
        for key, value in PROFILES[self.profile].items():
            if os.environ.get(ENV_NAMES[key]) in (None, ""):
                setattr(self, key, value)

    @property
    def ai_active(self) -> bool:
        return bool(self.ai_enabled and self.openrouter_api_key)

    @property
    def is_live(self) -> bool:
        if not self.live_enabled:
            return False
        if self.exchange == "us":
            return bool(self.us_key_id and self.us_secret)
        # the funder (wallet address) is optional: it is derived from the key when absent
        return bool(self.private_key)
