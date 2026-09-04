from datetime import datetime, timezone

from zevent_tracker.collector import Config, tick, write_raw


def test_write_raw_layout(tmp_path):
    ts = datetime(2026, 9, 4, 20, 5, 7, tzinfo=timezone.utc)
    p = write_raw(tmp_path, ts, b"{}")
    assert p == tmp_path / "2026-09-04" / "200507.json"
    assert p.read_bytes() == b"{}"


def test_tick_swallows_errors(tmp_path, monkeypatch):
    def boom(url, timeout=20.0):
        raise RuntimeError("down")

    monkeypatch.setattr("zevent_tracker.collector.fetch", boom)
    cfg = Config(api_url="x", database_url="x", poll_interval=60, raw_dir=tmp_path)
    tick(cfg)  # must not raise
