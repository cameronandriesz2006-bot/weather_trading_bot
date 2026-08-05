"""Tier C execution path. Nothing here may be imported by the scanner (backend/data/*).

`SIMULATION_MODE` stays True; this package exists so the order path can be built and
audited before any capital is committed. Every order it can produce is rail-limited to a
provably non-marketable BUY (see order_client.DryRunGuard).

Exports resolve lazily (PEP 562) so `import backend.exec.fill_policy` — the pure-logic C9
path — never drags the network-capable client modules in (audit 2026-08-05, SHOULD-FIX).
"""
from importlib import import_module

_HOME = {
    "ClobEnv": ".clob_auth",
    "MissingCredentials": ".clob_auth",
    "load_clob_env": ".clob_auth",
    "STRICT_DEFAULT": ".order_client",
    "DryRunGuard": ".order_client",
    "GuardRailViolation": ".order_client",
    "OrderClient": ".order_client",
    "OrderSpec": ".order_client",
    "PostResult": ".order_client",
}

__all__ = list(_HOME)


def __getattr__(name):
    if name in _HOME:
        return getattr(import_module(_HOME[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
