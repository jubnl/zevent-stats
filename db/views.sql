-- Derived views used by the Grafana dashboards. Runs automatically on a fresh database (mounted into
-- /docker-entrypoint-initdb.d, after init.sql). On an existing database apply it by hand, once per change,
-- piping the file in from the host:
--   docker compose exec -T db psql -U zevent -d zevent < db/views.sql
-- Do not use the copy mounted inside the container (-f /docker-entrypoint-initdb.d/views.sql): git replaces
-- the file on pull, and a running container keeps the old inode, so that copy is stale until the container
-- is recreated.
--
-- Background: mistermv's API counter merges his own donations with a second source. In the data that
-- second source is a copy of Domingo's counter: at 01:08 UTC on 2026-09-05 mistermv's counter jumped
-- by 261K (Domingo's total as of ~00:47 UTC) while the global total moved 1.7K, and since then every
-- donation to Domingo is credited to mistermv as well. The global total counts those once.
-- These views split mistermv into two rows, "mistermv" (his own donations) and a derived
-- "mistermv (private counter)" (the mirrored part), so leaderboards show both and the derived row can
-- be excluded from sums that are compared to the global total.
--
-- Split rule per sample minute, from the rebase minute on:
--   rebase minute      -> the whole jump is mirrored
--   any later minute   -> least(mistermv increment, Domingo increment), floored at 0; the excess is his own
--
-- The global total is NOT corrected. "External donations" (global minus the real streamer counters) is
-- money donated on the Streamlabs Charity team page without a member id, which lands in the team total
-- and on no streamer. zevent's global total tracks the Streamlabs team total with a ~1 minute lag
-- (sampled 2026-09-05 16:13-16:17 UTC: Streamlabs was 1.4-6.9K ahead, never behind). An earlier
-- version of this file subtracted mistermv's non-mirrored increments from the global total on the
-- theory that it counted them twice; the Streamlabs comparison showed that was wrong, so it was removed.

-- The views are recreated inside one transaction so the swap is atomic for the dashboards.
BEGIN;
DROP VIEW IF EXISTS snapshot_v;        -- removed 2026-09-05, see above
DROP VIEW IF EXISTS streamer_sample_v;
DROP VIEW IF EXISTS streamer_v;
DROP VIEW IF EXISTS mirror_dup;
DROP VIEW IF EXISTS mirror_config;

CREATE VIEW mirror_config AS
SELECT 'mistermv'::text                         AS login,
       'domingo'::text                          AS source_login,
       '2026-09-05 01:08:00+00'::timestamptz    AS rebase_ts,     -- first sample with the mirrored counter
       'mistermv-private'::text                 AS derived_id,    -- twitch_id of the derived row
       'mistermv (private counter)'::text       AS derived_display;

-- Per sample ts from the rebase on: the mirrored increment and the cumulative mirrored amount.
CREATE VIEW mirror_dup AS
WITH c AS (SELECT * FROM mirror_config),
d AS (
  SELECT s.ts, st.login, s.donation_total - lag(s.donation_total) OVER (PARTITION BY s.twitch_id ORDER BY s.ts) AS delta
  FROM streamer_sample s JOIN streamer st USING (twitch_id), c
  WHERE st.login IN (c.login, c.source_login)
),
m AS (
  SELECT d.ts,
         max(d.delta) FILTER (WHERE d.login = c.login)        AS own_delta,
         max(d.delta) FILTER (WHERE d.login = c.source_login) AS src_delta
  FROM d, c WHERE d.ts >= c.rebase_ts GROUP BY d.ts
),
dd AS (
  SELECT m.ts, CASE WHEN m.ts = c.rebase_ts THEN m.own_delta ELSE greatest(least(m.own_delta, m.src_delta), 0) END AS dup_delta
  FROM m, c WHERE m.own_delta IS NOT NULL AND m.src_delta IS NOT NULL
)
SELECT ts, dup_delta, sum(dup_delta) OVER (ORDER BY ts) AS dup FROM dd;

-- streamer plus the derived row. `derived` marks rows that are not API entities.
-- `location` (added 2026-09-05, needs the collector to have run once) is 'LAN' or 'Online'.
CREATE VIEW streamer_v AS
SELECT twitch_id, login, display, profile_url, donation_url, location, first_seen, last_seen, false AS derived
FROM streamer
UNION ALL
SELECT c.derived_id, st.login, c.derived_display, st.profile_url, st.donation_url, st.location, c.rebase_ts, st.last_seen, true
FROM streamer st JOIN mirror_config c ON st.login = c.login;

-- streamer_sample with mistermv's counter split: his row minus the mirrored amount, plus a derived row
-- carrying the mirrored amount (offline, no viewers, no game).
CREATE VIEW streamer_sample_v AS
WITH mirrored AS (
  SELECT d.ts, st.twitch_id, d.dup FROM mirror_dup d, streamer st JOIN mirror_config c ON st.login = c.login
)
SELECT s.ts, s.twitch_id, s.online, s.game, s.viewers, s.donation_total - coalesce(mr.dup, 0) AS donation_total, false AS derived
FROM streamer_sample s LEFT JOIN mirrored mr ON mr.ts = s.ts AND mr.twitch_id = s.twitch_id
UNION ALL
SELECT d.ts, c.derived_id, false, NULL, 0, d.dup, true
FROM mirror_dup d, mirror_config c;

COMMIT;
