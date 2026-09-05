import json
from datetime import datetime, timezone
from pathlib import Path

from zevent_tracker.external import OVERRIDE_HOLD, build_payload, load_overrides, resample, ticks, write_dumps
from zevent_tracker.parse import parse

T0 = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)


def ts(s):
    return T0.timestamp() + s


def test_ticks_are_minute_boundaries_inclusive():
    t = ticks(datetime(2026, 9, 3, 18, 0, 20, tzinfo=timezone.utc), datetime(2026, 9, 3, 18, 3, tzinfo=timezone.utc))
    assert [x.strftime("%H:%M:%S") for x in t] == ["18:01:00", "18:02:00", "18:03:00"]


def test_resample_latest_sample_wins_across_game_variants_and_respects_max_age():
    series = [
        {"metric": {"channel": "a", "game": "ZEVENT"}, "values": [(ts(-30), "1"), (ts(0), "2"), (ts(30), "3")]},
        {"metric": {"channel": "a", "game": "Dofus"}, "values": [(ts(50), "4")]},   # game change at +50s
        {"metric": {"channel": "a", "game": "ZEVENT"}, "values": [(ts(55), "NaN")]},  # staleness marker: ignored
        {"metric": {"channel": "b"}, "values": [(ts(-1000), "9")]},                  # too old for max_age
    ]
    t = ticks(T0, datetime(2026, 9, 3, 18, 1, tzinfo=timezone.utc))
    r = resample(series, t, key="channel", max_age=300)
    assert r[t[0]]["a"] == ({"channel": "a", "game": "ZEVENT"}, "2")       # sample at exactly the tick counts
    assert r[t[1]]["a"] == ({"channel": "a", "game": "Dofus"}, "4")        # newest sample, new game
    assert "b" not in r[t[0]]


def test_build_payload_matches_api_shape_and_parses(tmp_path):
    channels = {
        "a": {"donations": ({"channel": "a", "lan": "1", "game": "ZEVENT"}, "12.5"),
              "viewers": ({"channel": "a", "lan": "1", "game": "ZEVENT"}, "100"),
              "online": ({"channel": "a", "lan": "1", "game": "ZEVENT"}, "1")},
        "off": {"donations": ({"channel": "off", "lan": "0"}, "3"),
                "viewers": ({"channel": "off", "lan": "0"}, "7"),   # stale viewers on an offline channel are dropped
                "online": ({"channel": "off", "lan": "0"}, "0")},
        "nobody": {"donations": ({"channel": "nobody"}, "1")},       # no twitch id -> skipped
        "drfeelgood": {"donations": ({"channel": "drfeelgood"}, "5"), "online": ({}, "0")},  # renamed login
    }
    identity = {"a": {"twitch_id": "1", "display": "A", "profile_url": "p", "donation_url": "d", "location": "LAN"},
                "off": {"twitch_id": "2", "display": "Off"}, "dfg": {"twitch_id": "3", "display": "DFG"}}
    p = build_payload(T0, 1000.0, channels, identity)
    assert p["_source"].startswith("backfill")
    assert [s["twitch"] for s in p["live"]] == ["a", "drfeelgood", "off"]
    a, dfg, off = p["live"]
    assert dfg["twitch_id"] == "3" and dfg["display"] == "DFG"
    assert a["online"] is True and a["game"] == "ZEVENT" and a["viewersAmount"]["number"] == 100
    assert a["donationAmount"]["number"] == 12.5 and a["location"] == "LAN"
    assert off["online"] is False and off["game"] == "Offline" and off["viewersAmount"]["number"] == 0
    assert off["location"] == "Online"   # from the lan label when the identity has none
    assert p["viewersCount"]["number"] == 100
    parsed = parse(p, T0)
    assert parsed.snapshot.donation_total == 1000.0 and parsed.snapshot.streamers_online == 1
    assert parsed.snapshot.streamers_total == 3
    n = write_dumps({T0: p, datetime(2026, 9, 3, 18, 1, tzinfo=timezone.utc): None}, tmp_path)
    assert n == 1
    assert json.loads((tmp_path / "2026-09-03" / "180000.json").read_text())["_ts"] == "2026-09-03T18:00:00Z"


def test_build_payload_without_total_is_none():
    assert build_payload(T0, None, {}, {}) is None


def test_overrides_are_held_between_points(tmp_path):
    d = tmp_path / "louis-julien"
    d.mkdir()
    (d / "zevent.json").write_text(json.dumps([{"t": T0.timestamp() * 1000, "donations": 100.5},
                                                {"t": (T0.timestamp() + 1800) * 1000, "donations": 200}]))
    series = load_overrides(tmp_path)
    assert series[0]["metric"]["channel"] == "zevent"
    t = ticks(T0, datetime(2026, 9, 3, 19, 5, tzinfo=timezone.utc))
    r = resample(series, t, key="channel", max_age=OVERRIDE_HOLD)
    assert r[t[0]]["zevent"][1] == "100.5"
    assert r[t[29]]["zevent"][1] == "100.5"    # 18:29, held
    assert r[t[30]]["zevent"][1] == "200"      # 18:30, next point
    assert "zevent" not in r[t[65]]            # 19:05, more than 31 min after the last point
    assert load_overrides(None) == [] and load_overrides(tmp_path / "nope") == []
