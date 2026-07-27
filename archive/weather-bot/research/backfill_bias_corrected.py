"""One-off backfill: populate Trade.bias_corrected for weather trades created
before the column existed.

New trades record at entry whether a per-station bias was applied to their
city+metric (the scoreboard cohort tag). Older rows are NULL. This derives the
city+metric from each trade's event slug and tags it using the SAME predicate the
live path uses (is_bias_corrected), so old and new rows agree.

Note: this tags old rows against the CURRENT station_bias.json. That's correct
here because those trades were placed under the current bias table; only re-run a
bias backfill that changes a city's status would make this approximate for very
old rows (new trades are always tagged at their own entry time, so they stay exact).

Run:  python -m backend.data.backfill_bias_corrected
Safe to re-run; only touches weather trades whose bias_corrected is NULL.
"""
from backend.models.database import SessionLocal, Trade
from backend.data.weather_markets import parse_event_slug
from backend.data.weather import is_bias_corrected


def backfill() -> None:
    db = SessionLocal()
    try:
        weather = db.query(Trade).filter(Trade.market_type == "weather").all()
        todo = [t for t in weather if t.bias_corrected is None]
        if not todo:
            print("Nothing to backfill — all weather trades already tagged.")
            return

        print(f"Backfilling {len(todo)} trade(s)...")
        filled = skipped = 0
        for t in todo:
            parsed = parse_event_slug(t.event_slug or "")
            if not parsed:
                print(f"  trade {t.id}: unparseable slug {t.event_slug!r} — left NULL")
                skipped += 1
                continue
            city_key, metric, _td = parsed
            tag = is_bias_corrected(city_key, metric)
            t.bias_corrected = tag
            filled += 1
            print(f"  trade {t.id}: {city_key}/{metric} -> {'corrected' if tag else 'UNcorrected'}")
        db.commit()
        print(f"Done. Tagged {filled}, left NULL {skipped}.")
    finally:
        db.close()


if __name__ == "__main__":
    backfill()
