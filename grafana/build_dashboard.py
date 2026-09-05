"""Generate the Grafana dashboards. Run: uv run python grafana/build_dashboard.py

All are served by the single anonymous, read-only Grafana instance (fixed time range, 30s refresh), and
each has buttons to the others:
  zevent-public.json           ZEVENT: headline tiles, global graphs, leaderboards. Location + Streamer filters
  zevent-live-public.json      ZEVENT live: one row per streamer, green while live; Location defaults to on site
  zevent-insights-public.json  ZEVENT insights: milestones and pace, notable moments, patterns, on site vs
                               remote, games, donations not tied to a streamer. Location filter
  zevent-streamer-public.json  ZEVENT streamer: one or a few streamers in detail; opens on the current leader
The "-public" uid suffix is kept because the URLs are published (the proxy redirects / to /d/zevent-public).
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


def table(title, sql, x, y, w=8, h=12, money_cols=(), duration_cols=(), hour_cols=(), image_cols=(), percent_cols=(),
          streamer_links=False, description=None):
    overrides = [
                    {"matcher": {"id": "byName", "options": c},
                     "properties": [{"id": "custom.cellOptions", "value": {"type": "image"}},
                                    {"id": "custom.width", "value": 44}, {"id": "displayName", "value": " "}]}
                    for c in image_cols
                ] + [
                    {"matcher": {"id": "byName", "options": c},
                     "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 2}]}
                    for c in percent_cols
                ] + [
                    {"matcher": {"id": "byName", "options": c},
                     "properties": [{"id": "unit", "value": "currencyEUR"}, {"id": "decimals", "value": 2}]}
                    for c in money_cols
                ] + [
                    {"matcher": {"id": "byName", "options": c}, "properties": [{"id": "unit", "value": "dtdurations"}]}
                    for c in duration_cols
                ] + [
                    {"matcher": {"id": "byName", "options": c},
                     "properties": [{"id": "unit", "value": "suffix: h"}, {"id": "decimals", "value": 1}]}
                    for c in hour_cols
                ]
    if streamer_links:
        overrides += [
            {"matcher": {"id": "byName", "options": "Streamer"},
             "properties": [{"id": "links", "value": [twitch_link("__data.fields.login")]}]},
            {"matcher": {"id": "byName", "options": "login"}, "properties": [{"id": "custom.hidden", "value": True}]},
        ]
    return panel(
        "table", title, sql, x, y, w, h, fmt="table",
        # minWidth: Grafana's default (150px) pushes a fifth column out of an 8-wide panel into a
        # horizontal scroll; let the columns share the panel width instead
        fieldConfig={"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "minWidth": 50}},
                     "overrides": overrides},
        options={"showHeader": True, "sortBy": []},
        **({"description": description} if description else {}),
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


# Location filter of the `location` dashboard variable: "LAN" (on site) or "Online" (streaming from
# home). Matched as a regex so "All" (allValue ".*") also keeps streamers whose location is unknown,
# e.g. one that left the API list before the column existed. `st` is the streamer_v alias.
LOC = "coalesce(st.location, '') ~ '^(${location:regex})$'"
LOC_QUERY = ("SELECT CASE location WHEN 'LAN' THEN 'On site (LAN)' ELSE 'Remote (online)' END AS __text, "
             "location AS __value FROM streamer_v WHERE location IS NOT NULL GROUP BY location ORDER BY location")


# Hours live = sum over live samples of the time to the streamer's next sample, capped at 5 minutes
# so a collector outage does not count as streaming. Follows the time range and both filters.
# `loc` is the SQL location filter (st = streamer_v).
def hours_streamed_sql(loc):
    return (
        "SELECT coalesce(sum(extract(epoch FROM least(nxt - ts, interval '5 minutes'))) / 3600, 0) FROM ("
        "  SELECT s.ts, s.online, lead(s.ts) OVER (PARTITION BY s.twitch_id ORDER BY s.ts) AS nxt"
        "  FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)"
        f"  WHERE NOT st.derived AND {loc} AND $__timeFilter(s.ts) AND s.twitch_id IN ($streamer)"
        ") x WHERE online AND nxt IS NOT NULL"
    )


HOURS_DESCRIPTION = (
    "Total time live, summed over the streamers matching the Location and Streamer filters, within the "
    "selected time range. Each live sample counts until the next sample (capped at 5 minutes, so gaps in "
    "the data do not count)."
)


# Same rule per streamer, as a CTE `h(twitch_id, hours)` for the leaderboards (selected time range).
HOURS_CTE = (
    "WITH h AS ("
    "  SELECT twitch_id, sum(extract(epoch FROM least(nxt - ts, interval '5 minutes'))) / 3600 AS hours FROM ("
    "    SELECT ts, twitch_id, online, lead(ts) OVER (PARTITION BY twitch_id ORDER BY ts) AS nxt"
    "    FROM streamer_sample_v WHERE $__timeFilter(ts)"
    "  ) x WHERE online AND nxt IS NOT NULL GROUP BY twitch_id"
    ") "
)


def hours_stat(x, w, loc):
    return stat("Hours streamed", hours_streamed_sql(loc), x, w=w, y=4, unit="suffix: h", decimals=1,
                color="green", description=HOURS_DESCRIPTION)


# Leaderboards share one shape: avatar, Streamer (linked to Twitch), Donations and Viewers at the latest
# snapshot, Hours live within the selected range (same rule as the "Hours streamed" stat). `extra_col` goes
# right after the name (the "Gained" column of the top-gained board) and needs its CTE and join.
def leaderboard(title, x, y, order_by, where="true", extra_cte="", extra_col="", extra_join="", money_cols=()):
    sql = (
        HOURS_CTE +
        ", cur AS ("
        "  SELECT twitch_id, donation_total, viewers, online FROM streamer_sample_v"
        "  WHERE ts = (SELECT max(ts) FROM snapshot)"
        ") " + extra_cte +
        f'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", {extra_col}cur.donation_total AS "Donations", '
        'cur.viewers AS "Viewers", coalesce(h.hours, 0) AS "Hours", st.login AS login '
        "FROM cur JOIN streamer_v st USING (twitch_id) LEFT JOIN h USING (twitch_id) " + extra_join +
        f"WHERE {where} AND " + LOC + f" ORDER BY {order_by} LIMIT 25"
    )
    return table(title, sql, x, y, w=12, money_cols=money_cols + ("Donations",), hour_cols=("Hours",),
                 image_cols=("Avatar",), streamer_links=True)


def row(title, y):
    global _id
    _id += 1
    return {"id": _id, "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def reset_ids():
    global _id
    _id = 0


# Per-streamer gain within the selected range, as a CTE `g(twitch_id, gained)`.
GAIN_CTE = (
    "g AS ("
    "  SELECT twitch_id, sum(delta) AS gained FROM ("
    f"    SELECT ts, twitch_id, {gain_expr('donation_total', 'PARTITION BY twitch_id')} AS delta"
    "    FROM streamer_sample_v WHERE $__timeFilter(ts)"
    "  ) d WHERE delta IS NOT NULL GROUP BY twitch_id"
    ") "
)


def barchart(title, sql, x, y, w=12, h=9, x_field="", unit=None, description=None):
    d = {"custom": {"fillOpacity": 80, "lineWidth": 1}, "color": {"mode": "fixed", "fixedColor": "green"}}
    if unit:
        d["unit"] = unit
    extra = {"description": description} if description else {}
    return panel(
        "barchart", title, sql, x, y, w, h, fmt="table",
        fieldConfig={"defaults": d},
        options={"orientation": "auto", "xField": x_field, "showValue": "never", "barWidth": 0.8,
                 "legend": {"showLegend": False}, "tooltip": {"mode": "single", "sort": "none"}},
        **extra,
    )


# ---------------------------------------------------------------------------------------------------
# ZEVENT (main): headline tiles, the global graphs and the leaderboards. Everything else lives in the
# insights and streamer dashboards.
def main_panels():
    reset_ids()
    return [
        stat("Total donations", "SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1", 0, w=6,
             unit="currencyEUR", decimals=2),
        stat("Donations, last hour",
             "SELECT max(donation_total) - min(donation_total) FROM snapshot WHERE ts > now() - interval '1 hour'", 6,
             w=6, unit="currencyEUR", decimals=2, color="orange"),
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
        # Viewers of the streamers matching the Location and Streamer filters (sum of the per-streamer counts,
        # which equals the API's global viewer count).
        stat("Viewers now",
             "SELECT coalesce(sum(s.viewers), 0) FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC,
             0, w=6, y=4, unit="short", color="purple",
             description="Viewers of the streamers matching the Location and Streamer filters, at the latest sample."),
        stat("Peak viewers",
             "SELECT coalesce(max(v), 0) FROM ("
             "  SELECT s.ts, sum(s.viewers) AS v FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)"
             "  WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC +
             "  GROUP BY s.ts"
             ") x",
             6, w=6, y=4, unit="short", color="purple",
             description="Highest combined viewer count of the streamers matching the Location and Streamer filters, "
                         "within the selected time range."),
        stat("Streamers online",
             'SELECT streamers_online AS "Online", streamers_total AS "Total" FROM snapshot ORDER BY ts DESC LIMIT 1',
             12, w=6, y=4, color="blue", text_mode="value_and_name"),
        hours_stat(18, 6, LOC),

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

        row("Leaderboards (latest snapshot, $location)", 27),
        # 2x2 grid
        leaderboard("Top by donations", 0, 28, order_by="cur.donation_total DESC"),
        leaderboard("Top by viewers", 12, 28, order_by="cur.viewers DESC", where="cur.online"),
        leaderboard("Top gained in selected range", 0, 40, order_by="g.gained DESC",
                    extra_cte=", " + GAIN_CTE, extra_col='g.gained AS "Gained", ',
                    extra_join="JOIN g USING (twitch_id) ", money_cols=("Gained",)),
        leaderboard("Top by hours streamed in selected range", 12, 40,
                    order_by="coalesce(h.hours, 0) DESC, cur.donation_total DESC"),
    ]


# ---------------------------------------------------------------------------------------------------
# ZEVENT insights: milestones and pace, notable moments, patterns, on site vs remote, games, and the
# donations that cannot be tied to a streamer. Location filter only.
BLIP_NOTE = (
    "Gains within one minute of a streamer's counter. The organisation's own channels (ZEVENT, ZEventPlays) are "
    "left out: their counters move in lumps (tickets, shop) and, before Sept 4 22:58 CEST, come from a 30-minute "
    "source. Gains are measured between two samples at most 5 minutes apart, so a gap in "
    "the data is not counted as one minute). A restore after an upstream blip (a counter dropping to 0 and "
    "coming back minutes later) is left out: rows whose streamer lost at least 90% of the amount in the "
    "previous 10 minutes are skipped."
)

# Time to the streamer's next sample, capped so gaps in the data do not count; used for viewer-hours.
DT = "extract(epoch FROM least(nxt - ts, interval '5 minutes'))"


def insights_panels():
    reset_ids()
    rate = (
        "WITH cur AS (SELECT ts, donation_total FROM snapshot ORDER BY ts DESC LIMIT 1), "
        "past AS (SELECT sn.donation_total FROM snapshot sn, cur WHERE sn.ts <= cur.ts - interval '30 minutes' "
        "         ORDER BY sn.ts DESC LIMIT 1) "
    )
    by_loc = ("FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
              "WHERE $__timeFilter(s.ts) AND NOT st.derived AND " + LOC)
    return [
        stat("Donation rate, last 30 min",
             rate + "SELECT (cur.donation_total - past.donation_total) / 30 FROM cur, past",
             0, w=6, unit="currencyEUR", decimals=0, color="orange",
             description="Event total gained per minute over the last 30 minutes of data."),
        stat("Average rate since the start",
             "SELECT (max(donation_total) - min(donation_total)) / greatest(extract(epoch FROM max(ts) - min(ts)) / 60, 1) "
             "FROM snapshot",
             6, w=6, unit="currencyEUR", decimals=0, color="orange", description="Event total per minute, whole event."),
        stat("Next million",
             "SELECT (floor(donation_total / 1e6) + 1) * 1e6 FROM snapshot ORDER BY ts DESC LIMIT 1",
             12, w=6, unit="currencyEUR", decimals=0, color="green"),
        stat("Next million in",
             rate + "SELECT CASE WHEN cur.donation_total > past.donation_total THEN "
                    "((floor(cur.donation_total / 1e6) + 1) * 1e6 - cur.donation_total) "
                    "/ ((cur.donation_total - past.donation_total) / 1800.0) END FROM cur, past",
             18, w=6, unit="dtdurations", decimals=0, color="green",
             description="At the donation rate of the last 30 minutes."),

        row("Milestones and moments", 4),
        table("Millions, one by one",
              "WITH m AS ("
              "  SELECT ts, floor(max(donation_total) OVER (ORDER BY ts) / 1e6) AS mil FROM snapshot"
              "), c AS ("
              "  SELECT ts, mil, lag(mil) OVER (ORDER BY ts) AS prev FROM m"
              "), x AS (SELECT ts, mil FROM c WHERE prev IS NOT NULL AND mil > prev) "
              'SELECT mil * 1e6 AS "Milestone", ts AS "Reached at", '
              '       extract(epoch FROM ts - lag(ts) OVER (ORDER BY ts)) AS "After the previous", '
              '       extract(epoch FROM ts - (SELECT min(ts) FROM snapshot)) AS "Since the start" '
              "FROM x ORDER BY mil DESC",
              0, 5, w=12, h=9, money_cols=("Milestone",), duration_cols=("After the previous", "Since the start"),
              description="When the event total crossed each round million (running maximum, so a counter dip "
                          "cannot cross the same million twice)."),
        table("Largest single-minute gains",
              "WITH d AS ("
              f"  SELECT ts, twitch_id, {gain_expr('donation_total', 'PARTITION BY twitch_id')} AS delta, "
              "         ts - lag(ts) OVER (PARTITION BY twitch_id ORDER BY ts) AS gap"
              "  FROM streamer_sample_v WHERE $__timeFilter(ts) AND NOT derived"
              "), e AS ("
              "  SELECT *, min(delta) OVER (PARTITION BY twitch_id ORDER BY ts ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING) AS drop"
              "  FROM d WHERE delta IS NOT NULL AND gap <= interval '5 minutes'"
              ") "
              'SELECT e.ts AS "Time", st.display AS "Streamer", e.delta AS "Gain", st.login AS login '
              "FROM e JOIN streamer_v st USING (twitch_id) "
              "WHERE e.delta > 0 AND coalesce(e.drop, 0) > -0.9 * e.delta AND st.login NOT IN ('zevent', 'zeventplays') "
              "AND " + LOC + " ORDER BY e.delta DESC LIMIT 25",
              12, 5, w=12, h=9, money_cols=("Gain",), streamer_links=True, description=BLIP_NOTE),

        row("Patterns", 14),
        barchart("Donations by hour of the day (Europe/Paris)",
                 "SELECT to_char(h, 'FM00\"h\"') AS \"Hour\", sum(delta) AS \"Donated\" FROM ("
                 "  SELECT extract(hour FROM ts AT TIME ZONE 'Europe/Paris') AS h, "
                 f"         {gain_expr('donation_total')} AS delta"
                 "  FROM snapshot WHERE $__timeFilter(ts)"
                 ") d WHERE delta IS NOT NULL GROUP BY h ORDER BY h",
                 0, 15, x_field="Hour", unit="currencyEUR",
                 description="Event total gained per hour of the day, summed over the selected range (all locations)."),
        table("Donations per viewer-hour",
              "WITH v AS ("
              f"  SELECT twitch_id, sum(viewers * {DT}) / 3600 AS viewer_hours, "
              f"         sum({DT}) FILTER (WHERE online) / 3600 AS hours"
              "  FROM (SELECT ts, twitch_id, online, viewers, lead(ts) OVER (PARTITION BY twitch_id ORDER BY ts) AS nxt"
              "        FROM streamer_sample_v WHERE $__timeFilter(ts)) x"
              "  WHERE nxt IS NOT NULL GROUP BY twitch_id"
              "), " + GAIN_CTE +
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", g.gained / v.viewer_hours AS "Per viewer-hour", '
              'g.gained AS "Gained", v.viewer_hours AS "Viewer-hours", v.hours AS "Hours", st.login AS login '
              "FROM g JOIN v USING (twitch_id) JOIN streamer_v st USING (twitch_id) "
              "WHERE NOT st.derived AND v.viewer_hours >= 100 AND " + LOC + " ORDER BY 3 DESC LIMIT 25",
              12, 15, w=12, h=9, money_cols=("Per viewer-hour", "Gained"), hour_cols=("Hours",), image_cols=("Avatar",),
              streamer_links=True,
              description="Gained in the selected range divided by viewer-hours (viewers summed over time): how much a "
                          "community gives relative to its size. Streamers with fewer than 100 viewer-hours are left out."),
        table("Trending now (last 15 minutes)",
              "WITH cur AS (SELECT max(ts) AS ts FROM snapshot), "
              "n AS (SELECT twitch_id, donation_total, viewers FROM streamer_sample_v, cur WHERE streamer_sample_v.ts = cur.ts), "
              "p AS (SELECT DISTINCT ON (ss.twitch_id) ss.twitch_id, ss.donation_total, ss.viewers FROM streamer_sample_v ss, cur "
              "      WHERE ss.ts BETWEEN cur.ts - interval '60 minutes' AND cur.ts - interval '15 minutes' "
              "      ORDER BY ss.twitch_id, ss.ts DESC) "
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", n.donation_total - p.donation_total AS "Gained", '
              'n.viewers AS "Viewers", n.viewers - p.viewers AS "Viewers change", st.login AS login '
              "FROM n JOIN p USING (twitch_id) JOIN streamer_v st USING (twitch_id) "
              "WHERE NOT st.derived AND " + LOC + " ORDER BY 3 DESC LIMIT 25",
              0, 24, w=12, h=9, money_cols=("Gained",), image_cols=("Avatar",), streamer_links=True,
              description="Streamers by donations gained over the last 15 minutes of data, with the change in viewers "
                          "(compared with the latest sample at least 15 minutes old, looking back up to an hour)."),
        ts("Donations gained per interval, by location",
           "SELECT $__timeGroupAlias(d.ts, $__interval), coalesce(st.location, 'Unknown') AS metric, sum(d.delta) AS value FROM ("
           f"  SELECT ts, twitch_id, {gain_expr('donation_total', 'PARTITION BY twitch_id')} AS delta"
           "  FROM streamer_sample_v WHERE $__timeFilter(ts) AND NOT derived"
           ") d JOIN streamer_v st USING (twitch_id) WHERE d.delta IS NOT NULL AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           12, 24, unit="currencyEUR", bars=True, stack=True, min_interval="5m"),

        row("On site vs remote", 33),
        ts("Live streamers by location",
           "SELECT s.ts AS time, coalesce(st.location, 'Unknown') AS metric, count(*) FILTER (WHERE s.online) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           0, 34, unit="short", stack=True),
        ts("Viewers by location",
           "SELECT s.ts AS time, coalesce(st.location, 'Unknown') AS metric, sum(s.viewers) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           12, 34, unit="short", stack=True),

        row("Games", 43),
        ts("Streamers online per game",
           "WITH top AS (SELECT game FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY game ORDER BY count(*) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Other' END AS metric, count(*) AS value FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           0, 44, unit="short", stack=True),
        ts("Viewers per game",
           "WITH top AS (SELECT game FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY game ORDER BY sum(viewers) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Other' END AS metric, sum(viewers) AS value FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           12, 44, unit="short", stack=True),
        table("Hours streamed per game",
              "SELECT x.game AS \"Game\", sum(" + DT + ") / 3600 AS \"Hours\", count(DISTINCT x.twitch_id) AS \"Streamers\", "
              "       sum(x.viewers * " + DT + ") / 3600 AS \"Viewer-hours\" FROM ("
              "  SELECT s.ts, s.twitch_id, s.online, s.viewers, coalesce(s.game, '(no game)') AS game, "
              "         lead(s.ts) OVER (PARTITION BY s.twitch_id ORDER BY s.ts) AS nxt "
              + by_loc +
              ") x WHERE x.online AND x.nxt IS NOT NULL GROUP BY x.game ORDER BY 2 DESC LIMIT 25",
              0, 53, w=24, h=9, hour_cols=("Hours",)),

        row("Donations not tied to a streamer", 62),
        ts("Over time (total minus all streamers)",
           'SELECT sn.ts AS time, sn.donation_total - sum(s.donation_total) FILTER (WHERE NOT s.derived) AS "Not tied to a streamer", '
           'coalesce(sum(s.donation_total) FILTER (WHERE s.derived), 0) AS "Mirrored (mistermv)" '
           'FROM snapshot sn JOIN streamer_sample_v s USING (ts) '
           'WHERE $__timeFilter(sn.ts) GROUP BY sn.ts, sn.donation_total ORDER BY 1',
           0, 63, unit="currencyEUR", description=MIRROR_NOTE),
        ts("Per interval (total gain minus streamer gains)",
           'SELECT $__timeGroupAlias(ts, $__interval), sum(g) - sum(sg) AS "Not tied to a streamer" FROM ('
           f'  SELECT ts, {gain_expr("donation_total")} AS g FROM snapshot WHERE $__timeFilter(ts)'
           ') gl JOIN ('
           '  SELECT ts, sum(delta) AS sg FROM ('
           f'    SELECT ts, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta FROM streamer_sample_v WHERE NOT derived AND $__timeFilter(ts)'
           '  ) x GROUP BY ts'
           ') st USING (ts) WHERE g IS NOT NULL GROUP BY 1 ORDER BY 1',
           12, 63, unit="currencyEUR", bars=True, legend=False, min_interval="5m", description=MIRROR_NOTE),
    ]


# ---------------------------------------------------------------------------------------------------
# ZEVENT streamer: one or a few streamers in detail. The Streamer filter has no "All" and lists streamers
# by donations, so the dashboard opens on the current leader.
def streamer_panels():
    reset_ids()
    sel = ("FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
           "WHERE NOT st.derived AND s.twitch_id IN ($streamer)")
    return [
        stat("Donations",
             f"SELECT coalesce(sum(s.donation_total), 0) {sel} AND s.ts = (SELECT max(ts) FROM snapshot)",
             0, w=4, unit="currencyEUR", decimals=2),
        stat("Gained in selected range",
             "WITH " + GAIN_CTE + "SELECT coalesce(sum(g.gained), 0) FROM g JOIN streamer_v st USING (twitch_id) "
             "WHERE NOT st.derived AND g.twitch_id IN ($streamer)",
             4, w=4, unit="currencyEUR", decimals=2, color="orange"),
        stat("Best rank",
             "SELECT min(r) FROM (SELECT twitch_id, rank() OVER (ORDER BY donation_total DESC) AS r "
             "FROM streamer_sample_v WHERE ts = (SELECT max(ts) FROM snapshot) AND NOT derived) x "
             "WHERE twitch_id IN ($streamer)",
             8, w=4, unit="short", color="yellow",
             description="Position in the donation leaderboard at the latest sample (best of the selected streamers)."),
        stat("Viewers now",
             f"SELECT coalesce(sum(s.viewers), 0) {sel} AND s.ts = (SELECT max(ts) FROM snapshot)",
             12, w=4, unit="short", color="purple"),
        stat("Peak viewers",
             f"SELECT coalesce(max(v), 0) FROM (SELECT s.ts, sum(s.viewers) AS v {sel} AND $__timeFilter(s.ts) GROUP BY s.ts) x",
             16, w=4, unit="short", color="purple", description="Highest combined viewer count in the selected range."),
        stat("Hours streamed", hours_streamed_sql("true"), 20, w=4, unit="suffix: h", decimals=1, color="green",
             description=HOURS_DESCRIPTION),

        table("Selected streamers",
              HOURS_CTE + ", " + GAIN_CTE + ", "
              "cur AS ("
              "  SELECT twitch_id, ts, online, game, viewers, donation_total, "
              "         rank() OVER (ORDER BY donation_total DESC) AS rank, "
              "         donation_total / nullif((SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1), 0) AS share"
              "  FROM streamer_sample_v WHERE ts = (SELECT max(ts) FROM snapshot) AND NOT derived"
              "), v AS ("
              f"  SELECT twitch_id, max(viewers) AS peak, sum(viewers * {DT}) / nullif(sum({DT}) FILTER (WHERE online), 0) AS avg_live"
              "  FROM (SELECT ts, twitch_id, online, viewers, lead(ts) OVER (PARTITION BY twitch_id ORDER BY ts) AS nxt"
              "        FROM streamer_sample_v WHERE $__timeFilter(ts) AND twitch_id IN ($streamer)) x"
              "  WHERE nxt IS NOT NULL GROUP BY twitch_id"
              "), changed AS ("
              "  SELECT c.twitch_id, max(s.ts) AS at FROM cur c JOIN streamer_sample_v s USING (twitch_id)"
              "  WHERE c.twitch_id IN ($streamer) AND (s.online <> c.online OR s.game IS DISTINCT FROM c.game) GROUP BY c.twitch_id"
              ") "
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", '
              "       CASE st.location WHEN 'LAN' THEN 'On site' WHEN 'Online' THEN 'Remote' END AS \"Location\", "
              '       c.online AS "Live", c.game AS "Game", '
              '       extract(epoch FROM c.ts - coalesce(ch.at, st.first_seen)) AS "Since", '
              '       c.rank AS "Rank", c.donation_total AS "Donations", c.share AS "Share of total", '
              '       coalesce(g.gained, 0) AS "Gained", c.viewers AS "Viewers", v.peak AS "Peak viewers", '
              '       v.avg_live AS "Avg viewers", coalesce(h.hours, 0) AS "Hours", st.login AS login '
              "FROM cur c JOIN streamer_v st USING (twitch_id) LEFT JOIN h USING (twitch_id) LEFT JOIN g USING (twitch_id) "
              "LEFT JOIN v USING (twitch_id) LEFT JOIN changed ch USING (twitch_id) "
              "WHERE c.twitch_id IN ($streamer) ORDER BY c.donation_total DESC",
              0, 4, w=24, h=7, money_cols=("Donations", "Gained"), duration_cols=("Since",), hour_cols=("Hours",),
              image_cols=("Avatar",), percent_cols=("Share of total",), streamer_links=True,
              description="Rank and share are of the event total at the latest sample; Gained, Peak viewers, Avg viewers "
                          "(average while live) and Hours are within the selected range. Since: time in the current "
                          "live/game state."),

        ts("Donations over time",
           'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.donation_total AS value '
           f'{sel} AND $__timeFilter(s.ts) ORDER BY 1',
           0, 11, unit="currencyEUR", streamer_links=True),
        ts("Viewers over time",
           'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.viewers AS value '
           f'{sel} AND $__timeFilter(s.ts) ORDER BY 1',
           12, 11, unit="short", streamer_links=True),
        ts("Donations gained per interval",
           'SELECT $__timeGroupAlias(d.ts, $__interval), st.display AS metric, st.login AS login, sum(d.delta) AS value FROM ('
           f'  SELECT ts, twitch_id, {gain_expr("donation_total", "PARTITION BY twitch_id")} AS delta'
           '  FROM streamer_sample_v WHERE $__timeFilter(ts) AND twitch_id IN ($streamer)'
           ') d JOIN streamer_v st USING (twitch_id) WHERE d.delta IS NOT NULL GROUP BY 1, 2, 3 ORDER BY 1',
           0, 20, unit="currencyEUR", bars=True, stack=True, min_interval="5m", streamer_links=True),
        ts("Rank in the donation leaderboard over time",
           "SELECT r.ts AS time, st.display AS metric, st.login AS login, r.rank AS value FROM ("
           "  SELECT ts, twitch_id, rank() OVER (PARTITION BY ts ORDER BY donation_total DESC) AS rank"
           "  FROM streamer_sample_v WHERE $__timeFilter(ts) AND NOT derived"
           ") r JOIN streamer_v st USING (twitch_id) WHERE r.twitch_id IN ($streamer) ORDER BY 1",
           12, 20, unit="short", streamer_links=True, description="1 is the top; lower is better."),
        game_timeline(0, 29),
    ]


def game_timeline(x, y):
    """State timeline of the games played by the selected streamers, one colour per game."""
    sql = LIVE_SQL.format(loc="true", filter=" AND s.twitch_id IN ($streamer)")
    return panel(
        "state-timeline", "Games played", sql, x, y, 24, 8, fmt="table",
        fieldConfig={"defaults": {
            "custom": {"lineWidth": 0, "fillOpacity": 85, "spanNulls": False, "insertNulls": False},
            "color": {"mode": "palette-classic"},
            "displayName": "${__field.labels.Streamer}",
        }},
        options={
            "mergeValues": True, "showValue": "auto", "alignValue": "left", "rowHeight": 0.8, "perPage": 15,
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        transformations=[
            {"id": "organize", "options": {"excludeByName": {"first_live": True}}},
            {"id": "partitionByValues",
             "options": {"fields": ["Streamer"], "keepFields": False, "naming": {"asLabels": True}}},
        ],
        description="What each selected streamer was playing while live; gaps are offline time.",
    )


# ---------------------------------------------------------------------------------------------------
def query_var(name, label, query, all_value=None, multi=True, include_all=True):
    v = {
        "name": name, "label": label, "type": "query", "datasource": DS,
        "query": query, "definition": query,
        "multi": multi, "includeAll": include_all, "refresh": 1, "sort": 0,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]} if include_all else {},
    }
    if all_value is not None:
        v["allValue"] = all_value
    return v


def streamer_var(where):
    return query_var("streamer", "Streamer",
                     f"SELECT display AS __text, twitch_id AS __value FROM streamer_v st WHERE {where} ORDER BY lower(display)")


# Streamer filter of the streamer dashboard: no "All", ordered by donations so the first (default) entry
# is the current leader; the derived mistermv row is not a streamer and is left out.
STREAMER_VAR_DETAIL = query_var(
    "streamer", "Streamer",
    "SELECT st.display AS __text, st.twitch_id AS __value FROM streamer_v st "
    "JOIN streamer_sample_v s USING (twitch_id) WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived "
    "ORDER BY s.donation_total DESC",
    include_all=False,
)

LOCATION_VAR = query_var("location", "Location", LOC_QUERY, all_value=".*")
# Same Location filter, but "On site (LAN)" selected by default (live dashboard).
LOCATION_VAR_LAN = copy.deepcopy(LOCATION_VAR)
LOCATION_VAR_LAN["current"] = {"selected": True, "text": ["On site (LAN)"], "value": ["LAN"]}

# First datapoint until now; grows as data arrives. Data before 2026-09-04 20:58 UTC is backfilled from
# third-party sources (raw-backfill/, see zevent_tracker/external.py); its first tick is 17:01 UTC.
TIME_RANGE = {"from": "2026-09-03T17:01:00.000Z", "to": "now"}

DASHBOARDS = [  # (uid, button title)
    ("zevent-public", "Main stats"),
    ("zevent-live-public", "Live timeline"),
    ("zevent-insights-public", "Insights"),
    ("zevent-streamer-public", "Streamer"),
]


# Buttons in the controls bar (also shown in kiosk mode) to jump to the other dashboards. The explicit
# "?kiosk=1" keeps the chrome hidden after the jump; keepTime carries the selected time range over.
def links_from(uid):
    return [
        {"type": "link", "title": title, "url": f"/d/{other}?kiosk=1", "icon": "dashboard",
         "keepTime": True, "includeVars": False, "targetBlank": False, "asDropdown": False, "tags": [],
         "tooltip": ""}
        for other, title in DASHBOARDS if other != uid
    ]


def dashboard_base(uid, title, variables, panels_):
    return {
        "uid": uid,
        "title": title,
        "tags": ["zevent"],
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,
        "refresh": "30s",
        # hidden time picker also hides the refresh picker; from/to stay fixed at the values below
        "timepicker": {"hidden": False, "refresh_intervals": ["15s", "30s"]},
        "time": TIME_RANGE,
        "schemaVersion": 39,
        "version": 1,
        "templating": {"list": copy.deepcopy(variables)},
        "links": links_from(uid),
        "panels": panels_,
    }


# ---------------------------------------------------------------------------------------------------
# ZEVENT live. One row per streamer, a green bar while they are live, the game on hover. The query returns
# only the samples where a streamer's state changed (plus the last sample of each, so the final bar reaches
# the latest data) instead of every minute for every streamer, which keeps it at a few thousand rows.
# The state timeline holds the last value until the end of the time range, so each streamer also gets
# a NULL terminator one poll interval after their last sample: bars end where the data ends, not at
# "now", and a stalled collector does not show everyone as live.
# The partitionByValues transformation splits the table into one frame per streamer; the state
# timeline draws each frame as a row. Rows are sorted by first time live in the selected range,
# streamers never live in the range last. The sort column first_live is dropped by the organize
# transformation so it does not become a second row per streamer.
LIVE_SQL = """
WITH s AS (
  SELECT s.ts, s.twitch_id, st.display, CASE WHEN s.online THEN coalesce(s.game, '(no game)') END AS state
  FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)
  WHERE $__timeFilter(s.ts) AND NOT st.derived AND {loc}{filter}
), d AS (
  SELECT *, lag(state) OVER w AS prev, lead(ts) OVER w AS next,
         min(ts) FILTER (WHERE state IS NOT NULL) OVER (PARTITION BY twitch_id) AS first_live
  FROM s WINDOW w AS (PARTITION BY twitch_id ORDER BY ts)
), r AS (
  SELECT ts, display, state, first_live FROM d WHERE state IS DISTINCT FROM prev OR next IS NULL
  UNION ALL
  SELECT max(ts) + interval '1 minute', display, NULL, min(first_live) FROM d GROUP BY twitch_id, display
)
SELECT ts AS time, display AS "Streamer", state AS "State", first_live
FROM r ORDER BY first_live NULLS LAST, lower(display), ts
"""

LIVE_DESCRIPTION = (
    "Green while live. Hover a bar for the game. Rows are sorted by the first time the streamer went live in "
    "the selected range; streamers that were not live in the range are at the bottom."
)


def live_timeline(title, y, h, loc, filtered, description, per_page):
    sql = LIVE_SQL.format(loc=loc, filter=" AND s.twitch_id IN ($streamer)" if filtered else "")
    return panel(
        "state-timeline", title, sql, 0, y, 24, h, fmt="table",
        fieldConfig={"defaults": {
            "custom": {"lineWidth": 0, "fillOpacity": 85, "spanNulls": False, "insertNulls": False},
            "color": {"mode": "fixed", "fixedColor": "green"},
            "mappings": [{"type": "regex", "options": {"pattern": ".+", "result": {"color": "green", "index": 0}}}],
            "displayName": "${__field.labels.Streamer}",
        }},
        options={
            # perPage: the list is paginated so the panel height does not depend on the number of streamers
            "mergeValues": True, "showValue": "never", "alignValue": "left", "rowHeight": 0.8, "perPage": per_page,
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        transformations=[
            {"id": "organize", "options": {"excludeByName": {"first_live": True}}},
            {"id": "partitionByValues",
             "options": {"fields": ["Streamer"], "keepFields": False, "naming": {"asLabels": True}}},
        ],
        description=description,
    )


def live_panels(loc, scope):
    """Panels of the live dashboard. `loc` is the SQL location filter (st = streamer_v), `scope` a label."""
    reset_ids()
    latest = "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE NOT st.derived AND " + loc
    return [
        stat(f"Streamers online ({scope})",
             f'SELECT count(*) FILTER (WHERE s.online) AS "Online", count(*) AS "Total" {latest} '
             "AND s.ts = (SELECT max(ts) FROM snapshot)",
             0, w=4, color="blue", text_mode="value_and_name"),
        stat(f"Peak streamers online ({scope})",
             f"SELECT max(n) FROM (SELECT count(*) AS n {latest} AND s.online GROUP BY s.ts) x",
             4, w=4, unit="short", color="blue"),
        stat("Hours streamed", hours_streamed_sql(loc), 8, w=4, unit="suffix: h", decimals=1, color="green",
             description=HOURS_DESCRIPTION),
        ts(f"Streamers online over time ({scope})",
           f'SELECT s.ts AS time, count(*) FILTER (WHERE s.online) AS "Online" {latest} AND $__timeFilter(s.ts) '
           "GROUP BY 1 ORDER BY 1",
           12, 0, w=12, h=4, unit="short", legend=False),
        # paginated, follows the Location and Streamer filters
        live_timeline(f"Streamers ({scope}, $streamer)", 4, 34, loc, filtered=True, per_page=50,
                      description=LIVE_DESCRIPTION + " Follows the Location and Streamer filters."),
    ]


# ---------------------------------------------------------------------------------------------------
here = Path(__file__).parent


def write(dash):
    out = here / "provisioning" / "dashboards" / f"{dash['uid']}.json"
    out.write_text(json.dumps(dash, indent=2) + "\n")
    print(f"wrote {out} ({len(dash['panels'])} panels)")


write(dashboard_base("zevent-public", "ZEVENT", [LOCATION_VAR, streamer_var(LOC)], main_panels()))
write(dashboard_base("zevent-live-public", "ZEVENT live", [LOCATION_VAR_LAN, streamer_var(LOC)],
                     live_panels(LOC, "$location")))
write(dashboard_base("zevent-insights-public", "ZEVENT insights", [LOCATION_VAR], insights_panels()))
write(dashboard_base("zevent-streamer-public", "ZEVENT streamer", [STREAMER_VAR_DETAIL], streamer_panels()))
