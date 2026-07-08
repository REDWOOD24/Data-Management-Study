#!/usr/bin/env python3
"""Export simulation events from output/events.db to CSV."""

import argparse
import csv
import sqlite3
from pathlib import Path


DEFAULT_DB = Path(__file__).resolve().parent.parent / "output" / "events.db"
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "output" / "events.csv"
EVENTS_TABLE = "EVENTS"


def export_events(db_path: Path, csv_path: Path) -> int:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    csv_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(f"SELECT * FROM {EVENTS_TABLE} ORDER BY _ID")

        rows = cursor.fetchall()
        if not rows:
            fieldnames = [description[0] for description in cursor.description or []]
        else:
            fieldnames = rows[0].keys()

        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert output/events.db into a CSV file."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to the SQLite events database (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_CSV,
        help=f"Path to the output CSV file (default: {DEFAULT_CSV})",
    )
    args = parser.parse_args()

    row_count = export_events(args.db, args.output)
    print(f"Wrote {row_count} rows to {args.output}")


if __name__ == "__main__":
    """
    Example Usage: 
       python3 scripts/export_events_to_csv.py \
       --db /path/to/events.db \
       --output /path/to/events.csv
    """
    main()
