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
