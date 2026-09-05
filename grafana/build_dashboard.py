"""Generate the Grafana dashboards. Run: uv run python grafana/build_dashboard.py

Writes two variants of the same dashboard:
  provisioning/dashboards/zevent.json               internal instance (full Grafana)
  provisioning-public/dashboards/zevent-public.json public instance: time picker hidden
                                                    (fixed range + 30s refresh), variables usable
"""
import copy
import json
from pathlib import Path

DS = {"type": "grafana-postgresql-datasource", "uid": "zevent-pg"}
_id = 0


def target(sql, fmt="time_series"):
    return {"datasource": DS, "rawQuery": True, "rawSql": sql.strip(), "format": fmt, "refId": "A"}


def panel(ptype, title, sql, x, y, w, h, fmt="time_series", **extra):
    global _id
    _id += 1
    p = {
        "id": _id,
        "type": ptype,
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(sql, fmt)],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    for k, v in extra.items():
        if k in ("fieldConfig", "options"):
            p[k].update(v)
        else:
            p[k] = v
    return p


def stat(title, sql, x, w=6, unit=None, decimals=None, color="green", text_mode="value", description=None):
    d = {"color": {"mode": "fixed", "fixedColor": color}}
    extra = {"description": description} if description else {}
    if unit:
        d["unit"] = unit
    if decimals is not None:
        d["decimals"] = decimals
    return panel(
        "stat", title, sql, x, 0, w, 4, fmt="table",
        fieldConfig={"defaults": d},
        options={
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value", "graphMode": "none", "textMode": text_mode, "justifyMode": "center",
        },
        **extra,
    )


def ts(title, sql, x, y, w=12, h=9, unit=None, bars=False, legend=True, stack=False, min_interval=None,
       streamer_links=False, description=None):
    d = {"custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "auto", "pointSize": 4, "spanNulls": True}}
    if unit:
        d["unit"] = unit
    if streamer_links:
        # with a second string column the datasource emits labels {metric, login} instead of naming
        # the field after `metric`, so restore the legend name explicitly
        d["displayName"] = "${__field.labels.metric}"
        d["links"] = [twitch_link("__field.labels.login")]
    if bars:
        d["custom"].update({"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 1, "showPoints": "never"})
    if stack:
        d["custom"].update({"stacking": {"mode": "normal", "group": "A"}, "fillOpacity": 60, "lineWidth": 1})
    extra = {}
    if min_interval:
        extra["interval"] = min_interval
    if description:
        extra["description"] = description
    return panel(
        "timeseries", title, sql, x, y, w, h,
        fieldConfig={"defaults": d},
        options={
            # NOTE: displayMode "hidden" is deprecated and makes Grafana 11 render an empty panel; use showLegend.
            "legend": {"showLegend": legend, "displayMode": "list", "placement": "bottom", "calcs": []},
            "tooltip": {"mode": "single" if stack else "multi", "sort": "desc"},
        },
        **extra,
    )


# Data link to the streamer's Twitch channel. Tables carry the login in a hidden "login" column
# (${__data.fields.login}); time series carry it as a field label (${__field.labels.login}), which
# the postgres datasource creates from any extra string column in a time_series query.
def twitch_link(var):
    return {"title": "Open on Twitch", "url": "https://twitch.tv/${" + var + "}", "targetBlank": True}


def table(title, sql, x, y, w=8, h=12, money_cols=(), duration_cols=(), streamer_links=False):
    overrides = [
                    {"matcher": {"id": "byName", "options": c},
                     "properties": [{"id": "unit", "value": "currencyEUR"}, {"id": "decimals", "value": 0}]}
                    for c in money_cols
                ] + [
                    {"matcher": {"id": "byName", "options": c}, "properties": [{"id": "unit", "value": "dtdurations"}]}
                    for c in duration_cols
                ]
    if streamer_links:
        overrides += [
            {"matcher": {"id": "byName", "options": "Streamer"},
             "properties": [{"id": "links", "value": [twitch_link("__data.fields.login")]}]},
            {"matcher": {"id": "byName", "options": "login"}, "properties": [{"id": "custom.hidden", "value": True}]},
        ]
    return panel(
        "table", title, sql, x, y, w, h, fmt="table",
        fieldConfig={"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                     "overrides": overrides},
        options={"showHeader": True, "sortBy": []},
    )


# Gain since the previous sample: plain difference, so a range total equals last minus first.
# Upstream blips (a streamer counter dropping to 0 and being restored minutes later, seen on
# 2026-09-04 at 21:28 UTC) then show as a symmetric -X/+X pair that cancels out, and real
# refunds stay negative. A "counter reset" rule was tried and produced phantom gains when the
# upstream restored the amount, so it was removed.
def gain_expr(col, partition=""):
    return f"{col} - lag({col}) OVER ({partition} ORDER BY ts)"


# Upstream double count: at 01:08 UTC on 2026-09-05 mistermv's counter jumped by 261K (Domingo's total
# as of ~00:47 UTC) while the global total moved 1.7K, and since then it moves in lockstep with
# Domingo's counter: every donation to Domingo is credited to both. The global total counts those
# once, so "global minus streamers" drifts negative. mistermv's own donations (before the rebase and
# the small extra on top of Domingo's increments after it) are real and stay credited to him.
# Correction per sample minute, from MIRROR_REBASE on:
#   rebase minute            -> the whole jump is duplicated
#   any later minute         -> least(mistermv delta, Domingo delta), floored at 0; the excess is his own
MIRROR_LOGIN = "mistermv"
MIRROR_SOURCE = "domingo"
MIRROR_REBASE = "2026-09-05 01:08:00+00"  # first sample with the mirrored counter

# duplicated increment per sample ts (rows only exist from MIRROR_REBASE on; LEFT JOIN and coalesce to 0)
DUP_DELTA_SQL = (
    "SELECT ts, CASE WHEN ts = '" + MIRROR_REBASE + "' THEN m ELSE greatest(least(m, dom), 0) END AS dup_delta FROM ("
    f"  SELECT ts, max(delta) FILTER (WHERE login = '{MIRROR_LOGIN}') AS m,"
    f"             max(delta) FILTER (WHERE login = '{MIRROR_SOURCE}') AS dom FROM ("
    "    SELECT s.ts, st.login, s.donation_total - lag(s.donation_total) OVER (PARTITION BY s.twitch_id ORDER BY s.ts) AS delta"
    f"    FROM streamer_sample s JOIN streamer st USING (twitch_id) WHERE st.login IN ('{MIRROR_LOGIN}', '{MIRROR_SOURCE}')"
    f"  ) x WHERE ts >= '{MIRROR_REBASE}' GROUP BY ts"
    ") y WHERE m IS NOT NULL AND dom IS NOT NULL"
)
# cumulative duplicated amount per sample ts
DUP_SQL = f"SELECT ts, sum(dup_delta) OVER (ORDER BY ts) AS dup FROM ({DUP_DELTA_SQL}) dd"
MIRROR_NOTE = (
    "Global total minus the sum of streamer counters. Since 01:08 UTC on Sept 5 mistermv's counter mirrors "
    "Domingo's (every donation to Domingo credited to both), so the part of his increments that matches "
    "Domingo's is removed from the sum; see the \"Duplicated (mistermv)\" tile for the amount removed."
)


def row(title, y):
    global _id
    _id += 1
    return {"id": _id, "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


panels = [
    stat("Total donations", "SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1", 0, w=4, unit="currencyEUR",
         decimals=2),
    stat("Donations, last hour",
         "SELECT max(donation_total) - min(donation_total) FROM snapshot WHERE ts > now() - interval '1 hour'", 4, w=3,
         unit="currencyEUR", decimals=2, color="orange"),
    stat("External donations",
         "SELECT sn.donation_total - sum(s.donation_total) + coalesce(max(d.dup), 0) "
         f"FROM snapshot sn JOIN streamer_sample s USING (ts) LEFT JOIN ({DUP_SQL}) d USING (ts) "
         "WHERE sn.ts = (SELECT max(ts) FROM snapshot) GROUP BY sn.donation_total",
         7, w=4, unit="currencyEUR", decimals=2, color="yellow", description=MIRROR_NOTE),
    stat("Duplicated (mistermv)",
         f"SELECT coalesce(d.dup, 0) FROM snapshot sn LEFT JOIN ({DUP_SQL}) d USING (ts) "
         "WHERE sn.ts = (SELECT max(ts) FROM snapshot)",
         11, w=4, unit="currencyEUR", decimals=2, color="red",
         description="Part of mistermv's counter that mirrors Domingo's since 01:08 UTC on Sept 5: the initial "
                     "jump plus, each minute, the increment matching Domingo's. Counted once in the global "
                     "total, twice in the streamer sum; removed from External donations. His own donations "
                     "stay credited to him."),
    stat("Viewers now", "SELECT viewers_total FROM snapshot ORDER BY ts DESC LIMIT 1", 15, w=3, unit="short",
         color="purple"),
    # whole event, not the selected time range
    stat("Peak viewers", "SELECT max(viewers_total) FROM snapshot", 18, w=3, unit="short", color="purple"),
    stat("Streamers online",
         'SELECT streamers_online AS "Online", streamers_total AS "Total" FROM snapshot ORDER BY ts DESC LIMIT 1', 21,
         w=3, color="blue", text_mode="value_and_name"),

    row("Global", 4),
    ts("Total donations over time",
       'SELECT ts AS time, donation_total AS "Total" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
       0, 5, unit="currencyEUR", legend=False),
    ts("Viewers over time",
       'SELECT ts AS time, viewers_total AS "Viewers" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
       12, 5, unit="short", legend=False),
    ts("Donations per interval",
       'SELECT $__timeGroupAlias(ts, $__interval), sum(delta) AS "Donated" FROM ('
       '  SELECT ts, donation_total - lag(donation_total) OVER (ORDER BY ts) AS delta'
       '  FROM snapshot WHERE $__timeFilter(ts)'
       ') d WHERE delta IS NOT NULL GROUP BY 1 ORDER BY 1',
       0, 14, unit="currencyEUR", bars=True, legend=False, min_interval="5m"),
    ts("Streamers online",
       'SELECT ts AS time, streamers_online AS "Online" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
       12, 14, unit="short", legend=False),

    ts("External donations over time (total minus all streamers)",
       'SELECT sn.ts AS time, sn.donation_total - sum(s.donation_total) + coalesce(max(d.dup), 0) AS "External", '
       'coalesce(max(d.dup), 0) AS "Duplicated (mistermv)" '
       f'FROM snapshot sn JOIN streamer_sample s USING (ts) LEFT JOIN ({DUP_SQL}) d USING (ts) '
       'WHERE $__timeFilter(sn.ts) GROUP BY sn.ts, sn.donation_total ORDER BY 1',
       0, 23, unit="currencyEUR", description=MIRROR_NOTE),
    ts("External donations per interval (global gain minus streamer gains)",
       'SELECT $__timeGroupAlias(ts, $__interval), sum(g) - sum(sg) + coalesce(sum(dd.dup_delta), 0) AS "External" FROM ('
       f'  SELECT ts, {gain_expr("donation_total")} AS g FROM snapshot WHERE $__timeFilter(ts)'
       ') gl JOIN ('
       '  SELECT ts, sum(delta) AS sg FROM ('
       f'    SELECT ts, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta FROM streamer_sample WHERE $__timeFilter(ts)'
       '  ) x GROUP BY ts'
       f') st USING (ts) LEFT JOIN ({DUP_DELTA_SQL}) dd USING (ts) WHERE g IS NOT NULL GROUP BY 1 ORDER BY 1',
       12, 23, unit="currencyEUR", bars=True, legend=False, min_interval="5m", description=MIRROR_NOTE),

    ts("Streamers online per game",
       "WITH top AS (SELECT game FROM streamer_sample WHERE $__timeFilter(ts) AND online GROUP BY game ORDER BY count(*) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Other' END AS metric, count(*) AS value FROM streamer_sample WHERE $__timeFilter(ts) AND online GROUP BY 1, 2 ORDER BY 1",
       0, 32, unit="short", stack=True),
    ts("Viewers per game",
       "WITH top AS (SELECT game FROM streamer_sample WHERE $__timeFilter(ts) AND online GROUP BY game ORDER BY sum(viewers) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Other' END AS metric, sum(viewers) AS value FROM streamer_sample WHERE $__timeFilter(ts) AND online GROUP BY 1, 2 ORDER BY 1",
       12, 32, unit="short", stack=True),

    row("Leaderboards (latest snapshot)", 41),
    table("Top by donations",
          'SELECT st.display AS "Streamer", s.donation_total AS "Donations", s.viewers AS "Viewers", s.online AS "Online", st.login AS login '
          'FROM streamer_sample s JOIN streamer st USING (twitch_id) '
          'WHERE s.ts = (SELECT max(ts) FROM snapshot) ORDER BY s.donation_total DESC LIMIT 25',
          0, 42, money_cols=("Donations",), streamer_links=True),
    table("Top by viewers",
          'SELECT st.display AS "Streamer", s.viewers AS "Viewers", s.game AS "Game", s.donation_total AS "Donations", st.login AS login '
          'FROM streamer_sample s JOIN streamer st USING (twitch_id) '
          'WHERE s.ts = (SELECT max(ts) FROM snapshot) AND s.online ORDER BY s.viewers DESC LIMIT 25',
          8, 42, money_cols=("Donations",), streamer_links=True),
    table("Top gained in selected range",
          'SELECT st.display AS "Streamer", sum(d.delta) AS "Gained", (array_agg(d.donation_total ORDER BY d.ts DESC))[1] AS "Donations", st.login AS login FROM ('
          f'  SELECT ts, twitch_id, donation_total, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta'
          '  FROM streamer_sample WHERE $__timeFilter(ts)'
          ') d JOIN streamer st USING (twitch_id) WHERE d.delta IS NOT NULL '
          'GROUP BY st.twitch_id, st.display, st.login ORDER BY 2 DESC LIMIT 25',
          16, 42, money_cols=("Gained", "Donations"), streamer_links=True),

    row("Per streamer ($streamer)", 54),
    ts("Donations per streamer",
       'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.donation_total AS value '
       'FROM streamer_sample s JOIN streamer st USING (twitch_id) '
       'WHERE $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) ORDER BY 1',
       0, 55, unit="currencyEUR", stack=True, streamer_links=True),
    ts("Viewers per streamer",
       'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.viewers AS value '
       'FROM streamer_sample s JOIN streamer st USING (twitch_id) '
       'WHERE $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) ORDER BY 1',
       12, 55, unit="short", stack=True, streamer_links=True),
    ts("Donations gained per streamer per interval",
       'SELECT $__timeGroupAlias(d.ts, $__interval), st.display AS metric, st.login AS login, sum(d.delta) AS value FROM ('
       f'  SELECT ts, twitch_id, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta'
       '  FROM streamer_sample WHERE $__timeFilter(ts) AND twitch_id IN ($streamer)'
       ') d JOIN streamer st USING (twitch_id) WHERE d.delta IS NOT NULL GROUP BY 1, 2, 3 ORDER BY 1',
       0, 64, unit="currencyEUR", bars=True, stack=True, min_interval="5m", streamer_links=True),
    table("Status of selected streamers",
          'WITH cur AS ('
          '  SELECT twitch_id, ts, online, game, viewers FROM streamer_sample'
          '  WHERE ts = (SELECT max(ts) FROM snapshot) AND twitch_id IN ($streamer)'
          '), changed AS ('
          '  SELECT c.twitch_id, max(s.ts) AS at FROM cur c JOIN streamer_sample s USING (twitch_id)'
          '  WHERE s.online <> c.online OR s.game IS DISTINCT FROM c.game GROUP BY c.twitch_id'
          ') '
          'SELECT st.display AS "Streamer", c.online AS "Online", c.game AS "Game", c.viewers AS "Viewers", '
          '       extract(epoch FROM c.ts - coalesce(ch.at, st.first_seen)) AS "Since", st.login AS login '
          'FROM cur c JOIN streamer st USING (twitch_id) LEFT JOIN changed ch USING (twitch_id) '
          'ORDER BY c.online DESC, c.viewers DESC',
          12, 64, w=12, h=9, duration_cols=("Since",), streamer_links=True),
]

dashboard = {
    "uid": "zevent",
    "title": "ZEVENT",
    "tags": ["zevent"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "15s",
    "time": {"from": "2026-09-04T20:00:00.000Z", "to": "now"},  # event start until now; grows as data arrives
    "schemaVersion": 39,
    "version": 1,
    "templating": {
        "list": [
            {
                "name": "streamer",
                "label": "Streamer",
                "type": "query",
                "datasource": DS,
                "query": "SELECT display AS __text, twitch_id AS __value FROM streamer ORDER BY lower(display)",
                "definition": "SELECT display AS __text, twitch_id AS __value FROM streamer ORDER BY lower(display)",
                "multi": True,
                "includeAll": True,
                "refresh": 1,
                "sort": 0,
                "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            }
        ]
    },
    "panels": panels,
}

here = Path(__file__).parent
out = here / "provisioning" / "dashboards" / "zevent.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len(panels)} panels)")

public = copy.deepcopy(dashboard)
public.update({
    "uid": "zevent-public",
    "editable": False,
    "refresh": "15s",
    # hidden time picker also hides the refresh picker; from/to stay fixed at the values below
    "timepicker": {"hidden": True, "refresh_intervals": ["15s"]},
    "time": {"from": "2026-09-04T20:58:40.000Z", "to": "now"},
})
out_public = here / "provisioning-public" / "dashboards" / "zevent-public.json"
out_public.write_text(json.dumps(public, indent=2) + "\n")
print(f"wrote {out_public}")
