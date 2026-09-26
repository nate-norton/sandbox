"""CLOB access: public order-book reads (no auth) and authenticated order posting."""
from __future__ import annotations

import logging
from typing import Optional

import requests

from .models import Book, Leg

log = logging.getLogger(__name__)


class PublicClob:
    """Unauthenticated reads. Works in paper mode without any credentials."""

    def __init__(self, host: str, session: requests.Session | None = None, timeout: float = 20.0):
        self.host = host.rstrip("/")
        self.s = session or requests.Session()
        self.timeout = timeout
        self._fees: dict[str, int] = {}

    def books(self, token_ids: list[str], batch: int = 40) -> dict[str, Book]:
        out: dict[str, Book] = {}
        for i in range(0, len(token_ids), batch):
            chunk = token_ids[i:i + batch]
            try:
                r = self.s.post(f"{self.host}/books", json=[{"token_id": t} for t in chunk], timeout=self.timeout)
                r.raise_for_status()
                for d in r.json():
                    b = Book.from_clob(d)
                    if b.token_id:
                        out[b.token_id] = b
            except (requests.RequestException, ValueError) as e:
                log.warning("books batch failed (%d ids): %s", len(chunk), e)
        return out

    def fee_bps(self, token_id: str) -> int:
        if token_id in self._fees:
            return self._fees[token_id]
        try:
            r = self.s.get(f"{self.host}/fee-rate", params={"token_id": token_id}, timeout=self.timeout)
            r.raise_for_status()
            fee = int(r.json().get("base_fee") or 0)
        except (requests.RequestException, ValueError):
            fee = 0
        self._fees[token_id] = fee
        return fee


class LiveClob:
    """Authenticated trading through py-clob-client. Constructed only in live mode."""

    def __init__(self, host: str, chain_id: int, private_key: str, funder: str, signature_type: int):
        from py_clob_client.client import ClobClient  # imported lazily: paper mode needs no key

        self.client = ClobClient(host, key=private_key, chain_id=chain_id, signature_type=signature_type, funder=funder)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self.funder = funder
        self.signature_type = signature_type

    def usdc_balance(self) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        res = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self.signature_type)
        )
        return float(res.get("balance", 0)) / 1e6

    def open_orders(self) -> list[dict]:
        return self.client.get_orders() or []

    def cancel_all(self) -> None:
        self.client.cancel_all()

    def fill_or_kill(self, leg: Leg, tick_size: Optional[float] = None, neg_risk: Optional[bool] = None) -> tuple[float, dict]:
        """Post a fill-and-kill limit order. Returns (filled_size, raw_response).

        tick_size/neg_risk are hints; when None the client looks them up for the token.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

        tick = {0.1: "0.1", 0.01: "0.01", 0.001: "0.001", 0.0001: "0.0001"}.get(tick_size) if tick_size else None
        args = OrderArgs(token_id=leg.token_id, price=round(leg.price, 4), size=round(leg.size, 2), side=leg.side)
        signed = self.client.create_order(args, PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk))
        resp = self.client.post_order(signed, OrderType.FAK) or {}
        filled = _filled_from_response(resp, leg.size, leg.side)
        return filled, resp


def _filled_from_response(resp: dict, requested: float, side: str) -> float:
    """Best-effort filled share count from a FAK post response.

    For a BUY the shares received are the takingAmount (we give USDC, take shares); for a SELL
    they are the makingAmount. A FAK that could not fill anything comes back unmatched.
    """
    if not isinstance(resp, dict):
        return 0.0
    if resp.get("success") is False or resp.get("errorMsg"):
        return 0.0
    status = str(resp.get("status", "")).lower()
    if status not in ("matched", "filled"):
        return 0.0
    keys = ("takingAmount", "sizeMatched", "size_matched") if side.upper() == "BUY" else ("makingAmount", "sizeMatched", "size_matched")
    for k in keys:
        v = resp.get(k)
        if v not in (None, ""):
            try:
                return min(float(v), requested)
            except ValueError:
                pass
    return requested


class DataApi:
    """Positions/holdings for the funder wallet (live mode reporting & de-dup)."""

    def __init__(self, host: str, session: requests.Session | None = None, timeout: float = 20.0):
        self.host = host.rstrip("/")
        self.s = session or requests.Session()
        self.timeout = timeout

    def positions(self, user: str) -> list[dict]:
        try:
            r = self.s.get(f"{self.host}/positions", params={"user": user, "sizeThreshold": 0.01, "limit": 500}, timeout=self.timeout)
            r.raise_for_status()
            return r.json() or []
        except (requests.RequestException, ValueError) as e:
            log.warning("data-api positions failed: %s", e)
            return []
