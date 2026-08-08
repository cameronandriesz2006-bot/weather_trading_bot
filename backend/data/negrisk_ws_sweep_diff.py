#!/usr/bin/env python3
"""Compare the two opportunity watchers over the period both were running.

Sources:
  logs/negrisk_ws_detect.jsonl  - the live-feed watcher (rows: detect / episode_end)
  logs/negrisk_arb.jsonl        - the every-2s checker   (rows: board)

Both tools already use a 300s gap rule to group sightings of the same
board+side into one "episode" (one opportunity window). This script re-groups
raw rows from both logs with that same rule, so the two sides are counted
identically, then matches episodes across sources when their time spans
(with 2.5s slack, about one checker interval) overlap.

Durations are first-sighting to last-sighting; a flickering opportunity that
comes and goes within its window counts its whole span, so "duration" is an
upper bound on time actually in profit.

Run from the repo root:  python3 -m backend.data.negrisk_ws_sweep_diff
Results doc: WS_VS_SWEEP_2026-08-08.md
"""
import json
import statistics
from collections import Counter
from datetime import datetime, timezone

GAP_S = 300.0     # same episode-gap rule both tools use
SLACK_S = 2.5     # ~one checker interval, allowed when matching across sources
WS_LOG = "logs/negrisk_ws_detect.jsonl"
SWEEP_LOG = "logs/negrisk_arb.jsonl"


def ts(s):
    return datetime.fromisoformat(s).timestamp()


def utc(t):
    return datetime.fromtimestamp(t, tz=timezone.utc)


def load_rows():
    ws_rows, ws_start, ws_last = [], None, None
    for line in open(WS_LOG):
        r = json.loads(line)
        t = r.get("type")
        if t == "start" and ws_start is None:
            ws_start = ts(r["ts"])
        if t == "detect":
            ws_rows.append((ts(r["ts"]), r["slug"], r["side"], r.get("profit", 0.0)))
        elif t == "episode_end":
            ws_rows.append((ts(r["ts"]), r["slug"], r["side"], r.get("peak_profit", 0.0)))
        if "ts" in r:
            ws_last = ts(r["ts"])

    sw_rows, sw_last = [], None
    for line in open(SWEEP_LOG):
        if '"type": "board"' not in line and '"type": "sweep"' not in line:
            continue
        r = json.loads(line)
        t0 = ts(r["ts"])
        sw_last = t0
        if r["type"] != "board" or t0 < ws_start:
            continue
        for side in ("buy", "short"):
            if side in r and isinstance(r[side], dict):
                sw_rows.append((t0, r["slug"], side, r[side].get("profit", 0.0)))

    return ws_rows, sw_rows, ws_start, min(ws_last, sw_last)


def cluster(rows, window_end):
    by_key = {}
    for row in sorted(rows):
        if row[0] > window_end:
            continue
        key = (row[1], row[2])
        eps = by_key.setdefault(key, [])
        if eps and row[0] - eps[-1]["last"] < GAP_S:
            e = eps[-1]
            e["last"] = row[0]
            e["peak"] = max(e["peak"], row[3])
            e["rows"] += 1
        else:
            eps.append({"slug": row[1], "side": row[2], "first": row[0],
                        "last": row[0], "peak": row[3], "rows": 1})
    return [e for eps in by_key.values() for e in eps]


def main():
    ws_rows, sw_rows, ws_start, window_end = load_rows()
    hours = (window_end - ws_start) / 3600
    print(f"window: {utc(ws_start).isoformat()} -> {utc(window_end).isoformat()} ({hours:.1f} h)")

    ws_eps = cluster(ws_rows, window_end)
    sw_eps = cluster(sw_rows, window_end)
    print(f"episodes: live-feed={len(ws_eps)}  checker={len(sw_eps)}")

    sw_by_key = {}
    for e in sw_eps:
        sw_by_key.setdefault((e["slug"], e["side"]), []).append(e)

    matched, ws_only = [], []
    for w in ws_eps:
        hit = next((s for s in sw_by_key.get((w["slug"], w["side"]), [])
                    if s["first"] - SLACK_S <= w["last"] and s["last"] + SLACK_S >= w["first"]),
                   None)
        if hit is not None:
            hit["_matched"] = True
            matched.append((w, hit))
        else:
            ws_only.append(w)
    sw_only = [e for e in sw_eps if not e.get("_matched")]

    print(f"seen by both: {len(sw_eps) - len(sw_only)}/{len(sw_eps)} of checker episodes"
          f" ({len(matched)} live-feed episodes involved)")
    print(f"live-feed only: {len(ws_only)}   checker only: {len(sw_only)}")

    leads = sorted(s["first"] - w["first"] for w, s in matched)
    if leads:
        print(f"how much later the checker saw shared episodes (s): "
              f"median={statistics.median(leads):.2f} "
              f"p10={leads[int(0.1 * len(leads))]:.2f} p90={leads[int(0.9 * len(leads))]:.2f}")

    print(f"peak $ on shared episodes: live-feed view={sum(w['peak'] for w, _ in matched):.2f} "
          f"checker view={sum(e['peak'] for e in sw_eps if e.get('_matched')):.2f}")

    durs = sorted(e["last"] - e["first"] for e in ws_only)
    if durs:
        under = sum(1 for d in durs if d < 2.4)
        print(f"live-feed-only: n={len(ws_only)} peak_sum=${sum(e['peak'] for e in ws_only):.2f} "
              f"(${sum(e['peak'] for e in ws_only) / hours * 24:.2f}/day at peak); "
              f"{under}/{len(durs)} lasted under one checker interval (2.4s), "
              f"median span {statistics.median(durs):.2f}s")
        for lo, hi in [(0, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 2.4), (2.4, 10), (10, float("inf"))]:
            es = [e for e in ws_only if lo <= e["last"] - e["first"] < hi]
            s = sum(e["peak"] for e in es)
            print(f"  span {lo}-{hi}s: n={len(es)} peak_sum=${s:.2f} (${s / hours * 24:.2f}/day)")
        longlived = sorted((e["peak"] for e in ws_only if e["last"] - e["first"] >= 0.7),
                           reverse=True)
        print(f"  lived >=0.7s: n={len(longlived)} sum=${sum(longlived):.2f} "
              f"(${sum(longlived) / hours * 24:.2f}/day); "
              f"excluding largest single one: ${sum(longlived[1:]) / hours * 24:.2f}/day")

    print("checker-only episodes (the 0.3s re-check zeroed the large ones):")
    for e in sorted(sw_only, key=lambda x: -x["peak"]):
        print(f"  {utc(e['first']).isoformat()} {e['slug']} {e['side']} "
              f"peak=${e['peak']:.4f} sightings={e['rows']}")

    c_ws = Counter(utc(e["first"]).date().isoformat() for e in ws_eps)
    c_sw = Counter(utc(e["first"]).date().isoformat() for e in sw_eps)
    print("episodes per day, live-feed:", dict(sorted(c_ws.items())))
    print("episodes per day, checker:  ", dict(sorted(c_sw.items())))


if __name__ == "__main__":
    main()
