from __future__ import annotations

import psycopg

from .parse import Parsed

SNAPSHOT_SQL = """
INSERT INTO snapshot (ts, donation_total, viewers_total, streamers_total, streamers_online)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (ts) DO NOTHING
"""

STREAMER_SQL = """
INSERT INTO streamer (twitch_id, login, display, profile_url, donation_url, first_seen, last_seen)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (twitch_id) DO UPDATE SET
  login = EXCLUDED.login,
  display = EXCLUDED.display,
  profile_url = EXCLUDED.profile_url,
  donation_url = EXCLUDED.donation_url,
  last_seen = EXCLUDED.last_seen
"""

SAMPLE_SQL = """
INSERT INTO streamer_sample (ts, twitch_id, online, game, viewers, donation_total)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (twitch_id, ts) DO NOTHING
"""


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
            [(x.twitch_id, x.login, x.display, x.profile_url, x.donation_url, x.ts, x.ts) for x in parsed.samples],
        )
        cur.executemany(
            SAMPLE_SQL,
            [(x.ts, x.twitch_id, x.online, x.game, x.viewers, x.donation_total) for x in parsed.samples],
        )
