"""WebSocket SHADOW detector for the negRisk arb — detection latency, measured not assumed.

WHY THIS EXISTS
---------------
`negrisk_arb_scan.py` sweeps every board every 2s. C8 (reports/tier_c/C8_latency.md) measured the
consequence: sign+wire is ~0.08s of the see->submit chain while the sweep cadence contributes
~1.0s mean and up to 2.0s — **71-83% of the chain, against a 0.7s window in which 28.1% of
arrival value survives.** Detection, not submission, is the binding constraint.

This module is the alternative detector: subscribe to the CLOB market websocket for every tracked
token, maintain the books from pushed updates, and re-score a board the instant one of its tokens
moves — using the scanner's own `board_sanity` / `best_subset_net` / `best_set_size_net`, at the
same net-of-fee arithmetic, so the two logs are directly comparable.

It **decides nothing and trades nothing.** It runs alongside `negrisk-arb.service`, writes
`logs/negrisk_ws_detect.jsonl`, and after ~24h that log gets diffed against `logs/negrisk_arb.jsonl`
to measure the detection gain in seconds and in dollars of arrival value. If the gain is small,
this gets deleted; that is the point of building it as a shadow.

MEASURED ON 2026-08-05 (Vultr AMS, live probes against wss://ws-subscriptions-clob.polymarket.com)
--------------------------------------------------------------------------------------------------
Everything below was observed on the wire from this box, not read from the docs.

* **Subscribe**: one text frame `{"assets_ids": [...], "type": "market"}`. No auth. Connect (TLS +
  WS handshake) **54-62ms**; first frame **~90-300ms** after connect.
* **`book`** — full snapshot: `{market, asset_id, timestamp, hash, bids[], asks[], event_type}`
  with `{"price","size"}` STRING levels. `asks` arrive **descending** (most expensive first), the
  opposite of what the optimiser walks, so they are re-sorted here exactly as
  `orderbook.fetch_books` does. **The initial dump is ONE websocket frame containing a JSON ARRAY
  of every subscribed asset's book** (500 assets = 1 frame of 1.05 MB), not one frame per asset.
  Steady-state `book` events arrive as bare objects.
* **`price_change`** — the delta: `{market, timestamp, event_type, price_changes:[{asset_id, price,
  size, side, hash, best_bid, best_ask}]}`. `side` is `BUY` (bid ladder) / `SELL` (ask ladder).
  **`size` is the ABSOLUTE new size at that price level, not a delta** — verified by applying 866
  live changes across 66 tokens under both interpretations and diffing against `POST /books`:
  absolute gave **3966 level matches / 0 mismatches**, cumulative-add gave 346 mismatches. `size`
  `0` removes the level. Entries arrive in pairs: every change is reported once on the YES token
  and once on the mirrored NO token (`p` on one, `1-p` on the other, side flipped).
* **`best_bid`/`best_ask` on a `price_change` entry describe the book AFTER that change**, and an
  EMPTY side is reported as the sentinel `best_bid:"0"` / `best_ask:"1"` — not null. Decoded that
  way, a locally maintained book agreed with them **13 994 / 13 994 exactly on both sides** over
  15 900 live entries. That makes them a free in-band gap detector, which is what `_disagrees`
  is; reading the sentinels as prices instead produced ~70 phantom gaps/s in the first draft.
* **`last_trade_price`** and **`tick_size_change`** also arrive; neither is needed for the ask
  ladders, both are counted.
* **Every frame carries an exchange `timestamp`** (ms epoch). It is logged as `ex_ts` and is NEVER
  used as our clock — `ts` is when we processed, `ws_recv_ts` is when the bytes arrived.
* **Keepalive is application-level**: send the text frame `PING`, server replies with the text
  frame `PONG`. Protocol-level ping is disabled here (`ping_interval=None`); a 45s silence
  watchdog forces a reconnect.
* **PER-CONNECTION LIMIT — the finding that shapes the design, and it is a BYTE limit.** There is
  no error, no close, and no documented cap: past a threshold the initial dump simply never
  arrives, while deltas keep flowing. Bisected on the live universe:

  | assets on one connection | dump frame bytes | `book` snapshots received |
  |---|---|---|
  | 330  | 702 267   | 332 (100%) |
  | 500  | 1 052 554 | 502 (100%) |
  | 700  | 1 464 389 | 704 (100%) |
  | **750**  | **1 547 307** | **750 (100%)** |
  | **770**  | —         | **2 (0.3%)** |
  | 1000 | —         | 6 (0.6%)   |
  | 3366 | —         | 10 (0.3%)  |

  750 assets serialise to 1 547 307 bytes and arrive; 770 would be ~1.59 MB and do not. That is
  **1.5 MiB = 1 572 864 bytes**, so the cap is on the dump payload, not on the asset count — which
  means the safe shard size **moves with book depth** and cannot be hard-coded from an asset
  count alone. Hence `SHARD_ASSETS = 500` (~1.05 MB, ~67% of the limit): margin for deeper books.
  153 boards x 22 tokens = **3366 assets = 7 connections** today. Two consequences worth stating:
  `websockets.connect(max_size=None)` is **required** (the library's 1 MiB default would reject
  our own 1.05 MB dump), and every connect also does a REST `POST /books` resync so the feed is
  correct even if this behaviour silently changes again.
* **Rate**: full universe on one socket = **1671 frames/s, 8.0% of one core** to parse and count
  (30s sample). Single board = 25 frames/s. No throttling, no rate-limit response, no 429.

DESIGN NOTES THAT ARE NOT OBVIOUS
---------------------------------
* **Re-scoring per message is impossible, so "on change" is coalesced.** `evaluate_event` costs
  2.2ms/board; 1671 events/s would need 6.1 CPU-seconds per second. So (a) a **cheap exact gate**
  runs first and (b) rescoring is bounded to 1/`--coalesce-ms` passes/s (default 20ms = 50Hz).
  20ms of coalescing against 2000ms of sweep cadence keeps 99% of the available gain.
* **The gate is a necessary condition, not a heuristic — it cannot hide a hit.** Every ladder is
  sorted cheapest-first, so `cost_i(k) >= k * best_ask_i`, and taker fees are >= 0. Therefore
  BUY requires `sum_i best_ask(YES_i) < 1` and SHORT requires
  `sum_i max(0, 1 - best_ask(NO_i)) > 1`. Both are O(11) float adds (0.015ms/board vs 2.2ms).
  Measured on a live 153-board snapshot: 27 boards pass, 126 excluded, **0 gate misses** against
  a full `evaluate_event` run (asserted again in tests/test_ws_feed.py).
* **The depth cap is for speed, and never changes a logged number.** The hot path scores with
  `--ask-cap` (default 32) levels; a token needs 8 levels to reach 200 shares at p95, and 200
  shares/leg is ~$2000 of capital against a $250 bankroll. Whenever a side comes out profitable
  the board is **re-scored uncapped** before the row is written, so logged k/cost/profit are
  exactly what the sweep scanner would have printed.
* **Episodes mirror `negrisk_arb_pnl.py --episode-gap 300`** so the 24h diff is apples-to-apples:
  one `detect` row per NEW episode per (slug, side); a board that loses and regains profitability
  inside 300s stays the SAME episode. `episode_end` rows mark each time profitability first
  disappears (a flickering episode emits several — group by `ep`), and a `stale` end closes an
  episode whose board went 300s with no profitable observation.
* **After a REST resync the bid side is top-of-book only.** `orderbook.fetch_books` returns full
  ASK ladders (what the optimiser walks) but only `best_bid` — and that module must not be
  modified. Bids are used for the drift spot-check and nothing else, so this is stated rather
  than worked around; `bid_top_only` on the heartbeat counts tokens in that state.

FIRST LIVE READ — 2026-08-05, 6min of foreground running, ONE observation, not a rate
--------------------------------------------------------------------------------------
153 boards / 3366 tokens / 7 connections. ~1100-1300 frames/s, 15% of one core, 104-110MB RSS,
0 dropped frames, 0 reconnects, 3 REST spot-checks x 264 top-of-book comparisons = **0 drift**.

At 11:31:52.089Z it logged a buy on `highest-temperature-in-warsaw-on-august-5-2026`: k=30,
cost $27.47, **net $1.60 after fees**, roc 5.8%, executable — **22ms after the frame arrived**.
The episode ended 163ms later. `negrisk-arb.service` was sweeping throughout and logged
`hits: 0` on every sweep in that window, including the one stamped 11:31:52.048Z — 41ms before
this row. A ~0.16s window is simply not visible to a 2s cadence.

That is one episode. It is the shape C8 predicted, not a measurement of the gain; the 24h log
diff is what produces the number. Contention cost: the scanner's own `fetch_s` went from p50
0.354s alone to 0.400s with this running (+13%, n=1494/237) — real, and far from the 2s cadence.

Run:
  PYTHONPATH=. venv/bin/python -m backend.data.negrisk_ws_feed --duration 180
  PYTHONPATH=. venv/bin/python -m backend.data.negrisk_ws_feed          # forever
"""
import argparse
import asyncio
import ctypes
import gc
import json
import os
import random
import resource
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import websockets

from backend.data import negrisk_arb_scan as S
from backend.data.orderbook import fetch_books

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LOG_PATH = "logs/negrisk_ws_detect.jsonl"
# Our OWN token cache. The scanner's logs/negrisk_tokens_v2.json is read as a warm start and
# NEVER written: two processes doing a non-atomic json.dump into the same path is a torn file
# waiting to happen, and that file belongs to the running service.
TOKEN_CACHE = "logs/negrisk_ws_tokens.json"

SHARD_ASSETS = 500          # measured ceiling for a complete initial `book` dump (see docstring)
EPISODE_GAP_S = 300.0       # == negrisk_arb_pnl.py --episode-gap default
PING_EVERY = 10.0           # application-level PING/PONG per the market-channel protocol
SILENCE_S = 45.0            # no frame for this long -> assume dead socket, reconnect
MAX_LEVELS = 256            # per side per token; the tick grid bounds this anyway, belt+braces
MAX_PENDING = 400           # buffered updates per token while a REST resync is in flight
TICK_TOL = 0.0105           # "beyond one tick": the coarsest tick on these boards is 0.01
GAP_STRIKES = 2             # consecutive top-of-book disagreements before we force a resync


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


try:
    _LIBC = ctypes.CDLL("libc.so.6")
except OSError:                                    # not glibc: trim is a no-op, RSS just runs higher
    _LIBC = None


def _trim() -> None:
    """Hand freed arenas back to the OS.

    MEASURED: the book dicts are NOT the memory — clearing every level on every one of 3366
    tokens moved RSS by 2MB. What moves it is transient allocation: the ~1MB initial-dump frames
    (one per shard) and the 726-board token cache read at startup, which fragment glibc's heap.
    `gc.collect()` + `malloc_trim` on a 60s cadence took startup 68MB -> 49MB and steady state
    ~110MB -> ~104MB on a 950MB box where 390MB is free.
    """
    gc.collect()
    if _LIBC is not None:
        try:
            _LIBC.malloc_trim(0)
        except Exception:
            pass


def _rss_mb() -> float:
    """Current RSS. getrusage reports the PEAK, which hides a process that has since shrunk."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except Exception:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _levels(raw) -> List[Tuple[float, float]]:
    """Parse [{'price','size'},...] -> [(price,size)], dropping junk. Mirrors orderbook._parse_levels."""
    out: List[Tuple[float, float]] = []
    for lvl in raw or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if p > 0 and s > 0:
            out.append((p, s))
    return out


def _eq(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def _disagrees(ours: Optional[float], theirs) -> bool:
    """Does the exchange's stated top of book differ from ours?

    MEASURED 2026-08-05: `best_bid`/`best_ask` on a `price_change` entry describe the book AFTER
    that change, and an EMPTY side is reported as the sentinel `best_bid: "0"` / `best_ask: "1"`,
    not as null. Over 15 900 live entries, once those sentinels are decoded as "no levels",
    agreement with a locally maintained book was **13 994 / 13 994 exact on both sides** — the
    1 906 apparent disagreements were all sentinel-vs-empty. Reading the sentinel as a price is
    what turned this check into a resync storm (~70 false gaps/s) in the first draft.
    """
    if theirs is None or theirs == "":
        return False
    try:
        t = float(theirs)
    except (TypeError, ValueError):
        return False
    if t <= 0.0 or t >= 1.0:
        t = None                      # sentinel: that side of the book is empty
    return not _eq(ours, t)


# --------------------------------------------------------------------------- book state
class TokenBook:
    """One token's live book, maintained from pushed updates.

    Both sides are kept: asks are what the optimiser walks, bids are what the REST spot-check
    compares. Levels live in a dict keyed by price — deltas are absolute *per price level*, so a
    dict is the natural shape — with a lazily rebuilt sorted ask ladder for the scorer.
    """
    __slots__ = ("bids", "asks", "_lad", "seeded", "pending", "ex_ts", "bid_top_only", "strikes")

    def __init__(self):
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self._lad: Optional[List[Tuple[float, float]]] = None
        self.seeded = False
        self.pending: Optional[list] = None
        self.ex_ts: Optional[int] = None
        self.bid_top_only = False
        self.strikes = 0

    def snapshot(self, bids, asks, bid_top_only: bool = False) -> None:
        self.bids = {p: s for p, s in bids if s > 0}
        self.asks = {p: s for p, s in asks if s > 0}
        self._lad = None
        self.seeded = True
        self.bid_top_only = bid_top_only
        self.strikes = 0

    def set_level(self, side: str, price: float, size: float) -> bool:
        """Absolute size at one price level. Returns True if the ASK ladder changed."""
        d = self.bids if side == "BUY" else self.asks
        if size <= 0:
            d.pop(price, None)
        else:
            d[price] = size
            if len(d) > MAX_LEVELS:
                # Unreachable on a <=0.001 tick grid; if it ever happens, drop the levels
                # furthest from the touch rather than grow without bound.
                keep = sorted(d.items(), key=(lambda kv: -kv[0]) if side == "BUY"
                              else (lambda kv: kv[0]))[:MAX_LEVELS]
                d.clear()
                d.update(keep)
        if side == "BUY":
            return False
        self._lad = None
        return True

    def ladder(self, cap: Optional[int] = None) -> List[Tuple[float, float]]:
        """Ask ladder, cheapest-first — the exact shape `LiveBook.asks` has."""
        if self._lad is None:
            self._lad = sorted(self.asks.items())
        return self._lad if cap is None else self._lad[:cap]

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None


# --------------------------------------------------------------------------- scoring
def gate(legs: List[dict], asks_of, sanity: dict) -> Tuple[bool, bool]:
    """Exact necessary conditions for (buy, short) profit. O(n) float adds, no ladder walk.

    Sound because every ladder is cheapest-first — `cost_i(k) >= k*best_ask_i` — and the taker
    fee is non-negative. So `net_buy(k) <= k*(1 - sum best_ask(YES_i))` and, over the legs any
    subset could include, `net_short(k) <= k*(sum (1 - best_ask(NO_i)) - 1)`. A board failing the
    gate cannot be profitable at ANY depth: this is the contrapositive, not a filter.
    """
    buy = short = False
    if sanity.get("exhaustive"):
        tot, ok = 0.0, True
        for leg in legs:
            a = asks_of(leg["yes"])
            if not a:
                ok = False
                break
            tot += a[0][0]
        buy = ok and tot < 1.0
    if sanity.get("exclusive"):
        tot, n = 0.0, 0
        for leg in legs:
            a = asks_of(leg["no"])
            if a and a[0][0] < 1.0:
                tot += 1.0 - a[0][0]
                n += 1
        short = n >= 2 and tot > 1.0
    return buy, short


def score_board(legs: List[dict], asks_of, sanity: Optional[dict] = None,
                want_buy: bool = True, want_short: bool = True) -> Optional[dict]:
    """`negrisk_arb_scan.evaluate_event` re-expressed over pushed books.

    Identical arithmetic — it calls the scanner's own `best_subset_net` / `best_set_size_net` and
    rounds to the same places — with two deliberate differences:
      * `short_gross_illusion` is not computed. It is a log-only record of the old gross scorer's
        mistake, costs ~1.1ms/board (two thirds of the hot-path budget), and the sweep log already
        carries it continuously.
      * `want_buy`/`want_short` skip a side the gate has already proved cannot be profitable.
    With both wants True the output equals `evaluate_event` minus that one key — asserted on real
    captured books in tests/test_ws_feed.py::test_score_board_matches_scanner.
    """
    n = len(legs)
    yes_lad, no_sub = [], []
    complete_yes = complete_no = True
    for i, leg in enumerate(legs):
        ya = asks_of(leg["yes"])
        na = asks_of(leg["no"])
        if ya:
            yes_lad.append(ya)
        else:
            complete_yes = False
        if na:
            no_sub.append((i, na))
        else:
            complete_no = False
    if not no_sub:
        return None

    sanity = sanity or {"exclusive": False, "exhaustive": False, "why": "unchecked"}
    out = {"n": n, "legs_quoted": len(no_sub), "complete": complete_no}

    if want_short and sanity["exclusive"]:
        net = S.best_subset_net(no_sub)
        if net:
            out["short"] = {"k": round(net["k"], 2), "cost": round(net["cost"], 2),
                            "gross": round(net["gross"], 4), "fee": round(net["fee"], 4),
                            "profit": round(net["net"], 4), "roc": round(net["roc"], 5),
                            "legs": net["legs"],
                            "executable": net["k"] >= S.MIN_ORDER_SHARES}
    if want_buy and complete_yes and sanity["exhaustive"]:
        out["top_yes_ask_sum"] = round(sum(l[0][0] for l in yes_lad), 4)
        buy = S.best_set_size_net(yes_lad, 1.0)
        if buy:
            out["buy"] = {"k": round(buy["k"], 2), "cost": round(buy["cost"], 2),
                          "gross": round(buy["gross"], 4), "fee": round(buy["fee"], 4),
                          "profit": round(buy["net"], 4), "roc": round(buy["roc"], 5),
                          "executable": buy["k"] >= S.MIN_ORDER_SHARES}
    if not sanity["exclusive"] or not sanity["exhaustive"]:
        out["sanity"] = sanity.get("why") or "ok"
    return out


# --------------------------------------------------------------------------- episodes
class EpisodeTracker:
    """New / continuing / ended, on the sweep analytics' 300s gap rule.

    Deliberately mirrors `negrisk_arb_pnl.executions()`: rows for one (slug, side) separated by
    <= `gap_s` are ONE standing mispricing. A board that flickers off and back inside 300s
    therefore yields one `detect`, exactly as the sweep replay yields one execution — otherwise
    the 24h comparison would credit the faster detector with episodes the slower one merges.
    """

    def __init__(self, gap_s: float = EPISODE_GAP_S):
        self.gap_s = gap_s
        self.eps: Dict[Tuple[str, str], dict] = {}
        self.next_id = 1

    def observe(self, slug: str, side: str, prof: Optional[dict],
                now: float) -> List[Tuple[str, dict]]:
        """('new'|'end', episode) transitions caused by this observation."""
        key = (slug, side)
        ep = self.eps.get(key)
        out: List[Tuple[str, dict]] = []
        if prof is not None:
            if ep is None or (now - ep["last_prof"]) > self.gap_s:
                ep = {"id": self.next_id, "slug": slug, "side": side, "start": now,
                      "last_prof": now, "updates": 1, "open": True, "resumes": 0,
                      "peak": prof["profit"], "last_profit": prof["profit"]}
                self.next_id += 1
                self.eps[key] = ep
                out.append(("new", ep))
            else:
                if not ep["open"]:
                    ep["open"] = True
                    ep["resumes"] += 1
                ep["last_prof"] = now
                ep["updates"] += 1
                ep["peak"] = max(ep["peak"], prof["profit"])
                ep["last_profit"] = prof["profit"]
        elif ep is not None and ep["open"]:
            ep["open"] = False
            out.append(("end", ep))
        return out

    def expire(self, now: float) -> List[dict]:
        """Episodes whose board went quiet while profitable — closed once past the gap."""
        out = []
        for key, ep in list(self.eps.items()):
            if (now - ep["last_prof"]) > self.gap_s:
                if ep["open"]:
                    ep["open"] = False
                    out.append(ep)
                else:
                    del self.eps[key]      # past the gap and already closed: cannot resume
        return out

    def drop(self, slug: str) -> List[dict]:
        """Board left the universe (resolved/delisted): close anything still open on it."""
        out = []
        for key in [k for k in self.eps if k[0] == slug]:
            ep = self.eps.pop(key)
            if ep["open"]:
                ep["open"] = False
                out.append(ep)
        return out

    def open_count(self) -> int:
        return sum(1 for e in self.eps.values() if e["open"])


# --------------------------------------------------------------------------- the feed
class WSFeed:
    def __init__(self, args):
        self.a = args
        self.books: Dict[str, TokenBook] = {}
        self.tok2slug: Dict[str, str] = {}
        self.by_slug: Dict[str, dict] = {}
        self.shards: List[List[str]] = []          # shard idx -> [slug]
        self.slug2shard: Dict[str, int] = {}
        self.shard_gen: List[int] = []             # bump to force that reader to resubscribe
        self.q: "asyncio.Queue[Tuple[float, str]]" = asyncio.Queue(maxsize=args.queue_max)
        self.dirty: Dict[str, Tuple[float, Optional[int]]] = {}
        self.resync_q: set = set()
        self.full_resync = False
        self.ep = EpisodeTracker(args.episode_gap)
        self.log_fh = None
        self.cap_fh = None
        self.cap_bytes = 0
        self.client: Optional[httpx.AsyncClient] = None
        self.readers: List[asyncio.Task] = []
        self.t_start = time.time()
        # Stamped on every row: restarts reset in-memory episode state, so the 24h diff
        # re-clusters detects across runs by slug+side under the 300s rule (audit MUST-FIX 2).
        self.run_id = _iso(self.t_start)
        self.stop = asyncio.Event()
        self.conns_up = 0
        self.c = dict(frames=0, events=0, book=0, pc=0, other=0, bad=0, unknown_asset=0,
                      dropped=0, reconnects=0, conn_err=0, resyncs=0, resync_tokens=0,
                      resync_missing=0,
                      gaps=0, gap_bid=0, gap_ask=0, gap_resyncs=0,
                      drift=0, spot_mismatch=0, spot_checks=0,
                      rescores=0, boards_scored=0, detects=0, ends=0, rescore_ms=0.0,
                      rescore_ms_max=0.0, qmax=0)
        self.hb_base = dict(self.c)

    # ---------------------------------------------------------------- logging
    def emit(self, row: dict) -> None:
        row.setdefault("source", "ws")
        row.setdefault("run", self.run_id)
        if self.log_fh:
            self.log_fh.write(json.dumps(row) + "\n")
            self.log_fh.flush()
        # --quiet keeps the systemd run log to structural events only; every row is in the JSONL.
        if not self.a.quiet or row.get("type") in ("start", "stop", "wsnote", "drift"):
            print(json.dumps(row)[:1400], flush=True)

    # ---------------------------------------------------------------- universe
    async def load_universe(self) -> List[dict]:
        """The scanner's own enumeration, via the scanner's own functions."""
        cache: Dict[str, dict] = {}
        for path in (TOKEN_CACHE, S.TOKEN_CACHE):     # ours first, the scanner's as a warm start
            try:
                with open(path) as f:
                    for k, v in (json.load(f) or {}).items():
                        cache.setdefault(k, v)
            except Exception:
                pass
        slugs = await S.enumerate_events(self.client, self.a.tag, self.a.max_events)
        missing = [s for s in slugs if s not in cache]
        if missing:
            sem = asyncio.Semaphore(8)

            async def one(s):
                async with sem:
                    return await S.fetch_event_tokens(self.client, s)
            for rec in await asyncio.gather(*[one(s) for s in missing]):
                if rec:
                    cache[rec["slug"]] = rec
            os.makedirs(os.path.dirname(TOKEN_CACHE) or ".", exist_ok=True)
            tmp = TOKEN_CACHE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, TOKEN_CACHE)              # atomic: never hand out a torn cache
        out = [cache[s] for s in slugs if s in cache]
        for ev in out:
            if "sanity" not in ev:
                ev["sanity"] = S.board_sanity(ev["legs"])
        return out

    def _shard_load(self, i: int) -> int:
        return sum(2 * len(self.by_slug[s]["legs"]) for s in self.shards[i] if s in self.by_slug)

    def reshard(self, events: List[dict]) -> Tuple[int, int, List[int]]:
        """Stable board->shard packing. Only shards whose membership changed get resubscribed."""
        new = {e["slug"]: e for e in events}
        gone = [s for s in self.by_slug if s not in new]
        added = [s for s in new if s not in self.by_slug]
        touched = set()
        for s in gone:
            i = self.slug2shard.pop(s, None)
            if i is not None and s in self.shards[i]:
                self.shards[i].remove(s)
                touched.add(i)
            for leg in self.by_slug[s]["legs"]:
                for t in (leg["yes"], leg["no"]):
                    self.books.pop(t, None)
                    self.tok2slug.pop(t, None)
            del self.by_slug[s]
            self.dirty.pop(s, None)
        for s, ev in new.items():
            self.by_slug[s] = ev                      # refresh legs/sanity in place
        for s in added:
            ev = new[s]
            need = 2 * len(ev["legs"])
            idx = next((i for i in range(len(self.shards))
                        if self._shard_load(i) + need <= SHARD_ASSETS), None)
            if idx is None:
                self.shards.append([])
                self.shard_gen.append(0)
                idx = len(self.shards) - 1
            self.shards[idx].append(s)
            self.slug2shard[s] = idx
            touched.add(idx)
            for leg in ev["legs"]:
                for t in (leg["yes"], leg["no"]):
                    self.tok2slug[t] = s
                    self.books.setdefault(t, TokenBook())
        return len(added), len(gone), sorted(touched)

    def shard_assets(self, i: int) -> List[str]:
        out = []
        for s in self.shards[i]:
            ev = self.by_slug.get(s)
            if ev:
                for leg in ev["legs"]:
                    out += [leg["yes"], leg["no"]]
        return out

    # ---------------------------------------------------------------- reader
    async def reader(self, i: int) -> None:
        backoff = 0.5
        while not self.stop.is_set():
            gen = self.shard_gen[i]
            assets = self.shard_assets(i)
            if not assets:
                await asyncio.sleep(2.0)
                continue
            try:
                async with websockets.connect(WS_URL, open_timeout=20, max_size=None,
                                              ping_interval=None, close_timeout=2) as ws:
                    self.conns_up += 1
                    backoff = 0.5
                    try:
                        await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                        # A fresh socket is a known gap: nothing that follows can be trusted
                        # until the REST authority lands, so buffer these tokens' deltas.
                        self.mark_resync(assets)
                        last_ping = last_rx = time.time()
                        while not self.stop.is_set() and self.shard_gen[i] == gen:
                            now = time.time()
                            if now - last_ping >= PING_EVERY:
                                await ws.send("PING")
                                last_ping = now
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            except asyncio.TimeoutError:
                                if time.time() - last_rx > SILENCE_S:
                                    raise RuntimeError(f"silent for {SILENCE_S:.0f}s")
                                continue
                            last_rx = time.time()
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", "replace")
                            self.push(last_rx, raw)
                    finally:
                        self.conns_up = max(0, self.conns_up - 1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.c["conn_err"] += 1
                if not self.stop.is_set():
                    self.emit({"type": "wsnote", "ts": _iso(time.time()), "shard": i,
                               "what": "disconnect", "err": f"{type(e).__name__}: {e}"[:200]})
            if self.stop.is_set():
                return
            self.c["reconnects"] += 1
            await asyncio.sleep(backoff + random.random() * 0.3)
            backoff = min(30.0, backoff * 2)

    def push(self, recv_ts: float, raw: str) -> None:
        """Bounded, drop-OLDEST. On a 0.7s decay window a stale frame is worth less than a fresh
        one — but dropping means missed level updates, so a drop also arms a full REST resync."""
        if self.cap_fh is not None and self.cap_bytes < self.a.capture_bytes:
            self._capture(recv_ts, raw)
        try:
            self.q.put_nowait((recv_ts, raw))
        except asyncio.QueueFull:
            self.c["dropped"] += 1
            self.full_resync = True
            try:
                self.q.get_nowait()
                self.q.put_nowait((recv_ts, raw))
            except Exception:
                pass
        n = self.q.qsize()
        if n > self.c["qmax"]:
            self.c["qmax"] = n

    def _capture(self, recv_ts: float, raw: str) -> None:
        """Tee real frames to disk for the offline tests, bounded.

        The initial dump is a single ~1MB array frame, which would eat the whole budget, so it is
        written TRIMMED to its first few book objects. The shape is untouched — still a JSON array
        of real `book` events — only the number of entries is reduced, and the fixture row is
        marked `trimmed` so the tests do not mistake it for a whole dump.
        """
        trimmed = False
        if len(raw) > 40000:
            try:
                d = json.loads(raw)
            except Exception:
                return
            if not isinstance(d, list) or not d:
                return
            raw = json.dumps(d[:4])
            trimmed = True
        row = {"recv_ts": recv_ts, "raw": raw}
        if trimmed:
            row["trimmed"] = True
        self.cap_fh.write(json.dumps(row) + "\n")
        self.cap_bytes += len(raw)

    # ---------------------------------------------------------------- frame handling
    def mark_resync(self, tokens) -> None:
        for t in tokens:
            b = self.books.get(t)
            if b is None:
                continue
            if b.pending is None:
                b.pending = []
            self.resync_q.add(t)

    def apply_frame(self, recv_ts: float, raw: str) -> None:
        s = raw.strip()
        if not s or s in ("PONG", "PING"):
            return
        try:
            d = json.loads(s)
        except Exception:
            self.c["bad"] += 1
            return
        for it in (d if isinstance(d, list) else [d]):
            if not isinstance(it, dict):
                self.c["bad"] += 1
                continue
            try:
                self.apply_event(recv_ts, it)
            except Exception:
                self.c["bad"] += 1

    def apply_event(self, recv_ts: float, it: dict) -> None:
        et = it.get("event_type")
        try:
            ex_ts = int(it["timestamp"]) if it.get("timestamp") is not None else None
        except (TypeError, ValueError):
            ex_ts = None
        self.c["events"] += 1
        if et == "book":
            a = it.get("asset_id")
            b = self.books.get(a)
            if b is None:
                self.c["unknown_asset"] += 1
                return
            self.c["book"] += 1
            bids, asks = _levels(it.get("bids")), _levels(it.get("asks"))
            if b.pending is not None:
                b.pending.append(("snap", bids, asks))
                if len(b.pending) > MAX_PENDING:
                    del b.pending[:-MAX_PENDING]
                    self.resync_q.add(a)      # replay is now lossy: resync again after this one
                return
            b.snapshot(bids, asks)
            b.ex_ts = ex_ts
            self.touch(a, recv_ts, ex_ts)
        elif et == "price_change":
            self.c["pc"] += 1
            changes = it.get("price_changes")
            if changes is None:
                changes = [it] if it.get("asset_id") else []
            tops: Dict[str, tuple] = {}
            for pc in changes:
                if not isinstance(pc, dict):
                    self.c["bad"] += 1
                    continue
                a = pc.get("asset_id")
                b = self.books.get(a)
                if b is None:
                    self.c["unknown_asset"] += 1
                    continue
                try:
                    price, size = float(pc["price"]), float(pc["size"])
                except (KeyError, TypeError, ValueError):
                    self.c["bad"] += 1
                    continue
                side = "BUY" if str(pc.get("side", "")).upper() == "BUY" else "SELL"
                if b.pending is not None:
                    b.pending.append(("lvl", side, price, size))
                    if len(b.pending) > MAX_PENDING:
                        del b.pending[:-MAX_PENDING]
                        self.resync_q.add(a)  # replay is now lossy: resync again after this one
                    continue
                b.set_level(side, price, size)
                b.ex_ts = ex_ts
                tops[a] = (pc.get("best_bid"), pc.get("best_ask"))
                self.touch(a, recv_ts, ex_ts)
            # GAP DETECTOR — the frame states the exchange's own top of book AFTER the change.
            # There is no sequence number on this feed, so this is the only in-band way to notice
            # a missed update. Two consecutive disagreements (not one, which races a same-instant
            # update) force a REST resync of that token.
            for a, (bb, ba) in tops.items():
                b = self.books[a]
                if not b.seeded:
                    continue
                dbid, dask = _disagrees(b.best_bid(), bb), _disagrees(b.best_ask(), ba)
                if dbid:
                    self.c["gap_bid"] += 1
                if dask:
                    self.c["gap_ask"] += 1
                if dbid or dask:
                    self.c["gaps"] += 1
                    b.strikes += 1
                    if b.strikes >= GAP_STRIKES:
                        self.c["gap_resyncs"] += 1
                        self.mark_resync([a])
                else:
                    b.strikes = 0
        elif et is None:
            self.c["bad"] += 1
        else:
            self.c["other"] += 1                      # last_trade_price / tick_size_change / ...

    def touch(self, token: str, recv_ts: float, ex_ts) -> None:
        slug = self.tok2slug.get(token)
        if slug is None:
            return
        prev = self.dirty.get(slug)
        # ws_recv_ts must be the EARLIEST unprocessed arrival that dirtied the board — that is
        # the honest "when did this information reach us".
        self.dirty[slug] = ((prev[0] if prev and prev[0] < recv_ts else recv_ts), ex_ts)

    # ---------------------------------------------------------------- scoring loop
    async def processor(self) -> None:
        coalesce = self.a.coalesce_ms / 1000.0
        last = 0.0
        while not self.stop.is_set():
            try:
                item = await asyncio.wait_for(self.q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                item = None
            if item is not None:
                self.c["frames"] += 1
                self.apply_frame(*item)
                for _ in range(8000):     # drain what is already buffered: that IS the coalescing
                    try:
                        nxt = self.q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self.c["frames"] += 1
                    self.apply_frame(*nxt)
            now = time.time()
            if self.dirty and (now - last) >= coalesce:
                self.rescore(now)
                last = time.time()

    def rescore(self, now: float) -> None:
        t0 = time.perf_counter()
        dirty, self.dirty = self.dirty, {}
        cap = self.a.ask_cap
        books = self.books

        def asks_capped(t):
            b = books.get(t)
            if b is None or not b.seeded:
                return None
            return b.ladder(cap) or None

        def asks_full(t):
            b = books.get(t)
            if b is None or not b.seeded:
                return None
            return b.ladder(None) or None

        n = 0
        for slug, (recv_ts, ex_ts) in dirty.items():
            ev = self.by_slug.get(slug)
            if ev is None:
                continue
            # Never score a board that is mid-resync: a torn book reads as "not profitable" and
            # would emit a false episode_end.
            ready = False
            torn = False
            for leg in ev["legs"]:
                for t in (leg["yes"], leg["no"]):
                    b = books.get(t)
                    if b is None:
                        continue
                    if b.pending is not None:
                        torn = True
                        break
                    if b.seeded:
                        ready = True
                if torn:
                    break
            if torn or not ready:
                continue
            n += 1
            san = ev.get("sanity") or {}
            gb, gs = gate(ev["legs"], asks_capped, san)
            res = None
            if gb or gs:
                res = score_board(ev["legs"], asks_capped, san, want_buy=gb, want_short=gs)
                if res and (res.get("buy") or res.get("short")):
                    # rare path: re-score at FULL depth so the logged row is exactly what the
                    # sweep scanner would have produced, cap or no cap
                    res = score_board(ev["legs"], asks_full, san,
                                      want_buy=gb, want_short=gs) or res
            for side in ("buy", "short"):
                prof = (res or {}).get(side)
                for kind, ep in self.ep.observe(slug, side, prof, now):
                    if kind == "new":
                        self.c["detects"] += 1
                        self.emit(self.detect_row(ep, res, prof, now, recv_ts, ex_ts))
                    else:
                        self.c["ends"] += 1
                        self.emit(self.end_row(ep, now, recv_ts, ex_ts, "gone"))
        self.c["rescores"] += 1
        self.c["boards_scored"] += n
        ms = (time.perf_counter() - t0) * 1000.0
        self.c["rescore_ms"] += ms
        if ms > self.c["rescore_ms_max"]:
            self.c["rescore_ms_max"] = ms

    def detect_row(self, ep, res, prof, now, recv_ts, ex_ts) -> dict:
        row = {"type": "detect", "ts": _iso(now), "ws_recv_ts": _iso(recv_ts),
               "lag_ms": round((now - recv_ts) * 1000.0, 1), "ex_ts": ex_ts,
               "slug": ep["slug"], "side": ep["side"], "ep": ep["id"],
               "n": res.get("n"), "legs_quoted": res.get("legs_quoted"),
               "complete": res.get("complete")}
        if "top_yes_ask_sum" in res:
            row["top_yes_ask_sum"] = res["top_yes_ask_sum"]
        for k in ("k", "cost", "gross", "fee", "profit", "roc", "legs", "executable"):
            if k in prof:
                row[k] = prof[k]
        if "sanity" in res:
            row["sanity"] = res["sanity"]
        return row

    def end_row(self, ep, now, recv_ts, ex_ts, reason) -> dict:
        return {"type": "episode_end", "ts": _iso(now),
                "ws_recv_ts": _iso(recv_ts) if recv_ts else None, "ex_ts": ex_ts,
                "slug": ep["slug"], "side": ep["side"], "ep": ep["id"],
                "dur_s": round(ep["last_prof"] - ep["start"], 3),
                "updates": ep["updates"], "resumes": ep["resumes"],
                "peak_profit": ep["peak"], "last_profit": ep["last_profit"],
                "reason": reason}

    # ---------------------------------------------------------------- REST resync
    async def resyncer(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(1.0)
            if self.full_resync:
                self.full_resync = False
                allt = list(self.books)
                self.mark_resync(allt)
                self.emit({"type": "wsnote", "ts": _iso(time.time()),
                           "what": "full_resync", "tokens": len(allt)})
            if not self.resync_q:
                continue
            batch, self.resync_q = list(self.resync_q), set()
            try:
                rest = await fetch_books(batch, self.client)
            except Exception as e:
                for t in batch:                       # retry next tick, keep buffering
                    self.resync_q.add(t)
                self.emit({"type": "wsnote", "ts": _iso(time.time()), "what": "resync_failed",
                           "err": f"{type(e).__name__}: {e}"[:160]})
                await asyncio.sleep(2.0)
                continue
            now = time.time()
            for t in batch:
                b = self.books.get(t)
                if b is None:
                    continue
                pend, b.pending = (b.pending or []), None
                lb = rest.get(t)
                if lb is None:
                    # REST omitted this token (fetch_books swallows chunk failures,
                    # orderbook.py) — the pre-gap book cannot be trusted. Unseed so scoring
                    # skips it, keep buffering deltas, retry next tick. Audit MUST-FIX 1:
                    # without this, up to a whole chunk resumes on stale books, phantom-capable.
                    b.seeded = False
                    b.pending = pend
                    self.resync_q.add(t)
                    self.c["resync_missing"] += 1
                    continue
                # fetch_books returns the full ASK ladder (what the optimiser walks) but only
                # best_bid — see the module docstring. Bids restart top-only after a resync.
                bb = lb.top.best_bid
                b.snapshot([(bb, 1.0)] if bb else [], list(lb.asks), bid_top_only=True)
                b.ex_ts = None
                for upd in pend:                      # replay what arrived during the round trip
                    if upd[0] == "snap":
                        b.snapshot(upd[1], upd[2])
                    else:
                        b.set_level(upd[1], upd[2], upd[3])
                if b.seeded:
                    self.touch(t, now, None)
            self.c["resyncs"] += 1
            self.c["resync_tokens"] += len(batch)

    # ---------------------------------------------------------------- spot check
    async def spotcheck(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(self.a.spot_every)
            if self.stop.is_set():
                return
            slugs = list(self.by_slug)
            if not slugs:
                continue
            pick = random.sample(slugs, min(self.a.spot_boards, len(slugs)))
            toks = [t for s in pick for leg in self.by_slug[s]["legs"]
                    for t in (leg["yes"], leg["no"])]

            def tops():
                return {t: (self.books[t].best_bid(), self.books[t].best_ask())
                        for t in toks
                        if t in self.books and self.books[t].seeded
                        and self.books[t].pending is None}
            before = tops()
            try:
                rest = await fetch_books(toks, self.client)
            except Exception:
                continue
            after = tops()
            now = time.time()
            self.c["spot_checks"] += 1
            n_cmp = 0
            for t, lb in rest.items():
                if t not in before or t not in after:
                    continue
                for j, side in ((0, "bid"), (1, "ask")):
                    r = lb.top.best_bid if j == 0 else lb.top.best_ask
                    bv, av = before[t][j], after[t][j]
                    n_cmp += 1
                    # A book that legitimately moved during the round trip matches one of the two
                    # bracketing WS reads; only a value matching NEITHER is drift.
                    if _eq(r, bv) or _eq(r, av):
                        continue
                    self.c["spot_mismatch"] += 1
                    diff = min(abs((r or 0.0) - (v or 0.0)) for v in (bv, av))
                    if diff <= TICK_TOL:
                        continue
                    self.c["drift"] += 1
                    self.emit({"type": "drift", "ts": _iso(now), "slug": self.tok2slug.get(t),
                               "asset": t, "side": side, "ws_before": bv, "ws_after": av,
                               "rest": r, "diff": round(diff, 6)})
                    self.mark_resync([t])
            self.emit({"type": "spot", "ts": _iso(now), "boards": len(pick),
                       "tokens": len(toks), "compared": n_cmp,
                       "mismatch": self.c["spot_mismatch"], "drift": self.c["drift"]})

    # ---------------------------------------------------------------- universe refresh
    async def refresher(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(self.a.refresh_every)
            if self.stop.is_set():
                return
            try:
                evs = await self.load_universe()
            except Exception as e:
                self.emit({"type": "wsnote", "ts": _iso(time.time()), "what": "refresh_failed",
                           "err": f"{type(e).__name__}: {e}"[:160]})
                continue
            if not evs:
                continue
            before = set(self.by_slug)
            add, gone, touched = self.reshard(evs)
            for slug in before - set(self.by_slug):
                for ep in self.ep.drop(slug):
                    self.c["ends"] += 1
                    self.emit(self.end_row(ep, time.time(), None, None, "delisted"))
            if add or gone:
                self.emit({"type": "wsnote", "ts": _iso(time.time()), "what": "universe",
                           "boards": len(self.by_slug), "added": add, "removed": gone,
                           "shards": len(self.shards), "resub": touched})
                for i in touched:
                    self.shard_gen[i] += 1            # forces reconnect + resubscribe
                while len(self.readers) < len(self.shards):
                    self.readers.append(asyncio.create_task(self.reader(len(self.readers))))

    # ---------------------------------------------------------------- heartbeat
    async def heartbeat(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(self.a.hb_every)
            now = time.time()
            for ep in self.ep.expire(now):
                self.c["ends"] += 1
                self.emit(self.end_row(ep, now, None, None, "stale"))
            _trim()
            self.emit(self.hb_row(now))

    def hb_row(self, now: float) -> dict:
        d = {k: (self.c[k] - self.hb_base[k]) for k in self.c}
        self.hb_base = dict(self.c)
        ru = resource.getrusage(resource.RUSAGE_SELF)
        w = max(1e-9, self.a.hb_every)
        row = {
            "type": "hb", "ts": _iso(now), "up_s": round(now - self.t_start, 1),
            "boards": len(self.by_slug), "tokens": len(self.books),
            "books_seeded": sum(1 for b in self.books.values() if b.seeded),
            "bid_top_only": sum(1 for b in self.books.values() if b.bid_top_only),
            "shards": len(self.shards), "conns_up": self.conns_up,
            "frames_s": round(d["frames"] / w, 1), "events_s": round(d["events"] / w, 1),
            "pc_s": round(d["pc"] / w, 1), "book_s": round(d["book"] / w, 2),
            "other": d["other"], "bad": d["bad"], "unknown_asset": d["unknown_asset"],
            "dropped": d["dropped"], "q_max": self.c["qmax"], "q_now": self.q.qsize(),
            "reconnects": d["reconnects"], "conn_err": d["conn_err"],
            "resyncs": d["resyncs"], "resync_tokens": d["resync_tokens"],
            "gaps": d["gaps"], "gap_bid": d["gap_bid"], "gap_ask": d["gap_ask"],
            "gap_resyncs": d["gap_resyncs"],
            "drift": d["drift"], "spot_mismatch": d["spot_mismatch"],
            "spot_checks": d["spot_checks"],
            "rescores_s": round(d["rescores"] / w, 1),
            "boards_scored_s": round(d["boards_scored"] / w, 1),
            "rescore_ms_mean": round(d["rescore_ms"] / d["rescores"], 3) if d["rescores"] else 0.0,
            "rescore_ms_max": round(self.c["rescore_ms_max"], 2),
            "cpu_s": round(ru.ru_utime + ru.ru_stime, 1),
            "cpu_pct": round(100 * (ru.ru_utime + ru.ru_stime) / max(1e-9, now - self.t_start), 1),
            "rss_mb": round(_rss_mb(), 1), "rss_peak_mb": round(ru.ru_maxrss / 1024.0, 1),
            "open_eps": self.ep.open_count(), "detects": d["detects"], "ends": d["ends"],
        }
        self.c["qmax"] = 0
        return row

    # ---------------------------------------------------------------- run
    async def run(self) -> None:
        os.makedirs("logs", exist_ok=True)
        self.log_fh = None if self.a.no_log else open(self.a.log, "a")
        if self.a.capture:
            os.makedirs(os.path.dirname(self.a.capture) or ".", exist_ok=True)
            self.cap_fh = open(self.a.capture, "w")
        limits = httpx.Limits(max_connections=12)
        async with httpx.AsyncClient(timeout=20, headers=S.HDR, limits=limits) as client:
            self.client = client
            evs = await self.load_universe()
            self.reshard(evs)
            del evs
            _trim()
            self.emit({"type": "start", "ts": _iso(time.time()), "boards": len(self.by_slug),
                       "tokens": len(self.books), "shards": len(self.shards),
                       "shard_assets": SHARD_ASSETS, "tag": self.a.tag,
                       "coalesce_ms": self.a.coalesce_ms, "ask_cap": self.a.ask_cap,
                       "episode_gap_s": self.a.episode_gap, "pid": os.getpid()})
            self.readers = [asyncio.create_task(self.reader(i)) for i in range(len(self.shards))]
            others = [asyncio.create_task(f()) for f in
                      (self.processor, self.resyncer, self.spotcheck, self.refresher,
                       self.heartbeat)]
            try:
                if self.a.duration:
                    await asyncio.sleep(self.a.duration)
                else:
                    await self.stop.wait()
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            self.stop.set()
            for t in self.readers + others:
                t.cancel()
            await asyncio.gather(*self.readers, *others, return_exceptions=True)
        now = time.time()
        self.emit({**self.hb_row(now), "type": "stop"})
        if self.log_fh:
            self.log_fh.close()
        if self.cap_fh:
            self.cap_fh.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="negRisk WS shadow detector (logs only, never trades)")
    ap.add_argument("--log", default=LOG_PATH)
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--duration", type=float, default=0, help="seconds; 0 = run forever")
    ap.add_argument("--tag", default="daily-temperature")
    ap.add_argument("--max-events", type=int, default=600)
    ap.add_argument("--coalesce-ms", type=float, default=20.0,
                    help="minimum gap between rescore passes; the detection resolution limit")
    ap.add_argument("--ask-cap", type=int, default=32,
                    help="ask levels handed to the hot-path scorer; a profitable board is always "
                         "re-scored uncapped before its row is written")
    ap.add_argument("--episode-gap", type=float, default=EPISODE_GAP_S)
    ap.add_argument("--refresh-every", type=float, default=900.0)
    ap.add_argument("--spot-every", type=float, default=300.0)
    ap.add_argument("--spot-boards", type=int, default=4)
    ap.add_argument("--hb-every", type=float, default=60.0)
    ap.add_argument("--queue-max", type=int, default=20000)
    ap.add_argument("--capture", default=None, help="tee raw frames to this path (test fixtures)")
    ap.add_argument("--capture-bytes", type=int, default=250_000)
    ap.add_argument("--quiet", action="store_true")
    return ap


async def main() -> None:
    await WSFeed(build_parser().parse_args()).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
