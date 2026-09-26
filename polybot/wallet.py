"""Derive the Polymarket funding wallet from the signing key.

Polymarket gives every account a deterministic smart-contract wallet that holds the USDC:
a "proxy wallet" for email (Magic) logins, or a Gnosis Safe for browser-wallet logins.
Both are CREATE2 addresses of the signer, using the factory addresses and init-code hashes
published in @polymarket/builder-relayer-client (dist/builder/derive.js, dist/constants).
"""
from __future__ import annotations

from eth_account import Account
from eth_utils import keccak, to_checksum_address

PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"        # Polygon, Magic/email accounts
SAFE_FACTORY = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"         # Polygon, browser-wallet accounts
PROXY_INIT_CODE_HASH = bytes.fromhex("d21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b")
SAFE_INIT_CODE_HASH = bytes.fromhex("2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf")


def _addr_bytes(address: str) -> bytes:
    return bytes.fromhex(address[2:] if address.startswith("0x") else address)


def _create2(factory: str, salt: bytes, init_code_hash: bytes) -> str:
    digest = keccak(b"\xff" + _addr_bytes(factory) + salt + init_code_hash)
    return to_checksum_address("0x" + digest[-20:].hex())


def signer_address(private_key: str) -> str:
    key = private_key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key).address


def derive_proxy_wallet(signer: str) -> str:
    """Email/Magic login (signature type 1): salt = keccak256(abi.encodePacked(address))."""
    return _create2(PROXY_FACTORY, keccak(_addr_bytes(signer)), PROXY_INIT_CODE_HASH)


def derive_safe_wallet(signer: str) -> str:
    """Browser-wallet login (signature type 2): salt = keccak256(abi.encode(address))."""
    return _create2(SAFE_FACTORY, keccak(_addr_bytes(signer).rjust(32, b"\x00")), SAFE_INIT_CODE_HASH)


def candidate_funders(private_key: str) -> dict[int, str]:
    """signature_type -> wallet address that the key controls under that account type."""
    s = signer_address(private_key)
    return {1: derive_proxy_wallet(s), 2: derive_safe_wallet(s), 0: s}
