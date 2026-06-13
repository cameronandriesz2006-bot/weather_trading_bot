"""One-off backfill: populate Trade.bucket_label for weather trades created
before the column existed.

New trades store the exact range (e.g. "88-89°F") at creation, but older rows
have it null. This looks each one up on Polymarket by market id and fills in the
groupItemTitle so the dashboard can show which range was bet.

Run:  python -m backend.data.backfill_trade_buckets
Safe to re-run; only touches weather trades whose bucket_label is empty.
"""
import asyncio
import httpx

from backend.models.database import SessionLocal, Trade

MARKET_URL = "https://gamma-api.polymarket.com/markets/"


async def backfill() -> None:
    db = SessionLocal()
    try:
        weather = db.query(Trade).filter(Trade.market_type == "weather").all()
        todo = [t for t in weather if not t.bucket_label]
        if not todo:
            print("Nothing to backfill — all weather trades already have a bucket label.")
            return

        print(f"Backfilling {len(todo)} trade(s)...")
        filled = 0
        async with httpx.AsyncClient(timeout=15.0) as client:
            for t in todo:
                try:
                    r = await client.get(f"{MARKET_URL}{t.market_ticker}")
                    if r.status_code != 200:
                        print(f"  trade {t.id}: market {t.market_ticker} -> HTTP {r.status_code}")
                        continue
                    label = r.json().get("groupItemTitle")
                    if label:
                        t.bucket_label = label
                        filled += 1
                        print(f"  trade {t.id}: {label}")
                    else:
                        print(f"  trade {t.id}: no groupItemTitle")
                except Exception as e:
                    print(f"  trade {t.id}: error {e}")
        db.commit()
        print(f"Done. Filled {filled}/{len(todo)}.")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(backfill())
