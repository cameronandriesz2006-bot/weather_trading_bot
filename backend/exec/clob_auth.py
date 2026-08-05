"""CLOB credentials: env loading, client construction, L2 API-cred derivation + disk cache.

Verified against the installed py-clob-client 0.34.6 source (read 2026-08-05):

  - ``ClobClient(host, chain_id, key, creds, signature_type, funder)`` — three modes,
    L0 (host only) / L1 (+ private key) / L2 (+ ApiCreds); ``_get_client_mode`` picks by
    what is present, and ``assert_level_2_auth`` gates every order endpoint.
  - Signature types are ``py_order_utils.model.signatures``: ``EOA = 0``,
    ``POLY_PROXY = 1`` (email/magic proxy), ``POLY_GNOSIS_SAFE = 2`` (browser-wallet proxy).
    ``OrderBuilder(signer, sig_type, funder)`` defaults sig_type to EOA and funder to the
    signer address — we require both explicitly instead, because a wrong funder signs
    orders against an account with no money and the failure is silent until post time.
  - ``create_or_derive_api_creds(nonce)`` = try ``create_api_key`` (POST /auth/api-key),
    fall back to ``derive_api_key`` (GET /auth/derive-api-key) on any exception. Both are
    L1-signed; the returned secret is NOT recoverable from the server later, hence the cache.

Env is read the same way ``backend/config.py`` reads it: pydantic-settings ``BaseSettings``
with ``env_file=".env"`` and ``extra="ignore"``, so ``.env`` overrides nothing else and
unknown keys in ``.env`` do not raise. All three vars are REQUIRED — no silent defaults.
A defaulted signature type or a funder silently set to the EOA address is exactly the
kind of guess that posts orders from the wrong wallet.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from typing import Optional

from pydantic_settings import BaseSettings

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

#: L2 creds cache. The API secret cannot be re-fetched from the server, so it is written
#: once, 0600, at the repo root and gitignored. Keyed by address+signature type: a
#: different key or wallet mode must never reuse another account's creds.
CREDS_CACHE_PATH = ".clob_creds.json"

REQUIRED_VARS = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_FUNDER_ADDRESS",
    "POLYMARKET_SIGNATURE_TYPE",
)

SIGNATURE_TYPES = {0: "EOA", 1: "POLY_PROXY (email/magic)", 2: "POLY_GNOSIS_SAFE (browser wallet)"}


class MissingCredentials(RuntimeError):
    """Raised when one or more required POLYMARKET_* vars are absent or empty.

    Carries ``missing`` so a CLI can exit with the exact list rather than a stack trace.
    """

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__(
            "missing required environment variable(s): "
            + ", ".join(self.missing)
            + " — set them in .env (see .env.example). No key exists on this box by design; "
            "everything except the authenticated POST path runs without one."
        )


class _ClobSettings(BaseSettings):
    """Only the three execution vars. Deliberately separate from ``Settings`` in
    backend/config.py so importing this module cannot perturb the running scanner."""

    POLYMARKET_PRIVATE_KEY: Optional[str] = None
    POLYMARKET_FUNDER_ADDRESS: Optional[str] = None
    POLYMARKET_SIGNATURE_TYPE: Optional[str] = None

    class Config:
        env_file = ".env"
        extra = "ignore"


@dataclass(frozen=True)
class ClobEnv:
    """Validated execution credentials. Never logged or reprd — ``__repr__`` is redacted."""

    private_key: str
    funder_address: str
    signature_type: int
    host: str = CLOB_HOST
    chain_id: int = POLYGON_CHAIN_ID

    def __repr__(self) -> str:  # keep the key out of tracebacks and logs
        return (
            f"ClobEnv(private_key=<redacted>, funder_address={self.funder_address!r}, "
            f"signature_type={self.signature_type}, host={self.host!r}, chain_id={self.chain_id})"
        )


def _clean(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v or None


def load_clob_env(env_file: Optional[str] = ".env") -> ClobEnv:
    """Read + validate the three execution vars. Raises MissingCredentials listing all
    absent ones at once (not one at a time — a caller with no key should learn everything
    it needs in one run), or ValueError on a present-but-malformed value."""
    s = _ClobSettings(_env_file=env_file)
    values = {
        "POLYMARKET_PRIVATE_KEY": _clean(s.POLYMARKET_PRIVATE_KEY),
        "POLYMARKET_FUNDER_ADDRESS": _clean(s.POLYMARKET_FUNDER_ADDRESS),
        "POLYMARKET_SIGNATURE_TYPE": _clean(s.POLYMARKET_SIGNATURE_TYPE),
    }
    missing = [k for k in REQUIRED_VARS if values[k] is None]
    if missing:
        raise MissingCredentials(missing)

    key = values["POLYMARKET_PRIVATE_KEY"]
    if not key.startswith("0x"):
        key = "0x" + key
    if len(key) != 66 or any(c not in "0123456789abcdefABCDEF" for c in key[2:]):
        raise ValueError("POLYMARKET_PRIVATE_KEY must be 32 hex bytes (0x + 64 hex chars)")

    funder = values["POLYMARKET_FUNDER_ADDRESS"]
    if not (funder.startswith("0x") and len(funder) == 42
            and all(c in "0123456789abcdefABCDEF" for c in funder[2:])):
        raise ValueError("POLYMARKET_FUNDER_ADDRESS must be a 20-byte hex address (0x + 40 hex chars)")

    raw_sig = values["POLYMARKET_SIGNATURE_TYPE"]
    try:
        sig = int(raw_sig)
    except ValueError:
        raise ValueError(
            f"POLYMARKET_SIGNATURE_TYPE must be an integer, got {raw_sig!r}. "
            + "; ".join(f"{k}={v}" for k, v in SIGNATURE_TYPES.items())
        )
    if sig not in SIGNATURE_TYPES:
        raise ValueError(
            f"POLYMARKET_SIGNATURE_TYPE must be one of {sorted(SIGNATURE_TYPES)}, got {sig}. "
            + "; ".join(f"{k}={v}" for k, v in SIGNATURE_TYPES.items())
        )

    return ClobEnv(private_key=key, funder_address=funder, signature_type=sig)


def build_client(env: ClobEnv, creds=None):
    """L1 client (or L2 if creds given). Import is local so that merely importing this
    module does not pull py_clob_client (and its httpx.Client) into the scanner process."""
    from py_clob_client.client import ClobClient

    return ClobClient(
        env.host,
        chain_id=env.chain_id,
        key=env.private_key,
        creds=creds,
        signature_type=env.signature_type,
        funder=env.funder_address,
    )


def _cache_key(address: str, signature_type: int) -> str:
    return f"{address.lower()}:{signature_type}"


def load_cached_creds(address: str, signature_type: int, path: str = CREDS_CACHE_PATH):
    """Return cached ApiCreds for this address+sig type, or None."""
    from py_clob_client.clob_types import ApiCreds

    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    entry = blob.get(_cache_key(address, signature_type))
    if not entry:
        return None
    try:
        return ApiCreds(
            api_key=entry["api_key"],
            api_secret=entry["api_secret"],
            api_passphrase=entry["api_passphrase"],
        )
    except KeyError:
        return None


def save_cached_creds(address: str, signature_type: int, creds, path: str = CREDS_CACHE_PATH) -> None:
    """Merge one entry into the cache and re-chmod 0600. The file holds an HMAC secret
    that grants order placement for this address — it is gitignored, never logged."""
    blob = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            blob = {}
    blob[_cache_key(address, signature_type)] = {
        "address": address,
        "signature_type": signature_type,
        "api_key": creds.api_key,
        "api_secret": creds.api_secret,
        "api_passphrase": creds.api_passphrase,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        json.dump(blob, fh, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # pre-existing file: O_CREAT mode doesn't apply


def ensure_api_creds(client, signature_type: int, path: str = CREDS_CACHE_PATH):
    """Cached L2 creds for the client's address, deriving them once if absent.

    ``create_or_derive_api_creds`` is idempotent server-side (create, else derive), but it
    is a network round trip and a signature per call, so it runs once per address and the
    result is cached to disk. Returns ApiCreds; also calls ``set_api_creds`` so the client
    moves to L2.
    """
    address = client.get_address()
    creds = load_cached_creds(address, signature_type, path)
    if creds is None:
        creds = client.create_or_derive_api_creds()
        if creds is None:
            raise RuntimeError(
                "create_or_derive_api_creds returned None — the CLOB accepted the L1 "
                "signature but the response could not be parsed as creds"
            )
        save_cached_creds(address, signature_type, creds, path)
    client.set_api_creds(creds)
    return creds
