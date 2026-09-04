import json
import os
from datetime import datetime, timezone

import psycopg
import pytest

from zevent_tracker.backfill import backfill, find_raw_files

URL = os.environ.get("TEST_DATABASE_URL")


def write(raw, day, hms, body):
    d = raw / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{hms}.json").write_bytes(body)


def test_find_raw_files_parses_ts_from_path(tmp_path):
    write(tmp_path, "2026-09-04", "205841", b"{}")
    write(tmp_path, "2026-09-05", "000100", b"{}")
    (tmp_path / "2026-09-05" / "notes.txt").write_text("ignore me")
    files = find_raw_files(tmp_path)
    assert [ts for ts, _ in files] == [
        datetime(2026, 9, 4, 20, 58, 41, tzinfo=timezone.utc),
        datetime(2026, 9, 5, 0, 1, 0, tzinfo=timezone.utc),
    ]


def test_find_raw_files_missing_dir(tmp_path):
    assert find_raw_files(tmp_path / "nope") == []


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
def test_backfill_imports_missing_and_skips_existing_and_corrupt(tmp_path):
    payload = json.dumps({
        "donationAmount": {"number": 10.5}, "viewersCount": {"number": 3},
        "live": [{"twitch_id": "bf1", "twitch": "bf", "display": "BF", "online": True, "game": "ZEVENT",
                  "viewersAmount": {"number": 3}, "donationAmount": {"number": 1.0}}],
    }).encode()
    write(tmp_path, "2031-01-01", "000000", payload)   # new
    write(tmp_path, "2031-01-01", "000100", payload)   # will pre-exist in DB
    write(tmp_path, "2031-01-01", "000200", b"{not json")  # corrupt
    t1 = datetime(2031, 1, 1, 0, 1, tzinfo=timezone.utc)
    with psycopg.connect(URL) as conn:
        conn.execute("INSERT INTO snapshot VALUES (%s, 0, 0, 0, 0)", (t1,))
        conn.commit()
        try:
            result = backfill(tmp_path, URL)
            n = conn.execute("SELECT count(*) FROM snapshot WHERE ts >= '2031-01-01' AND ts < '2031-01-02'").fetchone()[0]
            n_samples = conn.execute("SELECT count(*) FROM streamer_sample WHERE twitch_id = 'bf1'").fetchone()[0]
        finally:
            conn.execute("DELETE FROM streamer_sample WHERE twitch_id = 'bf1'")
            conn.execute("DELETE FROM streamer WHERE twitch_id = 'bf1'")
            conn.execute("DELETE FROM snapshot WHERE ts >= '2031-01-01' AND ts < '2031-01-02'")
            conn.commit()
    assert result == {"imported": 1, "skipped": 1, "failed": 1}
    assert n == 2
    assert n_samples == 1
