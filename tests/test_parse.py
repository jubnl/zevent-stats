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
