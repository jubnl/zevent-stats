-- Derived views used by the Grafana dashboards. Runs automatically on a fresh database (mounted into
-- /docker-entrypoint-initdb.d, after init.sql). On an existing database apply it by hand, once per change,
-- piping the file in from the host:
--   docker compose exec -T db psql -U zevent -d zevent < db/views.sql
-- Do not use the copy mounted inside the container (-f /docker-entrypoint-initdb.d/views.sql): git replaces
-- the file on pull, and a running container keeps the old inode, so that copy is stale until the container
-- is recreated. Apply it AFTER the collector has started at least once: the views read columns the
-- collector adds at startup (zevent_tracker/db.py SCHEMA_UPDATES).
--
-- Background: mistermv's API counter merges his own donations with a second source. In the data that
-- second source is a copy of Domingo's counter: at 01:08 UTC on 2026-09-05 mistermv's counter jumped
-- by 261K (Domingo's total as of ~00:47 UTC) while the global total moved 1.7K, and since then every
-- donation to Domingo is credited to mistermv as well. The global total counts those once.
-- These views split mistermv into two rows, "mistermv" (his own donations) and a derived
-- "mistermv (compteur privé)" (the mirrored part), so leaderboards show both and the derived row can
-- be excluded from sums that are compared to the global total.
--
-- Since 2026-09-06 the split is no longer computed here: the collector stores the mirrored amount on
-- mistermv's sample rows (columns dup and dup_gain, rule in zevent_tracker/db.py DUP_SQL, read from
-- mirror_config below), together with the per-row gain, gap and rank. The views only read columns, so
-- every dashboard query is a plain scan of the sample range.
--
-- The global total is NOT corrected. "External donations" (global minus the real streamer counters) is
-- money donated on the Streamlabs Charity team page without a member id, which lands in the team total
-- and on no streamer (plus the shop, e.g. 3.7M added at once on 2026-09-05 20:04 UTC). zevent's global
-- total tracks the Streamlabs team total with a ~1 minute lag.

-- The views are recreated inside one transaction so the swap is atomic for the dashboards.
BEGIN;
DROP VIEW IF EXISTS snapshot_v;        -- removed 2026-09-05
DROP VIEW IF EXISTS streamer_sample_v;
DROP VIEW IF EXISTS streamer_v;
DROP VIEW IF EXISTS mirror_dup;        -- removed 2026-09-06, replaced by the dup columns
DROP VIEW IF EXISTS mirror_config;

CREATE VIEW mirror_config AS
SELECT 'mistermv'::text                         AS login,
       'domingo'::text                          AS source_login,
       '2026-09-05 01:08:00+00'::timestamptz    AS rebase_ts,     -- first sample with the mirrored counter
       'mistermv-private'::text                 AS derived_id,    -- twitch_id of the derived row
       'Cagnotte spéciale du Vieux Monsieur'::text        AS derived_display;

-- streamer plus the derived row. `derived` marks rows that are not API entities.
-- `location` is 'LAN' (on site) or 'Online' (from home).
CREATE VIEW streamer_v AS
SELECT twitch_id, login, display, profile_url, donation_url, location, first_seen, last_seen, false AS derived
FROM streamer
UNION ALL
SELECT c.derived_id, st.login, c.derived_display, st.profile_url, st.donation_url, st.location, c.rebase_ts, st.last_seen, true
FROM streamer st JOIN mirror_config c ON st.login = c.login;

-- streamer_sample with mistermv's counter split: his row minus the mirrored amount, plus a derived row
-- carrying the mirrored amount (offline, no viewers, no game, no rank). Columns:
--   gain    donation gain since the streamer's previous sample (NULL on the first one)
--   gap_s   seconds since that previous sample (NULL on the first one); cap it at 300 when summing time
--   rank    position in the donation leaderboard at that ts
--   viewers_gain  viewers gained since the streamer's previous sample
--   offline_at    latest offline sample at or before this one; an online sample has been live since
--                 coalesce(offline_at, first_seen)
-- The derived row's gain at the rebase minute is NULL: that jump is not money moving that minute.
CREATE VIEW streamer_sample_v AS
SELECT ts, twitch_id, online, game, viewers,
       donation_total - coalesce(dup, 0) AS donation_total,
       gain - coalesce(dup_gain, 0)      AS gain,
       gap_s, rank, viewers_gain, offline_at, false AS derived
FROM streamer_sample
UNION ALL
SELECT s.ts, c.derived_id, false, NULL, 0, s.dup,
       CASE WHEN s.ts = c.rebase_ts THEN NULL ELSE s.dup_gain END,
       s.gap_s, NULL, NULL, NULL, true
FROM streamer_sample s JOIN streamer st USING (twitch_id) JOIN mirror_config c ON st.login = c.login
WHERE s.dup IS NOT NULL;

COMMIT;
