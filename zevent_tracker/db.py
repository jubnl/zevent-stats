from __future__ import annotations

import psycopg

from .parse import Parsed

SNAPSHOT_SQL = """
INSERT INTO snapshot (ts, donation_total, viewers_total, streamers_total, streamers_online)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (ts) DO NOTHING
"""

# Order-independent upsert: first_seen/last_seen are the extremes of every sample ever seen, and the
# descriptive fields come from the newest sample. This keeps a backfill of old dumps running next to
# the live collector (or after it, on a fresh database) from stamping the wrong first_seen or
# overwriting a current display name with an older one.
STREAMER_SQL = """
INSERT INTO streamer (twitch_id, login, display, profile_url, donation_url, location, first_seen, last_seen)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (twitch_id) DO UPDATE SET
  login        = CASE WHEN EXCLUDED.last_seen >= streamer.last_seen THEN EXCLUDED.login        ELSE streamer.login        END,
  display      = CASE WHEN EXCLUDED.last_seen >= streamer.last_seen THEN EXCLUDED.display      ELSE streamer.display      END,
  profile_url  = CASE WHEN EXCLUDED.last_seen >= streamer.last_seen THEN EXCLUDED.profile_url  ELSE streamer.profile_url  END,
  donation_url = CASE WHEN EXCLUDED.last_seen >= streamer.last_seen THEN EXCLUDED.donation_url ELSE streamer.donation_url END,
  location     = CASE WHEN EXCLUDED.last_seen >= streamer.last_seen THEN EXCLUDED.location     ELSE streamer.location     END,
  first_seen   = least(streamer.first_seen, EXCLUDED.first_seen),
  last_seen    = greatest(streamer.last_seen, EXCLUDED.last_seen)
"""

SAMPLE_SQL = """
INSERT INTO streamer_sample (ts, twitch_id, online, game, viewers, donation_total)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (twitch_id, ts) DO NOTHING
"""

# Idempotent schema additions for databases created before the column existed. db/init.sql has the full
# schema for a fresh database; this list is what the collector applies to an existing one at startup.
SCHEMA_UPDATES = [
    "ALTER TABLE streamer ADD COLUMN IF NOT EXISTS location text",  # added 2026-09-05
]


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.transaction(), conn.cursor() as cur:
        for stmt in SCHEMA_UPDATES:
            cur.execute(stmt)


def insert(conn: psycopg.Connection, parsed: Parsed) -> None:
    """Write one tick (snapshot + streamer upserts + samples) in a single transaction."""
    s = parsed.snapshot
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            SNAPSHOT_SQL,
            (s.ts, s.donation_total, s.viewers_total, s.streamers_total, s.streamers_online),
        )
        cur.executemany(
            STREAMER_SQL,
            [(x.twitch_id, x.login, x.display, x.profile_url, x.donation_url, x.location, x.ts, x.ts) for x in parsed.samples],
        )
        cur.executemany(
            SAMPLE_SQL,
            [(x.ts, x.twitch_id, x.online, x.game, x.viewers, x.donation_total) for x in parsed.samples],
        )
