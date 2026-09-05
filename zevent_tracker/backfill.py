"""Import raw JSON dumps (RAW_DIR/YYYY-MM-DD/HHMMSS.json) that are missing from the database.

Runs at collector start (in a background thread) and via `python main.py backfill`.
Deduplication: a file is skipped when a snapshot with its timestamp already exists,
and the inserts themselves use ON CONFLICT DO NOTHING, so re-running is always safe.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from .db import insert, recompute
from .parse import parse

log = logging.getLogger("zevent.backfill")


def find_raw_files(raw_dir: Path) -> list[tuple[datetime, Path]]:
    """All raw dumps under raw_dir with the UTC timestamp encoded in their path, oldest first."""
    if not raw_dir.is_dir():
        return []
    found = []
    for day_dir in sorted(p for p in raw_dir.iterdir() if p.is_dir()):
        for f in sorted(day_dir.glob("*.json")):
            try:
                ts = datetime.strptime(f"{day_dir.name} {f.stem}", "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            found.append((ts, f))
    return found


def backfill(raw_dir: Path, database_url: str) -> dict[str, int]:
    """Insert every raw dump whose timestamp is not yet in `snapshot`. Returns counters."""
    files = find_raw_files(raw_dir)
    counts = {"imported": 0, "skipped": 0, "failed": 0}
    if not files:
        log.info("backfill: no raw files under %s", raw_dir)
        return counts
    # autocommit so each file's insert() is its own committed transaction. Without it the first SELECT
    # opens an implicit transaction, every insert() becomes a savepoint inside it, nothing is durable
    # until the connection closes, and the 340-row streamer table bloats with one dead version per
    # upsert per file (a full re-import went from minutes to tens of minutes at 100% CPU).
    with psycopg.connect(database_url, autocommit=True) as conn:
        existing = {row[0] for row in conn.execute("SELECT ts FROM snapshot")}
        todo = [(ts, f) for ts, f in files if ts not in existing]
        counts["skipped"] = len(files) - len(todo)
        log.info("backfill: %d raw files, %d already in db, %d to import", len(files), counts["skipped"], len(todo))
        for i, (ts, f) in enumerate(todo, 1):
            try:
                insert(conn, parse(json.loads(f.read_bytes()), ts), derive=False)
                counts["imported"] += 1
            except Exception as e:  # corrupt file, bad shape: log and move on
                counts["failed"] += 1
                log.warning("backfill: %s failed: %s", f, e)
            if i % 200 == 0:
                log.info("backfill: %d/%d", i, len(todo))
        if counts["imported"]:
            # derived facts from the oldest imported minute on (later rows may have new predecessors)
            start = min(ts for ts, _ in todo)
            log.info("backfill: recomputing derived facts from %s", start.isoformat())
            with conn.transaction():
                recompute(conn, start)
    log.info("backfill done: %s", counts)
    return counts
