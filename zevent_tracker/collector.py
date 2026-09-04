from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from .api import fetch
from .backfill import backfill
from .db import insert
from .parse import parse

log = logging.getLogger("zevent")


@dataclass(frozen=True)
class Config:
    api_url: str
    database_url: str
    poll_interval: int
    raw_dir: Path
    backfill_on_start: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_url=os.environ.get("ZEVENT_API_URL", "https://zevent.fr/api/"),
            database_url=os.environ["DATABASE_URL"],
            poll_interval=int(os.environ.get("POLL_INTERVAL", "60")),
            raw_dir=Path(os.environ.get("RAW_DIR", "./raw")),
            backfill_on_start=os.environ.get("BACKFILL_ON_START", "true").lower() in ("1", "true", "yes"),
        )


def write_raw(raw_dir: Path, ts: datetime, body: bytes) -> Path:
    d = raw_dir / ts.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    p = d / ts.strftime("%H%M%S.json")
    p.write_bytes(body)
    return p


def tick(cfg: Config) -> None:
    """One poll. Never raises: a failed tick is logged and skipped."""
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
            ts.isoformat(),
            s.donation_total,
            s.viewers_total,
            s.streamers_total,
            s.streamers_online,
            (time.monotonic() - t0) * 1000,
        )
    except Exception:
        log.exception("tick %s failed", ts.isoformat())


def run(cfg: Config) -> None:
    log.info("collector start url=%s interval=%ss raw=%s", cfg.api_url, cfg.poll_interval, cfg.raw_dir)
    tick(cfg)
    if cfg.backfill_on_start:
        # import raw dumps missing from the db (e.g. after a restore or a db outage) without blocking polling
        threading.Thread(target=_safe_backfill, args=(cfg,), name="backfill", daemon=True).start()
    while True:
        # sleep until the next wall-clock boundary (e.g. :00 seconds)
        time.sleep(cfg.poll_interval - (time.time() % cfg.poll_interval))
        tick(cfg)


def _safe_backfill(cfg: Config) -> None:
    try:
        backfill(cfg.raw_dir, cfg.database_url)
    except Exception:
        log.exception("backfill failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.from_env()
    if sys.argv[1:] == ["backfill"]:
        backfill(cfg.raw_dir, cfg.database_url)
        return
    run(cfg)
