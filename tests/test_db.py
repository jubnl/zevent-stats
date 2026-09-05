import os
from datetime import datetime, timezone

import psycopg
import pytest

from zevent_tracker.db import ensure_schema, insert
from zevent_tracker.parse import Parsed, Snapshot, StreamerSample

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def make(ts):
    s = StreamerSample(ts, "1", "foo", "Foo", None, None, True, "ZEVENT", 10, 5.5, location="LAN")
    return Parsed(Snapshot(ts, 100.0, 10, 1, 1), [s])


def test_insert_twice_upserts_streamer():
    t1 = datetime(2030, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2030, 1, 1, 0, 1, tzinfo=timezone.utc)
    with psycopg.connect(URL) as conn:
        insert(conn, make(t1))
        insert(conn, make(t2))
        n = conn.execute("select count(*) from streamer_sample where twitch_id='1'").fetchone()[0]
        fs, ls, loc = conn.execute("select first_seen, last_seen, location from streamer where twitch_id='1'").fetchone()
        conn.execute("delete from streamer_sample where twitch_id='1'")
        conn.execute("delete from streamer where twitch_id='1'")
        conn.execute("delete from snapshot where ts in (%s,%s)", (t1, t2))
    assert n == 2
    assert fs == t1 and ls == t2
    assert loc == "LAN"


def test_insert_out_of_order_keeps_extremes_and_newest_fields():
    """A backfill of older dumps after a live tick must not move first_seen forward or revert names."""
    t1 = datetime(2030, 1, 2, tzinfo=timezone.utc)
    t2 = datetime(2030, 1, 2, 0, 1, tzinfo=timezone.utc)
    old = Parsed(Snapshot(t1, 100.0, 10, 1, 1), [StreamerSample(t1, "2", "old", "Old", None, None, True, "ZEVENT", 1, 1.0)])
    new = Parsed(Snapshot(t2, 100.0, 10, 1, 1), [StreamerSample(t2, "2", "new", "New", None, None, True, "ZEVENT", 1, 2.0)])
    with psycopg.connect(URL) as conn:
        insert(conn, new)
        insert(conn, old)  # arrives later, but is older
        display, fs, ls = conn.execute("select display, first_seen, last_seen from streamer where twitch_id='2'").fetchone()
        conn.execute("delete from streamer_sample where twitch_id='2'")
        conn.execute("delete from streamer where twitch_id='2'")
        conn.execute("delete from snapshot where ts in (%s,%s)", (t1, t2))
    assert display == "New"
    assert fs == t1 and ls == t2


def test_ensure_schema_is_idempotent():
    with psycopg.connect(URL) as conn:
        ensure_schema(conn)
        ensure_schema(conn)
        cols = {r[0] for r in conn.execute(
            "select column_name from information_schema.columns where table_name = 'streamer'")}
    assert "location" in cols
