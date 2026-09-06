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

# Idempotent schema additions for databases created before the columns existed. db/init.sql has the full
# schema for a fresh database; this list is what the collector applies to an existing one at startup.
SCHEMA_UPDATES = [
    "ALTER TABLE streamer ADD COLUMN IF NOT EXISTS location text",  # added 2026-09-05
    # Derived per-row facts (added 2026-09-06), filled by recompute() after every insert so the dashboards
    # sum plain columns instead of running window functions over the whole history:
    #   gain      donation_total minus the streamer's previous sample (NULL on the first sample)
    #   gap_s     seconds since the streamer's previous sample (NULL on the first sample)
    #   dup       the part of MisterMV's counter that was not his (see db/views.sql), NULL elsewhere
    #   dup_gain  its increment at this sample
    #   rank      position by (donation_total - dup) among the streamers sampled at this ts
    "ALTER TABLE streamer_sample ADD COLUMN IF NOT EXISTS gain numeric(12,2)",
    "ALTER TABLE streamer_sample ADD COLUMN IF NOT EXISTS gap_s integer",
    "ALTER TABLE streamer_sample ADD COLUMN IF NOT EXISTS dup numeric(12,2)",
    "ALTER TABLE streamer_sample ADD COLUMN IF NOT EXISTS dup_gain numeric(12,2)",
    "ALTER TABLE streamer_sample ADD COLUMN IF NOT EXISTS rank integer",
    # Added 2026-09-06 for the insights dashboard (viewer spikes, longest live sessions):
    #   viewers_gain  viewers minus the streamer's previous sample (NULL on the first sample)
    #   offline_at    ts of the streamer's latest offline sample at or before this one (NULL if never offline),
    #                 so "live since" of an online sample is offline_at (or first_seen) without a window function
    "ALTER TABLE streamer_sample ADD COLUMN IF NOT EXISTS viewers_gain integer",
    "ALTER TABLE streamer_sample ADD COLUMN IF NOT EXISTS offline_at timestamptz",
]


def ensure_schema(conn: psycopg.Connection) -> None:
    """Apply SCHEMA_UPDATES; on a database that has samples but no derived facts yet, fill them once."""
    with conn.transaction(), conn.cursor() as cur:
        for stmt in SCHEMA_UPDATES:
            cur.execute(stmt)
        # rows with a predecessor (gap_s set) but no viewers_gain were written before that column existed
        cur.execute("SELECT (EXISTS (SELECT 1 FROM streamer_sample) AND NOT EXISTS "
                    "(SELECT 1 FROM streamer_sample WHERE gap_s IS NOT NULL)) "
                    "OR EXISTS (SELECT 1 FROM streamer_sample WHERE gap_s IS NOT NULL AND viewers_gain IS NULL)")
        if cur.fetchone()[0]:
            recompute(conn, None)


# --- derived facts ---------------------------------------------------------------------------------
# All three statements take the rows at or after %(start)s::timestamptz (every row when start is NULL) and, for the
# window functions, the streamer's last row before it, so an out-of-order insert only needs a recompute
# from its timestamp. Rows whose values do not change are not written.
# offline_at is a running maximum, so the seed row (the streamer's last row before start) contributes its
# stored offline_at; rows in scope contribute their own ts when offline. greatest() ignores NULLs.
GAIN_SQL = """
WITH scope AS (
  SELECT ts, twitch_id, donation_total, viewers, online, NULL::timestamptz AS seed_offline_at
  FROM streamer_sample WHERE %(start)s::timestamptz IS NULL OR ts >= %(start)s::timestamptz
  UNION ALL
  SELECT p.ts, p.twitch_id, p.donation_total, p.viewers, p.online, p.offline_at
  FROM (SELECT DISTINCT twitch_id FROM streamer_sample WHERE ts >= %(start)s::timestamptz) x
  CROSS JOIN LATERAL (SELECT ts, twitch_id, donation_total, viewers, online, offline_at FROM streamer_sample s
                      WHERE s.twitch_id = x.twitch_id AND s.ts < %(start)s::timestamptz ORDER BY ts DESC LIMIT 1) p
  WHERE %(start)s::timestamptz IS NOT NULL
), calc AS (
  SELECT ts, twitch_id,
         donation_total - lag(donation_total) OVER w AS gain,
         extract(epoch FROM ts - lag(ts) OVER w)::int AS gap_s,
         viewers - lag(viewers) OVER w AS viewers_gain,
         greatest(max(CASE WHEN NOT online THEN ts END) OVER w, max(seed_offline_at) OVER w) AS offline_at
  FROM scope WINDOW w AS (PARTITION BY twitch_id ORDER BY ts)
)
UPDATE streamer_sample s SET gain = c.gain, gap_s = c.gap_s, viewers_gain = c.viewers_gain, offline_at = c.offline_at
FROM calc c
WHERE s.twitch_id = c.twitch_id AND s.ts = c.ts AND (%(start)s::timestamptz IS NULL OR s.ts >= %(start)s::timestamptz)
  AND (s.gain IS DISTINCT FROM c.gain OR s.gap_s IS DISTINCT FROM c.gap_s
       OR s.viewers_gain IS DISTINCT FROM c.viewers_gain OR s.offline_at IS DISTINCT FROM c.offline_at)
"""

# From rebase_ts to end_ts (exclusive) MisterMV's API counter was Domingo's total plus 1/factor of his own
# donations (db/views.sql has the full story); at end_ts the API restored his real counter. dup is the
# part of the counter that was not his (counter minus real own), so that donation_total - dup is his real
# counter everywhere. Per sample:
#   rebase minute            -> the whole jump (his real counter did not move)
#   later, both gains known  -> factor * mirrored - (factor - 1) * counter gain, the mirrored gain being
#                               least(counter gain, source gain) floored at 0
#   later, a gain missing    -> 0 (carried forward)
#   end minute               -> dup NULL (the counter is real again) and dup_gain = the whole drop, so the
#                               correction counts as no gain in the views; later rows have both NULL
# dup is the running sum, seeded with the last dup before %(start)s::timestamptz. end_ts NULL: no end.
DUP_SQL = """
WITH cfg AS (
  SELECT %(own_id)s::text AS own_id, %(src_id)s::text AS src_id, %(rebase_ts)s::timestamptz AS rebase_ts,
         coalesce(%(end_ts)s::timestamptz, 'infinity'::timestamptz) AS end_ts, coalesce(%(factor)s::numeric, 1) AS factor
), rows AS (
  SELECT s.ts, s.gain AS own_delta, d.gain AS src_delta, cfg.rebase_ts, cfg.end_ts, cfg.factor
  FROM streamer_sample s CROSS JOIN cfg
  LEFT JOIN streamer_sample d ON d.twitch_id = cfg.src_id AND d.ts = s.ts
  WHERE s.twitch_id = cfg.own_id AND s.ts >= cfg.rebase_ts AND s.ts <= cfg.end_ts
    AND (%(start)s::timestamptz IS NULL OR s.ts >= %(start)s::timestamptz)
), calc AS (
  SELECT ts, end_ts,
         CASE WHEN ts = rebase_ts OR ts = end_ts THEN coalesce(own_delta, 0)
              WHEN own_delta IS NULL OR src_delta IS NULL THEN 0
              ELSE factor * greatest(least(own_delta, src_delta), 0) - (factor - 1) * own_delta END AS dup_gain
  FROM rows
), cum AS (
  SELECT ts, dup_gain,
         CASE WHEN ts = end_ts THEN NULL ELSE
         coalesce((SELECT dup FROM streamer_sample p, cfg WHERE p.twitch_id = cfg.own_id AND p.ts >= cfg.rebase_ts
                   AND %(start)s::timestamptz IS NOT NULL AND p.ts < %(start)s::timestamptz ORDER BY p.ts DESC LIMIT 1), 0)
         + sum(dup_gain) OVER (ORDER BY ts) END AS dup
  FROM calc
)
UPDATE streamer_sample s SET dup = c.dup, dup_gain = c.dup_gain
FROM cum c, cfg
WHERE s.twitch_id = cfg.own_id AND s.ts = c.ts
  AND (s.dup IS DISTINCT FROM c.dup OR s.dup_gain IS DISTINCT FROM c.dup_gain)
"""

# Rows after the end of the mirror carry no dup (clears values written before end_ts was known).
DUP_END_SQL = """
UPDATE streamer_sample SET dup = NULL, dup_gain = NULL
WHERE twitch_id = %(own_id)s::text AND %(end_ts)s::timestamptz IS NOT NULL AND ts > %(end_ts)s::timestamptz
  AND (dup IS NOT NULL OR dup_gain IS NOT NULL)
"""

RANK_SQL = """
WITH calc AS (
  SELECT ts, twitch_id, rank() OVER (PARTITION BY ts ORDER BY donation_total - coalesce(dup, 0) DESC) AS rank
  FROM streamer_sample WHERE %(start)s::timestamptz IS NULL OR ts >= %(start)s::timestamptz
)
UPDATE streamer_sample s SET rank = c.rank
FROM calc c WHERE s.twitch_id = c.twitch_id AND s.ts = c.ts AND s.rank IS DISTINCT FROM c.rank
"""

MIRROR_CONFIG_SQL = """
SELECT (SELECT twitch_id FROM streamer WHERE login = c.login),
       (SELECT twitch_id FROM streamer WHERE login = c.source_login), c.rebase_ts, c.end_ts, c.factor
FROM mirror_config c
"""


def recompute(conn: psycopg.Connection, start, mirror: tuple | None = None) -> None:
    """Fill gain/gap_s/viewers_gain/offline_at, dup/dup_gain and rank for the samples at or after `start` (all when None).

    `mirror` is (own twitch_id, source twitch_id, rebase_ts, end_ts or None, factor or None); by default it is
    read from the mirror_config view of db/views.sql (skipped when the view or the streamers do not exist yet).
    """
    with conn.cursor() as cur:
        cur.execute(GAIN_SQL, {"start": start})
        if mirror is None:
            cur.execute("SELECT to_regclass('mirror_config') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute(MIRROR_CONFIG_SQL)
                row = cur.fetchone()
                mirror = row if row and row[0] and row[1] else None
        if mirror:
            own_id, src_id, rebase_ts, end_ts, factor = (tuple(mirror) + (None, None))[:5]
            args = {"start": start, "own_id": own_id, "src_id": src_id, "rebase_ts": rebase_ts, "end_ts": end_ts, "factor": factor}
            cur.execute(DUP_SQL, args)
            cur.execute(DUP_END_SQL, args)
        cur.execute(RANK_SQL, {"start": start})


def insert(conn: psycopg.Connection, parsed: Parsed, derive: bool = True) -> None:
    """Write one tick (snapshot + streamer upserts + samples) in a single transaction, then the derived
    facts of that tick (derive=False lets a bulk import recompute once at the end instead)."""
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
        if derive:
            recompute(conn, s.ts)
