"""One-command on-chain status of the trading wallet: PUSD / USDC.e / POL balances.

Run from the repo root:  python3 -m backend.exec.wallet_status
Reads only the PUBLIC funder address from .env — never touches the private key.
"""
from __future__ import annotations

import re
import sys

import httpx

ENV_PATH = "/root/weather_trading_bot/.env"

# RPCs that answer from this box (2026-08-08: polygon-rpc.com, llamarpc, 1rpc,
# ankr and blastapi all refuse or require keys).
RPCS = ["https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org"]

# Token registry — PUSD is the live collateral since the 2026 migration
# (see PUSD_MIGRATION_2026-08-08.md); the USDC rows exist to catch money
# accidentally parked in the wrong coin.
TOKENS = {
    "PUSD (live collateral)": ("0xC011a7E12A19F7b1F670d46f03B03F3342e82DFB", 6),
    "USDC.e (retired collateral)": ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),
    "USDC native": ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
}


def funder_address() -> str:
    for line in open(ENV_PATH):
        m = re.match(r"^POLYMARKET_FUNDER_ADDRESS=(0x[0-9a-fA-F]{40})\s*$", line)
        if m:
            return m.group(1)
    sys.exit("POLYMARKET_FUNDER_ADDRESS not set in .env")


def rpc(client: httpx.Client, url: str, method: str, params: list):
    j = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params}).json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


def main() -> None:
    addr = funder_address()
    print(f"wallet: {addr}")
    last_err: Exception | None = None
    with httpx.Client(timeout=15) as client:
        for url in RPCS:
            try:
                pol = int(rpc(client, url, "eth_getBalance", [addr, "latest"]), 16) / 1e18
                print(f"POL (gas): {pol:.2f}")
                for name, (token, dec) in TOKENS.items():
                    data = "0x70a08231" + addr[2:].lower().rjust(64, "0")
                    res = rpc(client, url, "eth_call",
                              [{"to": token, "data": data}, "latest"])
                    print(f"{name}: {int(res, 16) / 10 ** dec:,.2f}")
                print(f"(via {url})")
                return
            except Exception as e:  # try the next RPC
                last_err = e
    sys.exit(f"all RPCs failed: {last_err}")


if __name__ == "__main__":
    main()
