# ZEVENT Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll the ZEVENT API every minute into PostgreSQL and show history in Grafana.

**Architecture:** A Python collector loop (fetch → raw dump → parse → insert) writes into three Postgres tables. Grafana is provisioned from files against a read-only role. Everything runs in one Docker Compose stack.

**Tech Stack:** Python 3.11, httpx, psycopg 3, pytest, PostgreSQL 16, Grafana OSS, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-04-zevent-tracker-design.md`

## Global Constraints

- Python >= 3.11, managed with `uv`.
- No git commits are made by the agent (user rule). Skip every commit step.
- Collector never exits on a tick error; it logs and continues.
- All timestamps are UTC `timestamptz`.
- Derived values (rates, deltas, ranks) are computed in SQL at query time, never stored.

---

### Task 1: Parse the API payload (pure function)

**Files:**
- Create: `zevent_tracker/__init__.py` (empty)
- Create: `zevent_tracker/parse.py`
- Create: `tests/test_parse.py`
- Modify: `pyproject.toml` (add deps and pytest config)

**Interfaces:**
- Produces: `Snapshot`, `StreamerSample`, `Parsed` dataclasses and `parse(payload: dict, ts: datetime) -> Parsed`.

- [ ] **Step 1: Add dependencies**

```toml
[project]
name = "zevent-tracker"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27", "psycopg[binary]>=3.2"]

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Run: `uv sync`

- [ ] **Step 2: Write the failing test**

```python
import json
from datetime import datetime, timezone
from pathlib import Path

from zevent_tracker.parse import parse

TS = datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc)


def load():
    return json.loads(Path("data-shape.json").read_text())


def test_snapshot_row():
    p = parse(load(), TS)
    assert p.snapshot.ts == TS
    assert p.snapshot.donation_total == 2685787.68
    assert p.snapshot.viewers_total == 667017
    assert p.snapshot.streamers_total == 337
    assert p.snapshot.streamers_online == sum(1 for s in load()["live"] if s["online"])


def test_streamer_sample():
    p = parse(load(), TS)
    assert len(p.samples) == 337
    a = next(s for s in p.samples if s.twitch_id == "44842076")
    assert a.login == "aducine"
    assert a.display == "Aducine"
    assert a.online is True
    assert a.game == "ZEVENT"
    assert a.viewers == 55
    assert a.donation_total == 815.49
    assert a.donation_url == "https://zevent.fr/don/aducine"
    assert a.profile_url.startswith("https://")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest -v`
Expected: FAIL with `ModuleNotFoundError: zevent_tracker.parse`

- [ ] **Step 4: Implement**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Snapshot:
    ts: datetime
    donation_total: float
    viewers_total: int
    streamers_total: int
    streamers_online: int


@dataclass(frozen=True)
class StreamerSample:
    ts: datetime
    twitch_id: str
    login: str
    display: str
    profile_url: str | None
    donation_url: str | None
    online: bool
    game: str | None
    viewers: int
    donation_total: float


@dataclass(frozen=True)
class Parsed:
    snapshot: Snapshot
    samples: list[StreamerSample]


def _num(obj, key, default=0):
    v = obj.get(key)
    if isinstance(v, dict):
        v = v.get("number")
    return default if v is None else v


def parse(payload: dict, ts: datetime) -> Parsed:
    live = payload.get("live") or []
    samples = [
        StreamerSample(
            ts=ts,
            twitch_id=str(s["twitch_id"]),
            login=s.get("twitch") or "",
            display=s.get("display") or "",
            profile_url=s.get("profileUrl"),
            donation_url=s.get("donationUrl"),
            online=bool(s.get("online")),
            game=s.get("game"),
            viewers=int(_num(s, "viewersAmount")),
            donation_total=float(_num(s, "donationAmount")),
        )
        for s in live
    ]
    snapshot = Snapshot(
        ts=ts,
        donation_total=float(_num(payload, "donationAmount")),
        viewers_total=int(_num(payload, "viewersCount")),
        streamers_total=len(samples),
        streamers_online=sum(1 for s in samples if s.online),
    )
    return Parsed(snapshot=snapshot, samples=samples)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: 2 passed

---

### Task 2: Database schema and insert

**Files:**
- Create: `db/init.sql`
- Create: `zevent_tracker/db.py`
- Create: `tests/test_db.py` (integration, skipped without `TEST_DATABASE_URL`)

**Interfaces:**
- Consumes: `Parsed` from Task 1.
- Produces: `insert(conn: psycopg.Connection, parsed: Parsed) -> None`.

- [ ] **Step 1: Write the schema**

`db/init.sql` (runs once on first Postgres start via `/docker-entrypoint-initdb.d`):

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

CREATE ROLE grafana LOGIN PASSWORD 'grafana';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana;
```

- [ ] **Step 2: Write the failing integration test**

```python
import os
from datetime import datetime, timezone

import psycopg
import pytest

from zevent_tracker.db import insert
from zevent_tracker.parse import Parsed, Snapshot, StreamerSample

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def make(ts):
    s = StreamerSample(ts, "1", "foo", "Foo", None, None, True, "ZEVENT", 10, 5.5)
    return Parsed(Snapshot(ts, 100.0, 10, 1, 1), [s])


def test_insert_twice_upserts_streamer():
    t1 = datetime(2030, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2030, 1, 1, 0, 1, tzinfo=timezone.utc)
    with psycopg.connect(URL) as conn:
        insert(conn, make(t1))
        insert(conn, make(t2))
        n = conn.execute("select count(*) from streamer_sample where twitch_id='1'").fetchone()[0]
        fs, ls = conn.execute("select first_seen, last_seen from streamer where twitch_id='1'").fetchone()
        conn.execute("delete from streamer_sample where twitch_id='1'")
        conn.execute("delete from streamer where twitch_id='1'")
        conn.execute("delete from snapshot where ts in (%s,%s)", (t1, t2))
    assert n == 2
    assert fs == t1 and ls == t2
```

- [ ] **Step 3: Implement**

```python
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
    s = parsed.snapshot
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(SNAPSHOT_SQL, (s.ts, s.donation_total, s.viewers_total, s.streamers_total, s.streamers_online))
        cur.executemany(
            STREAMER_SQL,
            [(x.twitch_id, x.login, x.display, x.profile_url, x.donation_url, x.ts, x.ts) for x in parsed.samples],
        )
        cur.executemany(
            SAMPLE_SQL,
            [(x.ts, x.twitch_id, x.online, x.game, x.viewers, x.donation_total) for x in parsed.samples],
        )
```

- [ ] **Step 4: Run against a throwaway Postgres**

```bash
docker run -d --name zevent-test -e POSTGRES_PASSWORD=pw -p 55432:5432 -v $PWD/db/init.sql:/docker-entrypoint-initdb.d/init.sql postgres:16-alpine
sleep 5
TEST_DATABASE_URL=postgresql://postgres:pw@localhost:55432/postgres uv run pytest -v
docker rm -f zevent-test
```
Expected: 3 passed

---

### Task 3: Fetch, raw dump, and the collector loop

**Files:**
- Create: `zevent_tracker/api.py`
- Create: `zevent_tracker/collector.py`
- Create: `tests/test_collector.py`
- Modify: `main.py` (entry point)

**Interfaces:**
- Consumes: `parse`, `insert`.
- Produces: `fetch(url, timeout) -> bytes`, `write_raw(raw_dir, ts, body) -> Path`, `tick(cfg) -> None`, `run(cfg) -> None`, `Config`.

- [ ] **Step 1: Write failing tests for raw dump and tick error isolation**

```python
from datetime import datetime, timezone

from zevent_tracker.collector import Config, tick, write_raw


def test_write_raw_layout(tmp_path):
    ts = datetime(2026, 9, 4, 20, 5, 7, tzinfo=timezone.utc)
    p = write_raw(tmp_path, ts, b"{}")
    assert p == tmp_path / "2026-09-04" / "200507.json"
    assert p.read_bytes() == b"{}"


def test_tick_swallows_errors(tmp_path, monkeypatch):
    def boom(url, timeout):
        raise RuntimeError("down")
    monkeypatch.setattr("zevent_tracker.collector.fetch", boom)
    cfg = Config(api_url="x", database_url="x", poll_interval=60, raw_dir=tmp_path)
    tick(cfg)  # must not raise
```

- [ ] **Step 2: Implement api.py**

```python
import httpx


def fetch(url: str, timeout: float = 20.0) -> bytes:
    r = httpx.get(url, timeout=timeout, headers={"User-Agent": "zevent-tracker/0.1"})
    r.raise_for_status()
    return r.content
```

- [ ] **Step 3: Implement collector.py**

```python
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from .api import fetch
from .db import insert
from .parse import parse

log = logging.getLogger("zevent")


@dataclass(frozen=True)
class Config:
    api_url: str
    database_url: str
    poll_interval: int
    raw_dir: Path

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_url=os.environ.get("ZEVENT_API_URL", "https://zevent.fr/api/"),
            database_url=os.environ["DATABASE_URL"],
            poll_interval=int(os.environ.get("POLL_INTERVAL", "60")),
            raw_dir=Path(os.environ.get("RAW_DIR", "./raw")),
        )


def write_raw(raw_dir: Path, ts: datetime, body: bytes) -> Path:
    d = raw_dir / ts.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    p = d / ts.strftime("%H%M%S.json")
    p.write_bytes(body)
    return p


def tick(cfg: Config) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    t0 = time.monotonic()
    try:
        body = fetch(cfg.api_url)
        write_raw(cfg.raw_dir, ts, body)
        parsed = parse(json.loads(body), ts)
        with psycopg.connect(cfg.database_url) as conn:
            insert(conn, parsed)
        s = parsed.snapshot
        log.info(
            "%s total=%.2f viewers=%d streamers=%d online=%d %dms",
            ts.isoformat(), s.donation_total, s.viewers_total,
            s.streamers_total, s.streamers_online, (time.monotonic() - t0) * 1000,
        )
    except Exception:
        log.exception("tick %s failed", ts.isoformat())


def run(cfg: Config) -> None:
    log.info("collector start url=%s interval=%ss raw=%s", cfg.api_url, cfg.poll_interval, cfg.raw_dir)
    tick(cfg)
    while True:
        time.sleep(cfg.poll_interval - (time.time() % cfg.poll_interval))
        tick(cfg)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(Config.from_env())
```

- [ ] **Step 4: Entry point**

`main.py`:
```python
from zevent_tracker.collector import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest -v`
Expected: all pass

---

### Task 4: Compose stack (db + collector) and go live

**Files:**
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.env`, `.env.example`, `.gitignore`, `.dockerignore`

- [ ] **Step 1: Dockerfile**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY zevent_tracker ./zevent_tracker
COPY main.py ./
ENV PYTHONUNBUFFERED=1
CMD ["/app/.venv/bin/python", "main.py"]
```

- [ ] **Step 2: compose.yaml**

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: zevent
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: zevent
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    ports:
      - "127.0.0.1:5433:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U zevent -d zevent"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  collector:
    build: .
    environment:
      DATABASE_URL: postgresql://zevent:${POSTGRES_PASSWORD}@db:5432/zevent
      ZEVENT_API_URL: https://zevent.fr/api/
      POLL_INTERVAL: "60"
      RAW_DIR: /raw
    volumes:
      - ./raw:/raw
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

volumes:
  pgdata:
```

- [ ] **Step 3: env and ignore files**

`.env.example`:
```
POSTGRES_PASSWORD=change-me
GF_ADMIN_PASSWORD=change-me
```
`.gitignore`: `.env`, `raw/`, `.venv/`, `__pycache__/`, `.idea/`
`.dockerignore`: `.venv`, `raw`, `.git`, `.idea`, `tests`, `docs`

- [ ] **Step 4: Start and verify**

```bash
docker compose up -d --build
sleep 70
docker compose logs collector --tail 5
docker compose exec db psql -U zevent -c "select count(*) from snapshot; select count(*) from streamer_sample;"
ls raw/*/
```
Expected: log lines like `total=... viewers=...`, counts >= 1 and >= 300, one JSON file per minute.

---

### Task 5: Grafana service, datasource, dashboard

**Files:**
- Modify: `compose.yaml` (add grafana service and volume)
- Create: `grafana/provisioning/datasources/postgres.yaml`
- Create: `grafana/provisioning/dashboards/dashboards.yaml`
- Create: `grafana/provisioning/dashboards/zevent.json`

- [ ] **Step 1: Add grafana to compose**

```yaml
  grafana:
    image: grafana/grafana-oss:11.3.0
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GF_ADMIN_PASSWORD}
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
    volumes:
      - grafana-data:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
    ports:
      - "127.0.0.1:3000:3000"
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
```
plus `grafana-data:` under `volumes:`.

- [ ] **Step 2: Datasource**

```yaml
apiVersion: 1
datasources:
  - name: ZEVENT
    uid: zevent-pg
    type: postgres
    url: db:5432
    user: grafana
    secureJsonData:
      password: grafana
    jsonData:
      database: zevent
      sslmode: disable
      postgresVersion: 1600
      timescaledb: false
    isDefault: true
```

- [ ] **Step 3: Dashboard provider**

```yaml
apiVersion: 1
providers:
  - name: zevent
    folder: ""
    type: file
    options:
      path: /etc/grafana/provisioning/dashboards
```

- [ ] **Step 4: Dashboard JSON** — panels and their SQL (the JSON file wraps these):

Stat: current total
```sql
SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1
```
Stat: viewers now
```sql
SELECT viewers_total FROM snapshot ORDER BY ts DESC LIMIT 1
```
Stat: streamers online
```sql
SELECT streamers_online FROM snapshot ORDER BY ts DESC LIMIT 1
```
Stat: donations last hour
```sql
SELECT max(donation_total) - min(donation_total) FROM snapshot WHERE ts > now() - interval '1 hour'
```
Time series: global total
```sql
SELECT ts AS time, donation_total AS "Total €" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1
```
Time series: global viewers
```sql
SELECT ts AS time, viewers_total AS "Viewers" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1
```
Bars: donation rate per bucket
```sql
SELECT $__timeGroupAlias(ts, $__interval), max(donation_total) - min(donation_total) AS "€ / bucket"
FROM snapshot WHERE $__timeFilter(ts) GROUP BY 1 ORDER BY 1
```
Variable `streamer` (multi, query):
```sql
SELECT display AS __text, twitch_id AS __value FROM streamer ORDER BY display
```
Time series: per-streamer donations
```sql
SELECT s.ts AS time, st.display AS metric, s.donation_total AS value
FROM streamer_sample s JOIN streamer st USING (twitch_id)
WHERE $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) ORDER BY 1
```
Time series: per-streamer viewers
```sql
SELECT s.ts AS time, st.display AS metric, s.viewers AS value
FROM streamer_sample s JOIN streamer st USING (twitch_id)
WHERE $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) ORDER BY 1
```
State timeline: game
```sql
SELECT s.ts AS time, st.display AS metric, CASE WHEN s.online THEN s.game ELSE 'offline' END AS value
FROM streamer_sample s JOIN streamer st USING (twitch_id)
WHERE $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) ORDER BY 1
```
Table: top by donations (latest)
```sql
SELECT st.display AS "Streamer", s.donation_total AS "€", s.viewers AS "Viewers", s.online AS "Online"
FROM streamer_sample s JOIN streamer st USING (twitch_id)
WHERE s.ts = (SELECT max(ts) FROM snapshot) ORDER BY s.donation_total DESC LIMIT 20
```
Table: top by viewers (latest)
```sql
SELECT st.display AS "Streamer", s.viewers AS "Viewers", s.game AS "Game", s.donation_total AS "€"
FROM streamer_sample s JOIN streamer st USING (twitch_id)
WHERE s.ts = (SELECT max(ts) FROM snapshot) AND s.online ORDER BY s.viewers DESC LIMIT 20
```
Table: top gained in selected range
```sql
SELECT st.display AS "Streamer", max(s.donation_total) - min(s.donation_total) AS "Gained €"
FROM streamer_sample s JOIN streamer st USING (twitch_id)
WHERE $__timeFilter(s.ts) GROUP BY st.display ORDER BY 2 DESC LIMIT 20
```

- [ ] **Step 5: Restart and verify**

```bash
docker compose up -d
```
Open http://localhost:3000, dashboard "ZEVENT" loads with data. Check each panel renders without a query error.
