"""Summarise the shadow-fill probe: of the paper profit the scanner spots, how much is still
takeable ~1s later, when a real order could first have reached the book?

The scanner (`negrisk_arb_scan.py:shadow_probe`) re-fetches every hit board's books right
after scoring it and logs a ``shadow`` row: original profit vs what the same optimiser finds
in the fresh books (`now` = 0 means nothing net-positive remained — taken or cancelled,
either way not ours). This tool reduces those rows to the number the go/no-go needs. Two
views matter and they answer different questions:

  per-PROBE     every hit sweep probes again, so a standing mispricing is probed many times.
                Good for "how flickery are these books", biased toward long-lived episodes.
  per-EPISODE   first probe of each standing mispricing (same slug+side grouping as
                `negrisk_arb_pnl.py`, 300s gap) — the moment the P&L replay credits a trade.
                THIS is measured fill survival for the replayed P&L: dollar-weighted
                episode survival directly haircuts the replay's $/day.

    python -m backend.data.negrisk_shadow_report --until <iso-ts>
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import statistics

LOG = "logs/negrisk_arb.jsonl"
SIDES = ("buy", "short")
BUCKETS = ((0.0, 0.10, "< $0.10"), (0.10, 1.0, "$0.10-1"), (1.0, float("inf"), ">= $1"))


def load(path: str, since: str | None, until: str | None) -> list[dict]:
    rows = []
    for line in open(path):
        if '"shadow"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "shadow":
            continue
        if since and d["ts"] < since:      # ISO strings, all +00:00 -> lexicographic is fine
            continue
        if until and d["ts"] > until:
            continue
        rows.append(d)
    return rows


def flat(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        for s in SIDES:
            if s in r:
                out.append({"ts": r["ts"], "slug": r["slug"], "side": s,
                            "delay": r["delay_s"], **r[s]})
    return out


def survival(ps: list[dict], label: str) -> None:
    if not ps:
        print(f"  {label}: no probes")
        return
    orig, now = sum(p["orig"] for p in ps), sum(p["now"] for p in ps)
    alive = sum(1 for p in ps if p["now"] > 0)
    print(f"  {label}: n={len(ps)}  alive {alive}/{len(ps)} ({alive / len(ps) * 100:.0f}%)  "
          f"$-weighted {now / orig * 100:.0f}%  (${now:.2f} of ${orig:.2f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--since", default=None, help="ISO ts; ignore probes before it")
    ap.add_argument("--until", default=None, help="ISO ts; ignore probes after it (reproducibility)")
    ap.add_argument("--episode-gap", type=float, default=300.0)
    args = ap.parse_args()

    probes = flat(load(args.log, args.since, args.until))
    if not probes:
        print("no shadow rows in window — probe went live 2026-07-27, check the window")
        return
    t0, t1 = probes[0]["ts"], probes[-1]["ts"]
    hours = (datetime.datetime.fromisoformat(t1)
             - datetime.datetime.fromisoformat(t0)).total_seconds() / 3600.0
    print(f"log      {args.log}")
    print(f"window   {t0[:16]} -> {t1[:16]} UTC  ({hours:.2f}h)")
    print(f"probes   {len(probes)}  median delay {statistics.median(p['delay'] for p in probes):.2f}s")

    print("\n=== per-PROBE survival (book flicker; long episodes over-represented) ===")
    survival(probes, "all  ")
    for s in SIDES:
        survival([p for p in probes if p["side"] == s], f"{s:>5}")
    for lo, hi, lab in BUCKETS:
        survival([p for p in probes if lo <= p["orig"] < hi], f"orig {lab:>8}")

    # Episode view: first probe per standing mispricing = what the P&L replay credits.
    eps = collections.defaultdict(list)
    for p in probes:
        eps[(p["slug"], p["side"])].append(p)
    firsts = []
    for rows in eps.values():
        rows.sort(key=lambda p: p["ts"])
        last = None
        for p in rows:
            t = datetime.datetime.fromisoformat(p["ts"])
            if last is None or (t - last).total_seconds() > args.episode_gap:
                firsts.append(p)
            last = t
    print(f"\n=== per-EPISODE survival at ARRIVAL probe — the replay haircut ===")
    survival(firsts, "all  ")
    for s in SIDES:
        survival([p for p in firsts if p["side"] == s], f"{s:>5}")
    for lo, hi, lab in BUCKETS:
        survival([p for p in firsts if lo <= p["orig"] < hi], f"orig {lab:>8}")

    big = sorted(firsts, key=lambda p: -p["orig"])[:10]
    print("\n  largest arrivals:")
    for p in big:
        print(f"    {p['ts'][:19]} {p['side']:>5} orig ${p['orig']:>7.2f} (k={p['k_orig']:.0f})"
              f" -> now ${p['now']:>7.2f} (k={p['k_now']:.0f})  {p['slug'][:44]}")


if __name__ == "__main__":
    main()
