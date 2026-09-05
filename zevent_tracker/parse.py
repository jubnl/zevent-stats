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
    location: str | None = None  # "LAN" (on site) or "Online" (streaming from home)


@dataclass(frozen=True)
class Parsed:
    snapshot: Snapshot
    samples: list[StreamerSample]


def _num(obj: dict, key: str, default: float = 0):
    """Read `obj[key].number` (the API wraps numbers as {number, formatted})."""
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
            location=s.get("location"),
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
