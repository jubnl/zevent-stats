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


def test_recompute_fills_gain_gap_rank_and_out_of_order_predecessor():
    t1 = datetime(2030, 1, 3, tzinfo=timezone.utc)
    t2 = datetime(2030, 1, 3, 0, 1, tzinfo=timezone.utc)
    t3 = datetime(2030, 1, 3, 0, 3, tzinfo=timezone.utc)  # 2-minute gap
    def tick(t, a, b):
        return Parsed(Snapshot(t, 100.0, 10, 2, 2), [
            StreamerSample(t, "r1", "r1", "R1", None, None, True, "ZEVENT", 5, a),
            StreamerSample(t, "r2", "r2", "R2", None, None, True, "ZEVENT", 5, b)])
    with psycopg.connect(URL) as conn:
        try:
            insert(conn, tick(t1, 10.0, 20.0))
            insert(conn, tick(t3, 30.0, 25.0))
            rows = conn.execute("select twitch_id, ts, gain, gap_s, rank from streamer_sample where twitch_id in ('r1','r2') order by ts, twitch_id").fetchall()
            assert [(r[0], r[2], r[3], r[4]) for r in rows] == [
                ("r1", None, None, 2), ("r2", None, None, 1),          # first samples: no gain/gap; r2 leads
                ("r1", 20.0, 180, 1), ("r2", 5.0, 180, 2)]             # t3: gains since t1, r1 leads now
            insert(conn, tick(t2, 15.0, 22.0))  # arrives later but sits between t1 and t3
            rows = conn.execute("select twitch_id, ts, gain, gap_s from streamer_sample where twitch_id = 'r1' order by ts").fetchall()
            assert [(r[2], r[3]) for r in rows] == [(None, None), (5.0, 60), (15.0, 120)]  # t3 re-based on t2
        finally:
            conn.execute("delete from streamer_sample where twitch_id in ('r1','r2')")
            conn.execute("delete from streamer where twitch_id in ('r1','r2')")
            conn.execute("delete from snapshot where ts in (%s,%s,%s)", (t1, t2, t3))
            conn.commit()


def test_recompute_mirror_split():
    """Own counter mirrors the source from the rebase minute: the jump, then min(own, source) gains."""
    from zevent_tracker.db import recompute
    ts = [datetime(2030, 1, 4, 0, i, tzinfo=timezone.utc) for i in range(4)]
    own = [100.0, 400.0, 410.0, 430.0]   # +300 jump (mirrors source total 300), then +10, +20
    src = [300.0, 300.0, 305.0, 330.0]   # +0, +5, +25
    with psycopg.connect(URL) as conn:
        try:
            for t, a, b in zip(ts, own, src):
                insert(conn, Parsed(Snapshot(t, 0.0, 0, 2, 2), [
                    StreamerSample(t, "own", "own", "Own", None, None, True, None, 0, a),
                    StreamerSample(t, "src", "src", "Src", None, None, True, None, 0, b)]))
            with conn.transaction():
                recompute(conn, ts[0], mirror=("own", "src", ts[1]))
            rows = conn.execute("select ts, dup_gain, dup, rank from streamer_sample where twitch_id = 'own' order by ts").fetchall()
            assert [(r[1], r[2]) for r in rows] == [(None, None), (300.0, 300.0), (5.0, 305.0), (20.0, 325.0)]
            assert [r[3] for r in rows] == [2, 2, 2, 2]  # own minus dup (100, 100, 105, 105) stays below src
            # recompute from a later start seeds dup from the previous row
            with conn.transaction():
                recompute(conn, ts[3], mirror=("own", "src", ts[1]))
            assert conn.execute("select dup from streamer_sample where twitch_id = 'own' and ts = %s", (ts[3],)).fetchone()[0] == 325.0
        finally:
            conn.execute("delete from streamer_sample where twitch_id in ('own','src')")
            conn.execute("delete from streamer where twitch_id in ('own','src')")
            conn.execute("delete from snapshot where ts = any(%s)", (ts,))
            conn.commit()
