"""Donation goals of every streamer, from evenmorestats (the API behind https://zevent.gdoc.fr/donation_goals).

    main.py pull-goals [--cached] [JSON] [SQL]

fetches the goals of every participation of the event and writes them twice: the raw API answer as JSON
(default raw-goals/donation_goals.json, committed like raw/) and db/donation_goals.sql, a self-contained
script that (re)creates and fills the donation_goal table. The SQL file is what gets deployed: on a fresh
database it runs from /docker-entrypoint-initdb.d (compose.yaml), on an existing one apply it by hand,
piping it in from the host like db/views.sql:

    docker compose exec -T db psql -U zevent -d zevent < db/donation_goals.sql

--cached rebuilds the SQL from the JSON without calling the API.

API shape (no key needed): GET /events/<event>/donation_goals/overview lists every participation with its
Twitch id; GET /participations/<id>/donation_goals lists its goals in site order, amounts in cents, with
the site's own `reached` flag (`accomplished` = the streamer did the thing). Categories seen in 2026:
donation (a threshold on the streamer's counter, 96% of goals), global (threshold on the event total),
donation_equal / donation_more_than / donation_largest (a single donation of that size), recurent
(repeats every N euros), incentive (a per-streamer rule).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import httpx

log = logging.getLogger("zevent.goals")

EMS_API = "https://api.evenmorestats.fr"
EVENT_ID = "019f5bd1-fe07-7d78-a326-a02198a9d50f"  # ZEvent 2026
UA = {"User-Agent": "zevent-tracker (https://zevent.jubnl.ch)"}
DEFAULT_JSON = Path("raw-goals/donation_goals.json")
DEFAULT_SQL = Path("db/donation_goals.sql")
# Twitch logins whose goals are never imported: clara_jones was removed from the event (and from our
# database) on 2026-09-07; the site had already dropped her by then, this keeps a later pull from
# bringing her back.
EXCLUDED_LOGINS = {"clara_jones"}

TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS donation_goal (
  id           text PRIMARY KEY,          -- evenmorestats goal id
  twitch_id    text NOT NULL,             -- streamer.twitch_id (no FK: this file may run before any sample)
  position     integer NOT NULL,          -- order on the site (amount ascending)
  amount       numeric(12,2) NOT NULL,    -- euros
  name         text NOT NULL,
  category     text NOT NULL,             -- donation | global | donation_equal | donation_more_than | donation_largest | recurent | incentive
  reached      boolean NOT NULL,          -- the site's flag
  accomplished boolean NOT NULL,          -- the streamer did it (the site's flag)
  links        text[] NOT NULL DEFAULT '{}'  -- clips / proof
);
CREATE INDEX IF NOT EXISTS donation_goal_twitch_idx ON donation_goal (twitch_id);
"""


def fetch(event_id: str = EVENT_ID, api: str = EMS_API, pause: float = 0.05, client: httpx.Client | None = None) -> list[dict]:
    """Every participation of the event with its goals: [{participation_id, name, location, twitch_id,
    twitch_login, goals: [api goal, ...]}, ...] in overview order."""
    own = client is None
    client = client or httpx.Client(timeout=60.0, headers=UA)
    try:
        r = client.get(f"{api}/events/{event_id}/donation_goals/overview")
        r.raise_for_status()
        out = []
        for p in r.json():
            tw = (p.get("socials") or {}).get("twitch") or {}
            g = client.get(f"{api}/participations/{p['id']}/donation_goals")
            g.raise_for_status()
            out.append({
                "participation_id": p["id"], "name": p["name"], "location": p.get("location"),
                "twitch_id": str(tw["id"]) if tw.get("id") is not None else None, "twitch_login": tw.get("login"),
                "goals": g.json(),
            })
            time.sleep(pause)
        log.info("fetched %d participations, %d goals", len(out), sum(len(p["goals"]) for p in out))
        return out
    finally:
        if own:
            client.close()


def rows(participations: list[dict]) -> list[tuple]:
    """(id, twitch_id, position, amount_eur, name, category, reached, accomplished, links) per goal,
    in site order; participations without a Twitch id (nothing to join them to) or in EXCLUDED_LOGINS
    are skipped."""
    out = []
    for p in participations:
        if not p.get("twitch_id"):
            log.warning("no twitch id for %s, skipped", p.get("name"))
            continue
        if (p.get("twitch_login") or "").lower() in EXCLUDED_LOGINS:
            log.info("%s is excluded, skipped", p.get("name"))
            continue
        for i, g in enumerate(p.get("goals") or []):
            out.append((g["id"], p["twitch_id"], i + 1, round(g["amount"] / 100, 2), g["name"].strip(),
                        g["category"], bool(g["reached"]), bool(g["accomplished"]), list(g.get("links") or [])))
    return out


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _row_sql(r: tuple) -> str:
    id_, twitch_id, pos, amount, name, category, reached, accomplished, links = r
    arr = "ARRAY[" + ", ".join(_q(l) for l in links) + "]::text[]" if links else "'{}'::text[]"
    return (f"({_q(id_)}, {_q(twitch_id)}, {pos}, {amount:.2f}, {_q(name)}, {_q(category)}, "
            f"{'true' if reached else 'false'}, {'true' if accomplished else 'false'}, {arr})")


def to_sql(participations: list[dict], fetched_at: str = "") -> str:
    """A transaction that creates donation_goal if needed and replaces its contents."""
    rs = rows(participations)
    head = [
        "-- Donation goals of every streamer, generated by `main.py pull-goals` (zevent_tracker/goals.py) from",
        "-- https://api.evenmorestats.fr" + (f" on {fetched_at}" if fetched_at else "") + ". Do not edit by hand.",
        "-- Fresh database: runs from /docker-entrypoint-initdb.d after init.sql. Existing database, from the host:",
        "--   docker compose exec -T db psql -U zevent -d zevent < db/donation_goals.sql",
        f"-- {len(rs)} goals, {len({r[1] for r in rs})} streamers.",
        "BEGIN;",
        TABLE_SQL.rstrip(),
        "TRUNCATE donation_goal;",
    ]
    body = []
    if rs:
        body.append("INSERT INTO donation_goal (id, twitch_id, position, amount, name, category, reached, accomplished, links) VALUES")
        body.append(",\n".join(_row_sql(r) for r in rs) + ";")
    return "\n".join(head + body + ["COMMIT;"]) + "\n"


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cached = "--cached" in argv
    args = [a for a in argv if a != "--cached"]
    json_path = Path(args[0]) if args else DEFAULT_JSON
    sql_path = Path(args[1]) if len(args) > 1 else DEFAULT_SQL
    if cached:
        doc = json.loads(json_path.read_text())
    else:
        doc = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event_id": EVENT_ID,
               "participations": fetch()}
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        log.info("wrote %s", json_path)
    sql_path.parent.mkdir(parents=True, exist_ok=True)
    sql_path.write_text(to_sql(doc["participations"], doc.get("fetched_at", "")))
    log.info("wrote %s (%d goals)", sql_path, len(rows(doc["participations"])))


if __name__ == "__main__":
    main(sys.argv[1:])
