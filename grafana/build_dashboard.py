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


def stat(title, sql, x, w=6, y=0, unit=None, decimals=None, color="green", text_mode="value", description=None):
    d = {"color": {"mode": "fixed", "fixedColor": color}}
    extra = {"description": description} if description else {}
    if unit:
        d["unit"] = unit
    if decimals is not None:
        d["decimals"] = decimals
    return panel(
        "stat", title, sql, x, y, w, 4, fmt="table",
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


# Panels read the streamer_v / streamer_sample_v views (db/views.sql), not the raw tables. The views
# split mistermv's counter into his own donations and a derived "mistermv (private counter)" row
# holding the part that mirrors Domingo's counter (see the file header for the full story). Rows
# with derived = true are not API entities and must be left out of sums compared to the global total.
MIRROR_NOTE = (
    "All donations that could not be tied to a streamer: the event total minus the sum of the streamer counters. "
    "This holds donations made by people without picking a streamer (the Streamlabs Charity team page without a "
    "member), the tickets of the concert on Thursday, all shop donations, and so on. The derived "
    "\"mistermv (private counter)\" row is left out of the streamer sum: since 01:08 UTC on Sept 5 it mirrors "
    "Domingo's counter, which is already counted once."
)


def row(title, y):
    global _id
    _id += 1
    return {"id": _id, "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


panels = [
    stat("Total donations", "SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1", 0, w=6, unit="currencyEUR",
         decimals=2),
    stat("Donations, last hour",
         "SELECT max(donation_total) - min(donation_total) FROM snapshot WHERE ts > now() - interval '1 hour'", 6, w=6,
         unit="currencyEUR", decimals=2, color="orange"),
    stat("Donations not tied to a streamer",
         "SELECT sn.donation_total - sum(s.donation_total) FROM snapshot sn JOIN streamer_sample_v s USING (ts) "
         "WHERE NOT s.derived AND sn.ts = (SELECT max(ts) FROM snapshot) GROUP BY sn.donation_total",
         12, w=6, unit="currencyEUR", decimals=2, color="yellow", description=MIRROR_NOTE),
    stat("MisterMV's private counter",
         "SELECT coalesce(sum(donation_total), 0) FROM streamer_sample_v "
         "WHERE derived AND ts = (SELECT max(ts) FROM snapshot)",
         18, w=6, unit="currencyEUR", decimals=2, color="red",
         description="The part of mistermv's counter that mirrors Domingo's since 01:08 UTC on Sept 5: donations to "
                     "Domingo credited to both. Shown as the \"mistermv (private counter)\" entry in the leaderboards "
                     "and left out of \"Donations not tied to a streamer\"."),
    stat("Viewers now", "SELECT viewers_total FROM snapshot ORDER BY ts DESC LIMIT 1", 0, w=8, y=4, unit="short",
         color="purple"),
    # whole event, not the selected time range
    stat("Peak viewers", "SELECT max(viewers_total) FROM snapshot", 8, w=8, y=4, unit="short", color="purple"),
    stat("Streamers online",
         'SELECT streamers_online AS "Online", streamers_total AS "Total" FROM snapshot ORDER BY ts DESC LIMIT 1', 16,
         w=8, y=4, color="blue", text_mode="value_and_name"),

    row("Global", 8),
    ts("Total donations over time",
       'SELECT ts AS time, donation_total AS "Total" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
       0, 9, unit="currencyEUR", legend=False),
    ts("Viewers over time",
       'SELECT ts AS time, viewers_total AS "Viewers" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
       12, 9, unit="short", legend=False),
    ts("Donations per interval",
       'SELECT $__timeGroupAlias(ts, $__interval), sum(delta) AS "Donated" FROM ('
       '  SELECT ts, donation_total - lag(donation_total) OVER (ORDER BY ts) AS delta'
       '  FROM snapshot WHERE $__timeFilter(ts)'
       ') d WHERE delta IS NOT NULL GROUP BY 1 ORDER BY 1',
       0, 18, unit="currencyEUR", bars=True, legend=False, min_interval="5m"),
    ts("Streamers online",
       'SELECT ts AS time, streamers_online AS "Online" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
       12, 18, unit="short", legend=False),

    ts("Donations not tied to a streamer, over time (total minus all streamers)",
       'SELECT sn.ts AS time, sn.donation_total - sum(s.donation_total) FILTER (WHERE NOT s.derived) AS "Not tied to a streamer", '
       'coalesce(sum(s.donation_total) FILTER (WHERE s.derived), 0) AS "Mirrored (mistermv)" '
       'FROM snapshot sn JOIN streamer_sample_v s USING (ts) '
       'WHERE $__timeFilter(sn.ts) GROUP BY sn.ts, sn.donation_total ORDER BY 1',
       0, 27, unit="currencyEUR", description=MIRROR_NOTE),
    ts("Donations not tied to a streamer, per interval (total gain minus streamer gains)",
       'SELECT $__timeGroupAlias(ts, $__interval), sum(g) - sum(sg) AS "Not tied to a streamer" FROM ('
       f'  SELECT ts, {gain_expr("donation_total")} AS g FROM snapshot WHERE $__timeFilter(ts)'
       ') gl JOIN ('
       '  SELECT ts, sum(delta) AS sg FROM ('
       f'    SELECT ts, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta FROM streamer_sample_v WHERE NOT derived AND $__timeFilter(ts)'
       '  ) x GROUP BY ts'
       ') st USING (ts) WHERE g IS NOT NULL GROUP BY 1 ORDER BY 1',
       12, 27, unit="currencyEUR", bars=True, legend=False, min_interval="5m", description=MIRROR_NOTE),

    ts("Streamers online per game",
       "WITH top AS (SELECT game FROM streamer_sample_v WHERE $__timeFilter(ts) AND online GROUP BY game ORDER BY count(*) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Other' END AS metric, count(*) AS value FROM streamer_sample_v WHERE $__timeFilter(ts) AND online GROUP BY 1, 2 ORDER BY 1",
       0, 36, unit="short", stack=True),
    ts("Viewers per game",
       "WITH top AS (SELECT game FROM streamer_sample_v WHERE $__timeFilter(ts) AND online GROUP BY game ORDER BY sum(viewers) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Other' END AS metric, sum(viewers) AS value FROM streamer_sample_v WHERE $__timeFilter(ts) AND online GROUP BY 1, 2 ORDER BY 1",
       12, 36, unit="short", stack=True),

    row("Leaderboards (latest snapshot)", 45),
    table("Top by donations",
          'SELECT st.display AS "Streamer", s.donation_total AS "Donations", s.viewers AS "Viewers", s.online AS "Online", st.login AS login '
          'FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) '
          'WHERE s.ts = (SELECT max(ts) FROM snapshot) ORDER BY s.donation_total DESC LIMIT 25',
          0, 46, money_cols=("Donations",), streamer_links=True),
    table("Top by viewers",
          'SELECT st.display AS "Streamer", s.viewers AS "Viewers", s.game AS "Game", s.donation_total AS "Donations", st.login AS login '
          'FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) '
          'WHERE s.ts = (SELECT max(ts) FROM snapshot) AND s.online ORDER BY s.viewers DESC LIMIT 25',
          8, 46, money_cols=("Donations",), streamer_links=True),
    table("Top gained in selected range",
          'SELECT st.display AS "Streamer", sum(d.delta) AS "Gained", (array_agg(d.donation_total ORDER BY d.ts DESC))[1] AS "Donations", st.login AS login FROM ('
          f'  SELECT ts, twitch_id, donation_total, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta'
          '  FROM streamer_sample_v WHERE $__timeFilter(ts)'
          ') d JOIN streamer_v st USING (twitch_id) WHERE d.delta IS NOT NULL '
          'GROUP BY st.twitch_id, st.display, st.login ORDER BY 2 DESC LIMIT 25',
          16, 46, money_cols=("Gained", "Donations"), streamer_links=True),

    row("Per streamer ($streamer)", 58),
    stat("Donations of selected streamers",
         "SELECT coalesce(sum(donation_total), 0) FROM streamer_sample_v "
         "WHERE ts = (SELECT max(ts) FROM snapshot) AND twitch_id IN ($streamer) AND NOT derived",
         0, w=24, y=59, unit="currencyEUR", decimals=2,
         description="Sum of the selected streamers' counters at the latest snapshot. The derived "
                     "\"mistermv (private counter)\" entry is not real money and is left out even when selected."),
    ts("Donations per streamer",
       'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.donation_total AS value '
       'FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) '
       'WHERE $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) ORDER BY 1',
       0, 63, unit="currencyEUR", stack=True, streamer_links=True),
    ts("Viewers per streamer",
       'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.viewers AS value '
       'FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) '
       'WHERE $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) ORDER BY 1',
       12, 63, unit="short", stack=True, streamer_links=True),
    ts("Donations gained per streamer per interval",
       'SELECT $__timeGroupAlias(d.ts, $__interval), st.display AS metric, st.login AS login, sum(d.delta) AS value FROM ('
       f'  SELECT ts, twitch_id, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta'
       '  FROM streamer_sample_v WHERE $__timeFilter(ts) AND twitch_id IN ($streamer)'
       ') d JOIN streamer_v st USING (twitch_id) WHERE d.delta IS NOT NULL GROUP BY 1, 2, 3 ORDER BY 1',
       0, 72, unit="currencyEUR", bars=True, stack=True, min_interval="5m", streamer_links=True),
    table("Status of selected streamers",
          'WITH cur AS ('
          '  SELECT twitch_id, ts, online, game, viewers FROM streamer_sample_v'
          '  WHERE ts = (SELECT max(ts) FROM snapshot) AND twitch_id IN ($streamer)'
          '), changed AS ('
          '  SELECT c.twitch_id, max(s.ts) AS at FROM cur c JOIN streamer_sample_v s USING (twitch_id)'
          '  WHERE s.online <> c.online OR s.game IS DISTINCT FROM c.game GROUP BY c.twitch_id'
          ') '
          'SELECT st.display AS "Streamer", c.online AS "Online", c.game AS "Game", c.viewers AS "Viewers", '
          '       extract(epoch FROM c.ts - coalesce(ch.at, st.first_seen)) AS "Since", st.login AS login '
          'FROM cur c JOIN streamer_v st USING (twitch_id) LEFT JOIN changed ch USING (twitch_id) '
          'ORDER BY c.online DESC, c.viewers DESC',
          12, 72, w=12, h=9, duration_cols=("Since",), streamer_links=True),
]

dashboard = {
    "uid": "zevent",
    "title": "ZEVENT",
    "tags": ["zevent"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "30s",
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
                "query": "SELECT display AS __text, twitch_id AS __value FROM streamer_v ORDER BY lower(display)",
                "definition": "SELECT display AS __text, twitch_id AS __value FROM streamer_v ORDER BY lower(display)",
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
    "refresh": "30s",
    # hidden time picker also hides the refresh picker; from/to stay fixed at the values below
    "timepicker": {"hidden": False, "refresh_intervals": ["15s", "30s"]},
    "time": {"from": "2026-09-04T20:58:40.000Z", "to": "now"},
})
out_public = here / "provisioning-public" / "dashboards" / "zevent-public.json"
out_public.write_text(json.dumps(public, indent=2) + "\n")
print(f"wrote {out_public}")
