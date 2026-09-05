"""Backfill from a third-party source for the hours before the collector started.

The collector's first sample is 2026-09-04 20:58:41 UTC; the event opened on 2026-09-03 at 18:00 UTC
(concert, donations open) and the channels' marathon on 2026-09-04 at 16:00 UTC. zevent.shellgratuit.com
runs a public Grafana whose datasource proxy exposes a Prometheus-style store scraping the same official
API every 30 seconds since 2026-09-03 01:00 UTC (values matched ours to the cent at the overlap).

Metrics: zevent_donations (event total) and, per channel, zevent_stream_donations, zevent_stream_viewers,
zevent_stream_online, labelled channel / channel_stylized / game / lan. A game change starts a new series,
so a channel can have several series; the latest raw sample wins at each tick.

Flow: fetch_raw() pulls raw samples in chunks (cached as JSON so re-runs do not hit the server),
resample() picks the latest sample at or before each minute tick, build_payload() turns a tick into the
official API's JSON shape, write_dumps() writes them as raw dump files (with a "_source" marker) that
`main.py backfill <dir>` imports like the collector's own dumps. Files under <out_dir>/_sources are
inputs (see load_overrides) and are ignored by the importer, whose file names must be timestamps.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("zevent.external")

SG_API = "https://zevent.shellgratuit.com/api/datasources/proxy/1/api/v1"
STREAM_METRICS = ("zevent_stream_donations", "zevent_stream_viewers", "zevent_stream_online")
GLOBAL_METRIC = "zevent_donations"
CHUNK = timedelta(hours=4)
MAX_AGE = 300  # seconds a raw sample may precede a tick and still count (Prometheus-style staleness)
SOURCE = "backfill:shellgratuit"
# Twitch logins that changed between the source's samples and ours: source login -> current login.
# drfeelgood became dfg (same Twitch id 38350595, same counter at the overlap).
ALIASES = {"drfeelgood": "dfg"}


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_raw(metric: str, start: datetime, end: datetime, cache_dir: Path, api: str = SG_API,
              chunk: timedelta = CHUNK, pause: float = 1.0) -> list[dict]:
    """Raw samples of `metric` in [start, end], as [{"metric": labels, "values": [[ts, "v"], ...]}, ...].

    Instant queries with a range selector (metric[4h]) return every raw sample with its timestamp; chunks
    are cached under cache_dir so a re-run is free for the server. Series of the same label set from
    different chunks are concatenated.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    by_labels: dict[str, dict] = {}
    t = start
    with httpx.Client(timeout=120.0) as client:
        while t < end:
            t2 = min(t + chunk, end)
            window = int((t2 - t).total_seconds())
            f = cache_dir / f"{metric}_{_iso(t2)}_{window}s.json"
            if f.exists():
                data = json.loads(f.read_text())
            else:
                r = client.get(f"{api}/query", params={"query": f"{metric}[{window}s]", "time": _iso(t2)})
                r.raise_for_status()
                data = r.json()
                if data.get("status") != "success":
                    raise RuntimeError(f"{metric} {t}..{t2}: {data}")
                f.write_text(json.dumps(data))
                log.info("fetched %s %s..%s: %d series", metric, _iso(t), _iso(t2), len(data["data"]["result"]))
                time.sleep(pause)
            for s in data["data"]["result"]:
                labels = {k: v for k, v in s["metric"].items() if k not in ("__name__", "instance", "job")}
                key = json.dumps(labels, sort_keys=True)
                entry = by_labels.setdefault(key, {"metric": labels, "values": []})
                entry["values"].extend((float(ts), v) for ts, v in s["values"])
            t = t2
    out = list(by_labels.values())
    for s in out:
        s["values"].sort()
    return out


def ticks(start: datetime, end: datetime, step: int = 60) -> list[datetime]:
    """Minute boundaries in [start, end]."""
    t = datetime.fromtimestamp((int(start.timestamp()) + step - 1) // step * step, tz=timezone.utc)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(seconds=step)
    return out


def resample(series: list[dict], tick_list: list[datetime], key: str, max_age: int = MAX_AGE) -> dict:
    """Latest raw sample at or before each tick, per value of label `key` (e.g. channel).

    Returns {tick: {key_value: (labels, value)}}. All series sharing the key (game variants) compete; the
    one with the most recent sample wins, so a game change is reflected from the tick after it happened.
    Samples older than max_age seconds are ignored, so a stalled scrape leaves a hole instead of a
    frozen value. "NaN" samples (staleness markers) are dropped.
    """
    # per key value: merged (ts, value, labels) sorted by ts
    merged: dict[str, list] = {}
    for s in series:
        k = s["metric"].get(key, "")
        lst = merged.setdefault(k, [])
        # "NaN" is the staleness marker written when a series ends (e.g. a game change): not a value
        lst.extend((ts, v, s["metric"]) for ts, v in s["values"] if v != "NaN")
    for lst in merged.values():
        lst.sort(key=lambda x: x[0])
    out = {t: {} for t in tick_list}
    for k, lst in merged.items():
        i = 0
        n = len(lst)
        for t in tick_list:
            tt = t.timestamp()
            while i < n and lst[i][0] <= tt:
                i += 1
            if i == 0:
                continue
            ts, v, labels = lst[i - 1]
            if tt - ts <= max_age:
                out[t][k] = (labels, v)
    return out


def build_payload(tick: datetime, total: float | None, channels: dict, identity: dict) -> dict | None:
    """One tick in the official API's shape.

    channels: {channel: {"donations": (labels, v), "viewers": ..., "online": ...}} from resample().
    identity: {channel: {"twitch_id", "display", "profile_url", "donation_url", "location"}}. Channels
    without a twitch_id are skipped (reported by the caller). Returns None when the event total is missing.
    """
    if total is None:
        return None
    live = []
    for ch, m in sorted(channels.items()):
        ident = identity.get(ALIASES.get(ch, ch))
        if not ident or not ident.get("twitch_id"):
            continue
        online_l, online_v = m.get("online", ({}, "0"))
        viewers = m.get("viewers", ({}, "0"))[1]
        donations = m.get("donations")
        if donations is None:
            continue
        labels = online_l or m.get("viewers", ({},))[0] or donations[0]
        is_online = float(online_v) > 0
        game = labels.get("game") or ("Offline" if not is_online else None)
        location = ident.get("location") or ("LAN" if labels.get("lan") == "1" else "Online")
        live.append({
            "twitch_id": ident["twitch_id"],
            "display": ident.get("display") or labels.get("channel_stylized") or ch,
            "twitch": ch,
            "profileUrl": ident.get("profile_url"),
            "online": is_online,
            "game": game,
            "viewersAmount": {"number": int(float(viewers)) if is_online else 0},
            "donationUrl": ident.get("donation_url"),
            "location": location,
            "donationAmount": {"number": float(donations[1])},
        })
    return {
        "live": live,
        "donationAmount": {"number": float(total)},
        "viewersCount": {"number": sum(s["viewersAmount"]["number"] for s in live)},
        "calendar": [],
        "_source": SOURCE,
        "_ts": _iso(tick),
    }


def write_dumps(payloads: dict, out_dir: Path) -> int:
    """payloads: {tick: payload}. Writes out_dir/YYYY-MM-DD/HHMMSS.json, the collector's layout."""
    n = 0
    for tick, payload in sorted(payloads.items()):
        if payload is None:
            continue
        d = out_dir / tick.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        (d / tick.strftime("%H%M%S.json")).write_text(json.dumps(payload, ensure_ascii=False))
        n += 1
    return n


def load_overrides(sources_dir: Path | None) -> list[dict]:
    """Per-channel donation counters that replace the source's, as resample() series.

    sources_dir/louis-julien/<login>.json holds [{"t": ms, "donations": eur}, ...] at 30-minute steps (see
    the README there): the source store reports the counters of the organisation channels "zevent" and
    "zeventplays" swapped and zeroed. Values are held until the next point (max_age in run()).
    """
    if not sources_dir or not (sources_dir / "louis-julien").is_dir():
        return []
    out = []
    for f in sorted((sources_dir / "louis-julien").glob("*.json")):
        pts = json.loads(f.read_text())
        out.append({"metric": {"channel": f.stem, "override": "louis-julien"},
                    "values": [(p["t"] / 1000, str(p["donations"])) for p in pts]})
    return out


OVERRIDE_HOLD = 31 * 60  # seconds: hold a 30-minute override point until the next one


def load_identity(database_url: str | None, participations: Path | None) -> dict:
    """channel login -> identity, from our streamer table first, then evenmorestats' participations
    (https://api.evenmorestats.fr/events/<id>/participations, which lists Twitch id and login)."""
    ident: dict[str, dict] = {}
    if participations and participations.exists():
        for p in json.loads(participations.read_text()):
            for s in p.get("streamers", []):
                tw = (s.get("socials") or {}).get("twitch") or {}
                if tw.get("login"):
                    ident[tw["login"].lower()] = {
                        "twitch_id": str(tw.get("id")), "display": s.get("name"),
                        "profile_url": s.get("profile_url"), "donation_url": None,
                        "location": {"lan": "LAN", "remote": "Online"}.get(p.get("location")),
                    }
    if database_url:
        import psycopg
        with psycopg.connect(database_url) as conn:
            rows = conn.execute("SELECT login, twitch_id, display, profile_url, donation_url, location FROM streamer")
            for login, twitch_id, display, profile_url, donation_url, location in rows:
                ident[login.lower()] = {"twitch_id": twitch_id, "display": display, "profile_url": profile_url,
                                        "donation_url": donation_url, "location": location}
    return ident


def run(start: datetime, end: datetime, cache_dir: Path, out_dir: Path, identity: dict,
        overrides: list[dict] = ()) -> dict:
    """Pull, resample and write. Returns counters and the list of channels that could not be identified."""
    tick_list = ticks(start, end)
    total = resample(fetch_raw(GLOBAL_METRIC, start, end, cache_dir), tick_list, key="__none__")
    per = {}
    for m in STREAM_METRICS:
        per[m] = resample(fetch_raw(m, start, end, cache_dir), tick_list, key="channel")
    over = resample(list(overrides), tick_list, key="channel", max_age=OVERRIDE_HOLD)
    payloads, unknown, missing_total = {}, set(), 0
    for t in tick_list:
        channels: dict[str, dict] = {}
        for m, short in zip(STREAM_METRICS, ("donations", "viewers", "online")):
            for ch, lv in per[m][t].items():
                channels.setdefault(ch, {})[short] = lv
        for ch, lv in over[t].items():
            if ch in channels:
                channels[ch]["donations"] = lv
        unknown.update(ch for ch in channels if not identity.get(ALIASES.get(ch, ch), {}).get("twitch_id"))
        tot = total[t].get("", (None, None))[1]
        if tot is None:
            missing_total += 1
        payloads[t] = build_payload(t, tot, channels, identity)
    written = write_dumps(payloads, out_dir)
    return {"ticks": len(tick_list), "written": written, "missing_total": missing_total, "unknown_channels": sorted(unknown)}


def main(argv: list[str]) -> None:
    """main.py pull-external START END OUT_DIR CACHE_DIR [PARTICIPATIONS_JSON]  (times in UTC, ISO)"""
    import os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start, end = (datetime.fromisoformat(a).replace(tzinfo=timezone.utc) for a in argv[:2])
    out_dir, cache_dir = Path(argv[2]), Path(argv[3])
    participations = Path(argv[4]) if len(argv) > 4 else None
    identity = load_identity(os.environ.get("DATABASE_URL"), participations)
    result = run(start, end, cache_dir, out_dir, identity, load_overrides(out_dir / "_sources"))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
