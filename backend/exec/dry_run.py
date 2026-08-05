"""C7 dry run: prove the order path end-to-end with no fillable order.

    python -m backend.exec.dry_run --live-dry-run

Refuses to do anything without ``--live-dry-run``. With the flag but no key it prints the
exact missing variables and exits 1 — the expected state on this box, which has no Polygon
private key by design.

What the live path does, in order (``_live_dry_run`` below is the whole of it — every rail
is inside OrderClient, so reading that one function is enough to audit the exposure):

  1. pick a negRisk board resolving tomorrow, from the scanner's token cache;
  2. read each candidate leg's book and keep the ones whose best bid is high enough that a
     $0.01–0.02 order sits provably under half the bid;
  3. ``post_single`` one BUY;
  4. ``post_batch`` two more in ONE ``POST /orders`` (the production batch shape);
  5. print every PostResult, the open-order count, then ``cancel_all`` and print it again.

Order type defaults to GTC here, NOT the production FOK: a far-from-market FOK is killed on
arrival, so it proves signing and auth but never rests, and there is nothing to cancel. GTC
proves the whole loop — accepted, resting, cancelled — which is what C7 is for. The orders
that rest are ≤ 5 shares at ≤ $0.02 under half the bid, ≤ $1.00 total, cancelled on exit.
Pass ``--order-type FOK`` to exercise the production type instead.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Optional

TOKEN_CACHE = "logs/negrisk_tokens_v2.json"   # written by the scanner; read-only here
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def _date_suffix(day: datetime.date) -> str:
    """The scanner's slugs end '-on-<month>-<d>-<yyyy>' (no zero padding)."""
    return f"-on-{MONTHS[day.month - 1]}-{day.day}-{day.year}"


def load_boards(path: str = TOKEN_CACHE) -> list[dict]:
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} not found — it is written by negrisk-arb.service. Run the scanner "
            "once, or pass --slug with an explicit board."
        )
    with open(path) as fh:
        return list(json.load(fh).values())


def pick_board(boards: list[dict], day: datetime.date, slug: Optional[str] = None) -> dict:
    if slug:
        for b in boards:
            if b.get("slug") == slug:
                return b
        raise SystemExit(f"slug {slug!r} not in the token cache")
    suffix = _date_suffix(day)
    hits = [b for b in boards if str(b.get("slug", "")).endswith(suffix)]
    if not hits:
        raise SystemExit(f"no board in the cache resolves on {day} (suffix {suffix!r})")
    # Most legs = the fullest board, so there is the widest choice of liquid legs.
    return max(hits, key=lambda b: len(b.get("legs", [])))


def pick_legs(oc, board: dict, price: float, need: int) -> list[tuple[str, float]]:
    """Legs whose best bid clears the rail with margin, most liquid first.

    Requires best_bid > 2*price by the guard; we demand 2.5x so a book that ticks down
    between selection and post does not turn a rail check into a failed run.
    """
    out = []
    for leg in board.get("legs", []):
        token_id = leg["yes"]
        try:
            best_bid, _ = oc.top_of_book(token_id)
        except Exception as exc:  # noqa: BLE001 — a dead leg is data
            print(f"  book fetch failed for {leg.get('t','?')}: {exc}")
            continue
        if best_bid is not None and best_bid > 2.5 * price:
            out.append((token_id, best_bid, leg.get("t", "?")))
    out.sort(key=lambda r: -r[1])
    if len(out) < need:
        raise SystemExit(
            f"only {len(out)} leg(s) on {board['slug']} have a bid above {2.5 * price:.4f}; "
            f"need {need}. Pick another board or lower --price."
        )
    for token_id, best_bid, label in out[:need]:
        print(f"  leg {label!r}: best bid {best_bid:.3f}, token …{token_id[-8:]}")
    return [(t, b) for t, b, _ in out[:need]]


def _show(tag: str, r) -> None:
    print(f"  {tag}: ok={r.ok} status={r.status} id={r.order_id} "
          f"err={r.error} latency={r.latency_ms:.1f}ms")


def _live_dry_run(args) -> int:
    """The entire authenticated path. Nothing here enforces safety — OrderClient does."""
    from .order_client import OrderClient, OrderSpec

    # STRICT_DEFAULT guard — the CLI has no flag that can loosen it.
    oc = OrderClient.from_env(env_file=args.env_file)
    print(f"address       : {oc.client.get_address()}")
    print(f"guard         : {oc.guard}")

    day = (datetime.date.fromisoformat(args.date) if args.date
           else datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=1))
    board = pick_board(load_boards(), day, args.slug)
    print(f"board         : {board['slug']} ({len(board['legs'])} legs)")
    legs = pick_legs(oc, board, args.price, need=3)

    with oc:                                     # exit cancels whatever is still resting
        single, sign_ms = oc.build_signed(
            OrderSpec(token_id=legs[0][0], side="BUY", price=args.price, size=args.size),
            args.order_type,
        )
        print(f"signed 1 order in {sign_ms:.1f}ms")
        _show("post_single", oc.post_single(single, args.order_type))

        batch = []
        for token_id, _ in legs[1:3]:
            s, ms = oc.build_signed(
                OrderSpec(token_id=token_id, side="BUY", price=args.price, size=args.size),
                args.order_type,
            )
            print(f"signed batch leg in {ms:.1f}ms")
            batch.append(s)
        for i, r in enumerate(oc.post_batch(batch, args.order_type)):
            _show(f"post_batch[{i}]", r)

        print(f"notional committed: ${oc.notional_spent:.4f} of ${oc.guard.max_notional:.2f}")
        print(f"open orders before cancel: {oc.open_order_count()}")
        print(f"cancel_all cancelled     : {oc.cancel_all()}")
        print(f"open orders after cancel : {oc.open_order_count()}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m backend.exec.dry_run", description=__doc__)
    p.add_argument("--live-dry-run", action="store_true",
                   help="required; without it this command does nothing")
    p.add_argument("--price", type=float, default=0.01,
                   help="limit price per share (guard caps it at 0.02)")
    p.add_argument("--size", type=float, default=5.0, help="shares per order (guard caps at 5)")
    p.add_argument("--order-type", default="GTC", choices=["GTC", "FOK", "GTD", "FAK"],
                   help="GTC so the order rests and can be cancelled; FOK is the production type")
    p.add_argument("--slug", default=None, help="explicit board slug instead of tomorrow's")
    p.add_argument("--date", default=None, help="YYYY-MM-DD resolution date (default: tomorrow UTC)")
    p.add_argument("--env-file", default=".env",
                   help="where the POLYMARKET_* vars live (a missing file = no key)")
    args = p.parse_args(argv)

    if not args.live_dry_run:
        print("refusing to run: this command posts REAL (guarded, non-marketable) orders.\n"
              "Re-run with --live-dry-run if that is what you want.")
        return 2

    from .clob_auth import MissingCredentials, load_clob_env

    try:
        load_clob_env(args.env_file)
    except MissingCredentials as exc:
        print("cannot run the authenticated dry run — missing environment variable(s):")
        for var in exc.missing:
            print(f"  {var}")
        print("Set them in .env (documented in .env.example). No key exists on this box "
              "by design; everything else in Tier C runs without one.")
        return 1
    except ValueError as exc:
        print(f"invalid credential configuration: {exc}")
        return 1

    return _live_dry_run(args)


if __name__ == "__main__":
    sys.exit(main())
