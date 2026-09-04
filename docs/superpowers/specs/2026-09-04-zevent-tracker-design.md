# ZEVENT tracker — design

Date: 2026-09-04. Event ends Monday 2026-09-07 around 02:00–03:00 local time.

## Goal

Poll `https://zevent.fr/api/` once per minute, store every value it returns
in PostgreSQL, and show the history in Grafana: global totals over time,
per-streamer curves, and leaderboards. Data collection must start as early
as possible; missed minutes cannot be recovered.

## Non-goals

- Backfilling data from before the collector starts.
- Tracking `calendar` and `marquee` (empty / null in the sample). The raw
  JSON dump keeps them if they ever appear.
- Alerting, auth hardening, HA. This is a local, single-machine stack.

## Architecture

`compose.yaml` with three services:

| service   | image                 | role                                   |
|-----------|-----------------------|----------------------------------------|
| db        | postgres:16-alpine    | storage; schema from `db/init.sql`     |
| collector | built from Dockerfile | poll API, dump raw JSON, insert rows   |
| grafana   | grafana/grafana-oss   | dashboards, provisioned from files     |

Volumes: `pgdata` (named), `grafana-data` (named), `./raw` (bind mount for
raw JSON dumps). `collector` and `grafana` depend on `db` healthcheck.
All services use `restart: unless-stopped`.

## Collector

Python 3.11+, dependencies: `httpx`, `psycopg[binary]`.

Package `zevent_tracker/`:

- `api.py` — `fetch(url, timeout) -> bytes`. Single GET, raises on non-2xx.
- `parse.py` — `parse(payload: dict, ts: datetime) -> Parsed` where `Parsed`
  holds one `Snapshot` row and a list of `StreamerSample` rows (dataclasses).
  Pure function, no I/O. Amounts read from `*.number`, formatted strings
  ignored.
- `db.py` — `insert(conn, parsed)`: one transaction that inserts the
  snapshot, upserts streamers, and bulk-inserts samples with
  `executemany`.
- `collector.py` — main loop. Sleeps until the next wall-clock minute
  boundary, then runs one tick.

One tick:

1. `ts = now(UTC)` truncated to seconds.
2. Fetch (20 s timeout).
3. Write raw body to `RAW_DIR/YYYY-MM-DD/HHMMSS.json` (UTC).
4. Parse, then insert in one transaction using a fresh connection.
5. Log one line: ts, total, viewers, streamer count, elapsed ms.

Any exception in a tick is logged with traceback and the tick is skipped.
The loop never exits on error. A fresh DB connection per tick means a DB
restart heals on the next minute.

Configuration via environment variables:

| var              | default                    |
|------------------|----------------------------|
| ZEVENT_API_URL   | https://zevent.fr/api/     |
| DATABASE_URL     | (required)                 |
| POLL_INTERVAL    | 60 (seconds)               |
| RAW_DIR          | ./raw                      |

## Schema (`db/init.sql`)

```sql
CREATE TABLE snapshot (
  ts               timestamptz PRIMARY KEY,
  donation_total   numeric(14,2) NOT NULL,
  viewers_total    integer NOT NULL,
  streamers_total  integer NOT NULL,
  streamers_online integer NOT NULL
);

CREATE TABLE streamer (
  twitch_id    text PRIMARY KEY,
  login        text NOT NULL,
  display      text NOT NULL,
  profile_url  text,
  donation_url text,
  first_seen   timestamptz NOT NULL,
  last_seen    timestamptz NOT NULL
);

CREATE TABLE streamer_sample (
  ts             timestamptz NOT NULL,
  twitch_id      text NOT NULL REFERENCES streamer(twitch_id),
  online         boolean NOT NULL,
  game           text,
  viewers        integer NOT NULL,
  donation_total numeric(12,2) NOT NULL,
  PRIMARY KEY (twitch_id, ts)
);
CREATE INDEX streamer_sample_ts_idx ON streamer_sample (ts);

CREATE ROLE grafana LOGIN PASSWORD '...';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana;
```

Streamer upsert updates `login`, `display`, `profile_url`, `donation_url`,
`last_seen`. Derived values (rates, deltas, rankings) are computed in SQL
at query time, never stored.

## Grafana

Provisioned from `grafana/provisioning/`:

- `datasources/postgres.yaml` — Postgres datasource `ZEVENT`, user
  `grafana`, read-only.
- `dashboards/dashboards.yaml` + `dashboards/zevent.json` — one dashboard:
  - Stat row: current donation total, viewers, streamers online,
    donations in the last hour.
  - Time series: global donation total; global viewers; donation rate per
    `$__timeGroup` bucket (max − min per bucket).
  - Variable `streamer` (multi-select, from `streamer` table). Panels:
    per-streamer donation total, per-streamer viewers, state timeline of
    online / game.
  - Tables: top 20 by donation total (latest sample), top 20 by viewers
    (latest sample), top 20 by donations gained in the selected range.

Anonymous viewer access on, admin password from `.env`.

## Error handling summary

- API down / slow / malformed → tick skipped, logged, next minute retried.
- DB down → same. Raw file is still written before the DB step, so the
  minute can be replayed later from disk.
- Collector crash → Docker restarts it.

## Testing

- `tests/test_parse.py`: feed `data-shape.json` to `parse()`, assert the
  snapshot row values and one streamer sample (Aducine: 55 viewers,
  815.49 €), and that the number of samples equals `len(live)`.
- Integration: `docker compose up -d`, wait two minutes, `SELECT count(*)`
  from both tables shows growth. Then open Grafana and eyeball panels.

## Delivery order

1. Schema, collector, compose with `db` + `collector`. Start it. Data flows.
2. Grafana service, datasource, dashboard. Iterate on panels while data
   accumulates.
