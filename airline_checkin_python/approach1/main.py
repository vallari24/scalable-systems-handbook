from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airlinecheckin.config import load_config
from airlinecheckin.db import connection
from airlinecheckin.repository import AirlineRepository

LOCK_CLAUSE = ""


def main() -> int:
    config = load_config()
    with connection(config) as conn:
        repo = AirlineRepository(conn)
        trips = repo.list_trips()
        seat = (
            repo.fetch_next_available_seat(
                trip_id=trips[0].id,
                lock_clause=LOCK_CLAUSE,
            )
            if trips
            else None
        )
        print("approach1")
        print(f"trips={len(trips)} next_seat={seat}")
        print("TODO: add simple-select booking logic here")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
