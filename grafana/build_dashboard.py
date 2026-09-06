"""Generate the Grafana dashboards. Run: uv run python grafana/build_dashboard.py

All are served by the single anonymous, read-only Grafana instance (fixed time range, no refresh: the
event is over and every "latest" value is frozen at its end, see freeze_sql), and
each has buttons to the others:
  zevent-public.json           ZEVENT: headline tiles, global graphs, leaderboards. Location + Streamer filters
  zevent-live-public.json      ZEVENT live: one row per streamer, green while live; Location defaults to on site
  zevent-insights-public.json  ZEVENT insights: milestones and pace, notable moments, patterns, on site vs
                               remote, games, donations not tied to a streamer. Location filter
  zevent-streamer-public.json  ZEVENT streamer: one or a few streamers in detail; opens on the current leader
  zevent-viewers-public.json   ZEVENT viewers: the viewers of every streamer stacked over time
The "-public" uid suffix is kept because the URLs are published (the proxy redirects / to /d/zevent-public).
"""
import copy
import json
from pathlib import Path

DS = {"type": "grafana-postgresql-datasource", "uid": "zevent-pg"}
_id = 0

# First datapoint to the end of the event. Data before 2026-09-04 20:58 UTC is backfilled from third-party
# sources (raw-backfill/, see zevent_tracker/external.py); its first tick is 17:01 UTC. The event total last
# moved at 23:11 UTC on 2026-09-06 (01:11 Paris time), so the range ends one minute after that.
END_TS = "2026-09-06T23:12:00.000Z"
TIME_RANGE = {"from": "2026-09-03T17:01:00.000Z", "to": END_TS}
END = f"'{END_TS}'::timestamptz"

# The dashboards are in their after-the-event state: every query that reads "the latest snapshot" is
# frozen at the end of the event by freeze_sql() (applied to each panel and variable query when a dashboard
# is built), so a collector still polling the frozen API cannot move the numbers (viewers dropped to a
# few thousand after the end while the total stayed put).
FROZEN = {
    "(SELECT max(ts) FROM snapshot)": f"(SELECT max(ts) FROM snapshot WHERE ts <= {END})",
    "FROM snapshot ORDER BY ts DESC LIMIT 1": f"FROM snapshot WHERE ts <= {END} ORDER BY ts DESC LIMIT 1",
}


def freeze_sql(sql):
    for a, b in FROZEN.items():
        sql = sql.replace(a, b)
    return sql


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


def stat(title, sql, x, w=6, y=0, unit=None, decimals=None, color="green", text_mode="value", description=None,
         thresholds=None, transformations=None, no_value=None, h=4, mappings=None, text_field=False):
    """`thresholds` colours the value by steps [(from_value_or_None, colour), ...] instead of a fixed colour;
    `mappings` is a list of Grafana value mappings (e.g. a range shown as text)."""
    d = {"color": {"mode": "fixed", "fixedColor": color}}
    if mappings:
        d["mappings"] = mappings
    if thresholds:
        d["color"] = {"mode": "thresholds"}
        d["thresholds"] = {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in thresholds]}
    extra = {"description": description} if description else {}
    if transformations:
        extra["transformations"] = transformations
    if unit:
        d["unit"] = unit
    if decimals is not None:
        d["decimals"] = decimals
    if no_value:
        d["noValue"] = no_value
    return panel(
        "stat", title, sql, x, y, w, h, fmt="table",
        fieldConfig={"defaults": d},
        options={
            # fields "" = numeric fields only; a stat showing a text column needs "/.*/"
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if text_field else "", "values": False},
            "colorMode": "value", "graphMode": "none", "textMode": text_mode, "justifyMode": "center",
        },
        **extra,
    )


def ts(title, sql, x, y, w=12, h=9, unit=None, bars=False, legend=True, stack=False, min_interval=None,
       streamer_links=False, description=None, overrides=()):
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
        fieldConfig={"defaults": d, "overrides": list(overrides)},
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
    return {"title": "Ouvrir sur Twitch", "url": "https://twitch.tv/${" + var + "}", "targetBlank": True}


def table(title, sql, x, y, w=8, h=12, money_cols=(), duration_cols=(), hour_cols=(), image_cols=(), percent_cols=(),
          streamer_links=False, description=None, delta_cols=()):
    overrides = [
                    # signed movement: green when positive, red when negative
                    {"matcher": {"id": "byName", "options": c},
                     "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                    {"id": "color", "value": {"mode": "thresholds"}},
                                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                        {"color": "red", "value": None}, {"color": "text", "value": 0},
                                        {"color": "green", "value": 1}]}}]}
                    for c in delta_cols
                ] + [
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


# Gain since the previous sample of the event total (snapshot table, a few thousand rows): plain
# difference, so a range total equals last minus first. Upstream blips (a counter dropping to 0 and
# being restored minutes later, seen on 2026-09-04 at 21:28 UTC) then show as a symmetric -X/+X pair
# that cancels out, and real refunds stay negative. Per-streamer gains are NOT computed this way any
# more: the collector stores them in streamer_sample.gain (see zevent_tracker/db.py), and the views
# expose them as streamer_sample_v.gain, so the dashboards sum a column instead of running a window
# function over the whole history.
def gain_expr(col):
    return f"{col} - lag({col}) OVER (ORDER BY ts)"


# Panels read the streamer_v / streamer_sample_v views (db/views.sql), not the raw tables. The views
# split mistermv's counter into his own donations and a derived "mistermv (compteur privé)" row
# holding the part that mirrors Domingo's counter (see the file header for the full story). Rows
# with derived = true are not API entities and must be left out of sums compared to the global total.
MIRROR_NOTE = (
    "Tous les dons qui n'ont pas pu être rattachés à un streamer : le total de l'événement moins la somme des "
    "compteurs des streamers. On y trouve les dons faits sans choisir de streamer (la page d'équipe Streamlabs "
    "Charity sans membre), les billets du concert de jeudi, tous les dons de la boutique, etc."
)


# Location filter of the `location` dashboard variable: "LAN" (on site) or "Online" (streaming from
# home). Matched as a regex so "All" (allValue ".*") also keeps streamers whose location is unknown,
# e.g. one that left the API list before the column existed. `st` is the streamer_v alias.
LOC = "coalesce(st.location, '') ~ '^(${location:regex})$'"
LOC_QUERY = ("SELECT CASE location WHEN 'LAN' THEN 'Sur place (LAN)' ELSE 'À distance (online)' END AS __text, "
             "location AS __value FROM streamer_v WHERE location IS NOT NULL GROUP BY location ORDER BY location")


# Hours live = sum over live samples of the time since the streamer's previous sample (stored as
# gap_s), capped at 5 minutes so a collector outage does not count as streaming. Follows the time
# range and both filters. `loc` is the SQL location filter (st = streamer_v).
LIVE_SECONDS = "least(s.gap_s, 300)"


def hours_streamed_sql(loc, with_game=None, live="s.online"):
    """One column (the stat's value); with `with_game` two named columns, the total and the part spent in that
    Twitch category, for a value_and_name stat. `live` is the SQL condition for a sample that counts."""
    cols = f"coalesce(sum({LIVE_SECONDS}) FILTER (WHERE {live}), 0) / 3600.0"
    if with_game:
        cols = (f'{cols} AS "Total", coalesce(sum({LIVE_SECONDS}) FILTER (WHERE s.online AND s.game = \'{with_game}\'), 0) '
                f'/ 3600.0 AS "Dont catégorie {with_game}"')
    return (
        f"SELECT {cols} "
        "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
        f"WHERE NOT st.derived AND {loc} AND $__timeFilter(s.ts) AND s.twitch_id IN ($streamer) AND s.gap_s IS NOT NULL"
    )


HOURS_DESCRIPTION = (
    "Temps total en live, additionné sur les streamers correspondant aux filtres Lieu et Streamer, dans la "
    "plage de temps sélectionnée. Chaque relevé en live compte la minute écoulée depuis le relevé précédent "
    "(plafonnée à 5 minutes, pour que les trous dans les données ne comptent pas)."
)


# Same rule per streamer, as a CTE `h(twitch_id, hours)` for the leaderboards (selected time range).
HOURS_CTE = (
    "WITH h AS ("
    "  SELECT s.twitch_id, sum(least(s.gap_s, 300)) FILTER (WHERE s.online) / 3600.0 AS hours"
    "  FROM streamer_sample_v s WHERE $__timeFilter(s.ts) AND s.gap_s IS NOT NULL GROUP BY s.twitch_id"
    ") "
)


# Per-streamer gain within the selected range, as a CTE `g(twitch_id, gained)`.
GAIN_CTE = (
    "g AS ("
    "  SELECT s.twitch_id, sum(s.gain) AS gained FROM streamer_sample_v s"
    "  WHERE $__timeFilter(s.ts) AND s.gain IS NOT NULL GROUP BY s.twitch_id"
    ") "
)


# Seconds a sample stands for (since the previous one), capped so gaps in the data do not count; used
# for viewer-hours. Rows are aliased x in the queries below.
DT = "least(x.gap_s, 300)"


PER_VIEWER_HOUR_DESCRIPTION = (
    "Ce que la communauté donne par rapport à sa taille : les dons gagnés par les streamers correspondant aux "
    "filtres Lieu et Streamer sur la période sélectionnée, divisés par leurs heures visionnées par viewer (le nombre de viewers "
    "additionné minute par minute sur la même période, en heures). Un viewer qui regarde pendant une heure vaut "
    "une heure visionnée par un viewer : 5 \u20ac signifie 5 \u20ac récoltés pour chaque heure regardée par un viewer. Les dons sans "
    "streamer (billets, boutique, anonymes) ne sont pas comptés."
)


# Gains of the matching streamers over the range divided by their viewer-hours. `loc` is the SQL location
# filter (st = streamer_v); "true" when the dashboard has no Location filter.
def per_viewer_hour_sql(loc):
    return (
        "WITH " + GAIN_CTE + ", v AS ("
        f"  SELECT sum(x.viewers * {DT}) / 3600.0 AS viewer_hours"
        "  FROM streamer_sample_v x JOIN streamer_v st USING (twitch_id)"
        f"  WHERE $__timeFilter(x.ts) AND NOT st.derived AND x.twitch_id IN ($streamer) AND {loc} AND x.gap_s IS NOT NULL"
        ") "
        "SELECT (SELECT coalesce(sum(g.gained), 0) FROM g JOIN streamer_v st USING (twitch_id) "
        f"        WHERE NOT st.derived AND g.twitch_id IN ($streamer) AND {loc}) / nullif(v.viewer_hours, 0) FROM v"
    )


VIEWER_HOURS_DESCRIPTION = (
    "La taille de l'audience dans le temps, en heures visionnées par viewer : le nombre de viewers des streamers correspondant "
    "aux filtres Lieu et Streamer, additionné minute par minute sur la période sélectionnée (chaque relevé compte "
    "le temps écoulé depuis le précédent, plafonné à 5 minutes pour que les trous dans les données ne comptent "
    "pas). Un viewer qui regarde pendant une heure vaut une heure visionnée par un viewer ; 1 000 viewers pendant 2 heures font "
    "2 000 heures visionnées par viewer."
)


# Viewer-hours of the matching streamers over the range (the denominator of "Donations per viewer-hour").
def viewer_hours_sql(loc):
    return (
        f"SELECT coalesce(sum(x.viewers * {DT}), 0) / 3600.0 "
        "FROM streamer_sample_v x JOIN streamer_v st USING (twitch_id) "
        f"WHERE $__timeFilter(x.ts) AND NOT st.derived AND x.twitch_id IN ($streamer) AND {loc} AND x.gap_s IS NOT NULL"
    )


def hours_stat(x, w, loc, y=4):
    return stat("Heures de stream", hours_streamed_sql(loc), x, w=w, y=y, unit="suffix: h", decimals=1,
                color="green", description=HOURS_DESCRIPTION)


# Leaderboards share one shape: avatar, Streamer (linked to Twitch), Donations at the latest snapshot (the
# end of the event), peak viewers and Hours live within the selected range (same rule as the "Hours
# streamed" stat). `extra_col` goes right after the name (the "Gained" column of the top-gained board) and
# needs its CTE and join.
def leaderboard(title, x, y, order_by, where="true", extra_cte="", extra_col="", extra_join="", money_cols=()):
    sql = (
        HOURS_CTE +
        ", cur AS ("
        "  SELECT twitch_id, donation_total, viewers, online FROM streamer_sample_v"
        "  WHERE ts = (SELECT max(ts) FROM snapshot)"
        "), pk AS ("
        "  SELECT twitch_id, max(viewers) AS peak FROM streamer_sample_v WHERE $__timeFilter(ts) GROUP BY twitch_id"
        ") " + extra_cte +
        f'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", {extra_col}cur.donation_total AS "Cagnotte", '
        'coalesce(pk.peak, 0) AS "Pic de viewers", coalesce(h.hours, 0) AS "Heures de stream", st.login AS login '
        "FROM cur JOIN streamer_v st USING (twitch_id) LEFT JOIN h USING (twitch_id) LEFT JOIN pk USING (twitch_id) " + extra_join +
        f"WHERE {where} AND " + LOC + f" ORDER BY {order_by} LIMIT 25"
    )
    return table(title, sql, x, y, w=12, money_cols=money_cols + ("Cagnotte",), hour_cols=("Heures de stream",),
                 image_cols=("Avatar",), streamer_links=True)


def row(title, y):
    global _id
    _id += 1
    return {"id": _id, "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def reset_ids():
    global _id
    _id = 0



def barchart(title, sql, x, y, w=12, h=9, x_field="", unit=None, description=None, series_colors=None,
             stacking="none", orientation="auto"):
    """One bar per row of the table result. `series_colors` maps value column names to colours: with two or
    more value columns the bars are drawn per series, side by side, with a legend (`stacking` "normal" stacks them)."""
    d = {"custom": {"fillOpacity": 80, "lineWidth": 1}, "color": {"mode": "fixed", "fixedColor": "green"}}
    if unit:
        d["unit"] = unit
    extra = {"description": description} if description else {}
    multi = bool(series_colors)
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
                 for name, color in (series_colors or {}).items()]
    if x_field:
        # the panel-wide unit would also format numeric-looking labels of the x field ("2016" -> "€2.02k")
        overrides.append({"matcher": {"id": "byName", "options": x_field},
                          "properties": [{"id": "unit", "value": "string"}]})
    return panel(
        "barchart", title, sql, x, y, w, h, fmt="table",
        fieldConfig={"defaults": d, "overrides": overrides},
        options={"orientation": orientation, "xField": x_field, "showValue": "never", "barWidth": 0.8, "stacking": stacking,
                 "legend": {"showLegend": multi, "displayMode": "list", "placement": "bottom"},
                 "tooltip": {"mode": "multi" if multi else "single", "sort": "none"}},
        **extra,
    )


def xychart(title, sql, x, y, w=12, h=9, x_col="", y_col="", y_unit=None, description=None):
    """Scatter plot, one point per row, both axes on a log scale; `x_col` on the x axis, every other numeric
    column on the y axis, string columns in the tooltip. Option names are those of the xychart panel
    shipped with Grafana 11.3 (seriesMapping/dims), not the newer mapping/series schema."""
    overrides = []
    if y_unit:
        overrides.append({"matcher": {"id": "byName", "options": y_col}, "properties": [{"id": "unit", "value": y_unit}]})
    custom = {"show": "points", "pointSize": {"fixed": 7}, "axisPlacement": "auto",
              "scaleDistribution": {"type": "log", "log": 10}}
    return panel(
        "xychart", title, sql, x, y, w, h, fmt="table",
        fieldConfig={"defaults": {"custom": custom, "color": {"mode": "fixed", "fixedColor": "green"}},
                     "overrides": overrides},
        options={
            "seriesMapping": "auto",
            "dims": {"frame": 0, "x": x_col, "exclude": []},
            "series": [],
            "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        **({"description": description} if description else {}),
    )


# Gain of the event total over a set of snapshot rows: last minus first (max minus min would count an
# upstream blip where the counter drops and is restored).
RANGE_GAIN = "(array_agg(donation_total ORDER BY ts DESC))[1] - (array_agg(donation_total ORDER BY ts))[1]"

# End of the event (the channels' marathon closes Monday 01:00 CEST, 23:00 UTC Sunday). Only used by the
# "projected total" tiles; after this instant they simply show the current total.
EVENT_END = "2026-09-06 23:00:00+00"


# One-minute jumps of the event total at or above this are one-off payments (the shop: 3.7M in one minute on
# 2026-09-05 20:04 UTC), not a pace that will continue; the projections leave those minutes out. Organic peak
# minutes stay well below (p99.9 of 2,300 minutes was 68K; two evening minutes of ~100K are the borderline).
BIG_DONATION = 300000


def projection_sql(hours):
    """Current total plus the pace of the last `hours` hours held until EVENT_END. The pace is the sum of the
    per-minute gains of the window, minutes with a jump of BIG_DONATION or more left out, over the time those
    minutes cover (gaps capped at 5 minutes, like everywhere else)."""
    return (
        "WITH cur AS (SELECT ts, donation_total FROM snapshot ORDER BY ts DESC LIMIT 1), "
        "d AS (SELECT ts, donation_total - lag(donation_total) OVER (ORDER BY ts) AS delta, "
        "             extract(epoch FROM ts - lag(ts) OVER (ORDER BY ts)) AS dt "
        f"      FROM snapshot WHERE ts >= (SELECT ts FROM cur) - interval '{hours} hours' - interval '5 minutes'), "
        "pace AS (SELECT sum(d.delta) / nullif(sum(d.dt), 0) AS per_s FROM d, cur "
        f"         WHERE d.ts > cur.ts - interval '{hours} hours' AND d.delta IS NOT NULL AND d.delta < {BIG_DONATION} AND d.dt <= 300) "
        "SELECT cur.donation_total + coalesce(pace.per_s, 0) "
        f"* greatest(extract(epoch FROM '{EVENT_END}'::timestamptz - cur.ts), 0) FROM cur, pace"
    )


# Previous editions: (year, name, final total in EUR). No edition in 2023.
EDITIONS = [
    (2016, "Avenger Project", 170770), (2017, "ZEvent", 451851), (2018, "ZEvent", 1094731), (2019, "ZEvent", 3510682),
    (2020, "ZEvent", 5724377), (2021, "ZEvent", 10064480), (2022, "ZEvent", 10182126), (2024, "ZEvent", 10145881),
    (2025, "ZEvent", 16658660),
]
RECORD_YEAR, RECORD = 2025, 16658660
EDITIONS_VALUES = "(VALUES " + ", ".join(f"({y}, '{n}', {t})" for y, n, t in EDITIONS) + ") e(year, name, total)"


def edition_label(year, total):
    return f"{year} : {total / 1e6:.2f} M€".replace(".", ",")


# Flat reference lines of the editions from 2019 on (the earlier ones sit on the x axis of a chart in the
# tens of millions), as extra columns of the total chart's query.
EDITION_LINES_SQL = ", ".join(f'{t} AS "{edition_label(y, t)}"' for y, n, t in EDITIONS if y >= 2019)
EDITION_LINES_OVERRIDE = {
    "matcher": {"id": "byRegexp", "options": "^20[0-9][0-9] :.*"},
    "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 6]}},
                   {"id": "custom.lineWidth", "value": 1}, {"id": "custom.fillOpacity", "value": 0},
                   {"id": "custom.showPoints", "value": "never"},
                   {"id": "color", "value": {"mode": "fixed", "fixedColor": "#8b949e"}}],
}


# French money formatting in SQL for text stats ("4 212 345 €"); Grafana's currency unit abbreviates.
def eur_text(expr):
    return f"replace(replace(to_char({expr}, 'FM999,999,999'), ',', ' '), '.', ',') || ' €'"


PARIS_HOUR = "to_char(h, 'DD/MM \"à\" HH24\"h\"')"


LOC_LABEL = "CASE st.location WHEN 'LAN' THEN 'Sur place (LAN)' WHEN 'Online' THEN 'À distance (online)' ELSE 'Inconnu' END"
ORG = "st.login NOT IN ('zevent', 'zeventplays')"


# ---------------------------------------------------------------------------------------------------
# ZEVENT (main): headline tiles, the global graphs and the leaderboards. Everything else lives in the
# insights and streamer dashboards.
def main_panels():
    reset_ids()
    return [
        # replaced "Dons, dernière heure" once the event ended: the total at the end of the fixed time range,
        # frozen whatever the API does afterwards
        # formatted in SQL ("32 891 874,15 €"): Grafana's currency unit would abbreviate it to €32.89M
        stat("Total final de l'événement",
             "SELECT replace(replace(to_char(donation_total, 'FM999,999,999.00'), ',', ' '), '.', ',') || ' €' "
             f"FROM snapshot WHERE ts <= '{TIME_RANGE['to']}'::timestamptz ORDER BY ts DESC LIMIT 1", 0,
             w=8, color="green", text_field=True,
             description="Total de l'événement à la fin de l'événement avant la fin des dons, le lundi 7 septembre à 01h11 "
                         "(heure de Paris)."),
        stat("Dons sans streamer",
             "SELECT sn.donation_total - sum(s.donation_total) FROM snapshot sn JOIN streamer_sample_v s USING (ts) "
             "WHERE NOT s.derived AND sn.ts = (SELECT max(ts) FROM snapshot) GROUP BY sn.donation_total",
             8, w=8, unit="currencyEUR", decimals=2, color="yellow", description=MIRROR_NOTE),
        # replaced the "Cagnotte spéciale du Vieux Monsieur" tile on 2026-09-06 once the API corrected
        # mistermv's counter (db/views.sql): the derived row no longer exists at the latest snapshot.
        # the record itself fell during the night of the 6th; the target everyone talks about is twice 2025
        stat(f"Il manque pour doubler {RECORD_YEAR}",
             f"SELECT {2 * RECORD} - donation_total FROM snapshot ORDER BY ts DESC LIMIT 1",
             16, w=8, unit="currencyEUR", decimals=2, thresholds=[(None, "green"), (1, "red")],
             mappings=[{"type": "range", "options": {"from": -1e12, "to": 0, "result": {"text": "Objectif atteint !", "color": "green"}}}],
             description=f"Écart entre le total actuel et le double du total de {RECORD_YEAR}, soit {2 * RECORD:,} €.".replace(f"{2 * RECORD:,}", f"{2 * RECORD:,}".replace(",", " "))),
        # Second and third tile rows: audience and rate tiles, three per row. Viewers of the streamers
        # matching the Location and Streamer filters (sum of the per-streamer counts, which equals the API's
        # global viewer count).
        stat("Viewers moyens",
             "SELECT coalesce(avg(v), 0) FROM ("
             "  SELECT s.ts, sum(s.viewers) AS v FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)"
             "  WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC +
             "  GROUP BY s.ts"
             ") x",
             0, w=8, y=4, unit="sishort", color="purple",
             description="Viewers cumulés des streamers correspondant aux filtres Lieu et Streamer, en moyenne sur les "
                         "relevés de la plage de temps sélectionnée."),
        stat("Pic de viewers",
             "SELECT coalesce(max(v), 0) FROM ("
             "  SELECT s.ts, sum(s.viewers) AS v FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)"
             "  WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC +
             "  GROUP BY s.ts"
             ") x",
             8, w=8, y=4, unit="sishort", color="purple",
             description="Plus grand nombre de viewers cumulés des streamers correspondant aux filtres Lieu et Streamer, "
                         "dans la plage de temps sélectionnée."),
        stat("Streamers",
             'SELECT count(DISTINCT s.twitch_id) FILTER (WHERE s.online) AS "Passés en live", count(DISTINCT s.twitch_id) AS "Inscrits" '
             "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC,
             0, w=8, y=8, color="blue", text_mode="value_and_name",
             description="Streamers correspondant aux filtres Lieu et Streamer : ceux passés en live au moins une fois "
                         "dans la plage de temps sélectionnée, et tous ceux listés par l'API."),
        stat("Heures visionnées", viewer_hours_sql(LOC), 16, w=8, y=4, unit="sishort", decimals=1, color="purple",
             description=VIEWER_HOURS_DESCRIPTION),
        hours_stat(8, 8, LOC, y=8),
        stat("Dons par heure visionnée par viewer", per_viewer_hour_sql(LOC), 16, w=8, y=8, unit="currencyEUR", decimals=2,
             color="green", description=PER_VIEWER_HOUR_DESCRIPTION),

        row("Global", 12),
        ts("Total des dons au fil du temps",
           f'SELECT ts AS time, donation_total AS "Total", {EDITION_LINES_SQL} FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
           0, 13, unit="currencyEUR", overrides=[EDITION_LINES_OVERRIDE],
           description="En pointillés, le total final des éditions précédentes (depuis 2019)."),
        ts("Viewers au fil du temps",
           'SELECT ts AS time, viewers_total AS "Viewers" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
           12, 13, unit="sishort", legend=False),
        ts("Dons par intervalle",
           'SELECT $__timeGroupAlias(ts, $__interval), sum(delta) AS "Dons" FROM ('
           '  SELECT ts, donation_total - lag(donation_total) OVER (ORDER BY ts) AS delta'
           '  FROM snapshot WHERE $__timeFilter(ts)'
           ') d WHERE delta IS NOT NULL GROUP BY 1 ORDER BY 1',
           0, 22, unit="currencyEUR", bars=True, legend=False, min_interval="5m"),
        ts("Streamers en live",
           'SELECT ts AS time, streamers_online AS "En live" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
           12, 22, unit="sishort", legend=False),

        row("Classements (fin de l'événement, $location)", 31),
        # 2x2 grid
        leaderboard("Top par dons", 0, 32, order_by="cur.donation_total DESC"),
        leaderboard("Top par heures visionnées", 12, 32, order_by="v.vh DESC",
                    extra_cte=", v AS (SELECT x.twitch_id, sum(x.viewers * least(x.gap_s, 300)) / 3600.0 AS vh "
                              "        FROM streamer_sample_v x WHERE $__timeFilter(x.ts) AND x.gap_s IS NOT NULL GROUP BY x.twitch_id) ",
                    extra_col='round(v.vh) AS "Heures visionnées", ', extra_join="JOIN v USING (twitch_id) "),
        leaderboard("Top des gains sur la période", 0, 44, order_by="g.gained DESC",
                    extra_cte=", " + GAIN_CTE, extra_col='g.gained AS "Gagné", ',
                    extra_join="JOIN g USING (twitch_id) ", money_cols=("Gagné",)),
        leaderboard("Top des heures de stream sur la période", 12, 44,
                    order_by="coalesce(h.hours, 0) DESC, cur.donation_total DESC"),
        table("Top des pics de viewers sur la période",
              "WITH pk AS ("
              "  SELECT s.twitch_id, max(s.viewers) AS peak FROM streamer_sample_v s"
              "  WHERE $__timeFilter(s.ts) AND NOT s.derived AND s.twitch_id IN ($streamer) GROUP BY s.twitch_id"
              "), cur AS ("
              "  SELECT twitch_id, viewers, donation_total FROM streamer_sample_v WHERE ts = (SELECT max(ts) FROM snapshot)"
              "), top AS ("
              "  SELECT pk.twitch_id, pk.peak FROM pk JOIN streamer_v st USING (twitch_id) WHERE pk.peak > 0 AND " + LOC +
              "  ORDER BY pk.peak DESC LIMIT 25"
              ") "
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", top.peak AS "Pic de viewers", '
              "       (SELECT min(x.ts) FROM streamer_sample_v x WHERE x.twitch_id = top.twitch_id AND x.viewers = top.peak "
              '        AND $__timeFilter(x.ts)) AS "Atteint à", '
              'cur.viewers AS "Viewers", cur.donation_total AS "Cagnotte", st.login AS login '
              "FROM top JOIN streamer_v st USING (twitch_id) LEFT JOIN cur USING (twitch_id) ORDER BY top.peak DESC",
              0, 56, w=12, money_cols=("Cagnotte",), image_cols=("Avatar",), streamer_links=True,
              description="Plus grand nombre de viewers atteint par chaque streamer dans la plage de temps sélectionnée, "
                          "et le moment où il l'a atteint. Suit les filtres Lieu et Streamer."),
        table("Top des viewers moyens en live sur la période",
              "WITH v AS ("
              f"  SELECT x.twitch_id, sum(x.viewers * {DT}) / 3600.0 AS viewer_hours, sum({DT}) FILTER (WHERE x.online) / 3600.0 AS hours"
              "  FROM streamer_sample_v x WHERE $__timeFilter(x.ts) AND NOT x.derived AND x.twitch_id IN ($streamer) AND x.gap_s IS NOT NULL"
              "  GROUP BY x.twitch_id"
              "), cur AS ("
              "  SELECT twitch_id, viewers, donation_total FROM streamer_sample_v WHERE ts = (SELECT max(ts) FROM snapshot)"
              ") "
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", round(v.viewer_hours / v.hours) AS "Viewers moyens", '
              'v.hours AS "Heures de stream", cur.viewers AS "Viewers", cur.donation_total AS "Cagnotte", st.login AS login '
              "FROM v JOIN streamer_v st USING (twitch_id) LEFT JOIN cur USING (twitch_id) "
              "WHERE v.hours >= 1 AND " + LOC + " ORDER BY 3 DESC LIMIT 25",
              12, 56, w=12, money_cols=("Cagnotte",), hour_cols=("Heures de stream",), image_cols=("Avatar",), streamer_links=True,
              description="Heures visionnées divisées par les heures en live sur la plage de temps sélectionnée : l'audience "
                          "habituelle du streamer quand il est en live. Streamers avec au moins une heure de live. "
                          "Suit les filtres Lieu et Streamer."),
    ]


# ---------------------------------------------------------------------------------------------------
# ZEVENT insights: milestones and pace, notable moments, patterns, on site vs remote, games, and the
# donations that cannot be tied to a streamer. Location filter only.
BLIP_NOTE = (
    "Gains du compteur d'un streamer en une minute. Les chaînes de l'organisation (ZEVENT, ZEventPlays) sont "
    "exclues : leurs compteurs bougent par paquets (billets, boutique) et, avant le 4 sept. 22:58 CEST, viennent "
    "d'une source relevée toutes les 30 minutes. Les gains sont mesurés entre deux relevés espacés d'au plus "
    "5 minutes, pour qu'un trou dans les données ne compte pas comme une minute. Le retour à la normale après un "
    "raté de l'API (un compteur qui tombe à 0 et revient quelques minutes plus tard) est exclu : les lignes dont "
    "le streamer a perdu au moins 90 % du montant dans les 10 minutes précédentes sont ignorées."
)


def insights_panels():
    reset_ids()
    by_loc = ("FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
              "WHERE $__timeFilter(s.ts) AND NOT st.derived AND " + LOC)
    return [
        # After the event: the pace, projection and "next million" tiles are replaced by the event's records.
        stat("Rythme moyen sur l'événement",
             f"SELECT ({RANGE_GAIN}) / greatest(extract(epoch FROM max(ts) - min(ts)) / 60, 1) "
             f"FROM snapshot WHERE ts <= {END}",
             0, w=6, unit="currencyEUR", decimals=2, color="orange",
             description="Total de l'événement divisé par sa durée, en euros par minute."),
        stat("Meilleure heure",
             f"SELECT {PARIS_HOUR} || ' : ' || {eur_text('g')} FROM ("
             "  SELECT date_trunc('hour', ts AT TIME ZONE 'Europe/Paris') AS h, sum(delta) AS g FROM ("
             f"    SELECT ts, {gain_expr('donation_total')} AS delta FROM snapshot WHERE ts <= {END}"
             "  ) d WHERE delta IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
             ") x",
             6, w=6, color="orange", text_field=True,
             description="L'heure de la journée (heure de Paris) où le total de l'événement a le plus progressé."),
        stat("Plus grosse minute",
             f"SELECT to_char(ts AT TIME ZONE 'Europe/Paris', 'DD/MM \"à\" HH24\"h\"MI') || ' : ' || {eur_text('delta')} FROM ("
             f"  SELECT ts, {gain_expr('donation_total')} AS delta FROM snapshot WHERE ts <= {END}"
             ") d WHERE delta IS NOT NULL ORDER BY delta DESC LIMIT 1",
             12, w=6, color="orange", text_field=True,
             description="La minute où le total de l'événement a le plus progressé (le 5 septembre à 22h04, la boutique)."),
        stat("Dernier palier franchi",
             "WITH m AS ("
             f"  SELECT ts, floor(max(donation_total) OVER (ORDER BY ts) / 1e6) AS mil FROM snapshot WHERE ts <= {END}"
             "), c AS (SELECT ts, mil, lag(mil) OVER (ORDER BY ts) AS prev FROM m) "
             "SELECT mil::int || ' M€ le ' || to_char(ts AT TIME ZONE 'Europe/Paris', 'DD/MM \"à\" HH24\"h\"MI') "
             "FROM c WHERE prev IS NOT NULL AND mil > prev ORDER BY ts DESC LIMIT 1",
             18, w=6, color="green", text_field=True,
             description="Le dernier million rond franchi par le total de l'événement, et quand (heure de Paris)."),

        row("Paliers et moments", 4),
        table("Les millions, un par un",
              "WITH m AS ("
              "  SELECT ts, floor(max(donation_total) OVER (ORDER BY ts) / 1e6) AS mil FROM snapshot"
              "), c AS ("
              "  SELECT ts, mil, lag(mil) OVER (ORDER BY ts) AS prev FROM m"
              "), x AS (SELECT ts, mil FROM c WHERE prev IS NOT NULL AND mil > prev) "
              'SELECT mil * 1e6 AS "Palier", ts AS "Atteint à", '
              '       extract(epoch FROM ts - lag(ts) OVER (ORDER BY ts)) AS "Après le précédent", '
              '       extract(epoch FROM ts - (SELECT min(ts) FROM snapshot)) AS "Depuis le début" '
              "FROM x ORDER BY mil DESC",
              0, 5, w=12, h=9, money_cols=("Palier",), duration_cols=("Après le précédent", "Depuis le début"),
              description="Quand le total de l'événement a franchi chaque million rond (maximum glissant : une baisse "
                          "du compteur ne peut pas franchir deux fois le même million)."),
        table("Plus gros gains en une minute",
              "WITH e AS ("
              "  SELECT s.ts, s.twitch_id, s.gain AS delta FROM streamer_sample_v s"
              "  WHERE $__timeFilter(s.ts) AND NOT s.derived AND s.gain > 0 AND s.gap_s <= 300"
              "  ORDER BY s.gain DESC LIMIT 200"
              ") "
              'SELECT e.ts AS "Heure", st.display AS "Streamer", e.delta AS "Gain", st.login AS login '
              "FROM e JOIN streamer_v st USING (twitch_id) "
              "WHERE st.login NOT IN ('zevent', 'zeventplays') AND " + LOC + " "
              "AND NOT EXISTS (SELECT 1 FROM streamer_sample_v p WHERE p.twitch_id = e.twitch_id "
              "                AND p.ts < e.ts AND p.ts >= e.ts - interval '10 minutes' AND p.gain <= -0.9 * e.delta) "
              "ORDER BY e.delta DESC LIMIT 25",
              12, 5, w=12, h=9, money_cols=("Gain",), streamer_links=True, description=BLIP_NOTE),
        table("Paliers des streamers",
              "SELECT st.profile_url AS \"Avatar\", st.display AS \"Streamer\", t.v AS \"Palier\", min(s.ts) AS \"Atteint à\", "
              "       st.login AS login "
              "FROM (VALUES (100000), (250000), (500000), (1000000), (2000000), (3000000), (5000000)) t(v) "
              "JOIN streamer_sample_v s ON s.donation_total >= t.v JOIN streamer_v st USING (twitch_id) "
              "WHERE NOT st.derived AND " + LOC + " GROUP BY st.profile_url, st.display, t.v, st.login "
              # not $__timeFilter(min(s.ts)): Grafana's macro parser stops at the first closing parenthesis
              "HAVING min(s.ts) BETWEEN $__timeFrom() AND $__timeTo() ORDER BY min(s.ts) DESC",
              0, 14, w=12, h=18, money_cols=("Palier",), image_cols=("Avatar",), streamer_links=True,
              description="Quand chaque streamer a franchi 100 K, 250 K, 500 K, 1 M, 2 M, 3 M ou 5 M€ pour la première "
                          "fois (les franchissements dans la plage de temps sélectionnée, les plus récents en premier)."),
        table("Plus gros bonds de viewers en une minute",
              "WITH e AS (SELECT s.ts, s.twitch_id, s.viewers_gain, s.viewers FROM streamer_sample_v s "
              "           WHERE $__timeFilter(s.ts) AND NOT s.derived AND s.viewers_gain > 0 "
              "             AND s.viewers - s.viewers_gain > 0 AND s.gap_s <= 300 ORDER BY s.viewers_gain DESC LIMIT 200) "
              "SELECT e.ts AS \"Heure\", st.profile_url AS \"Avatar\", st.display AS \"Streamer\", e.viewers_gain AS \"Bond\", "
              "       e.viewers AS \"Viewers après\", st.login AS login "
              "FROM e JOIN streamer_v st USING (twitch_id) WHERE " + LOC + " ORDER BY e.viewers_gain DESC LIMIT 25",
              12, 14, w=12, h=18, image_cols=("Avatar",), streamer_links=True,
              description="Plus fortes hausses du nombre de viewers d'un streamer entre deux relevés (raids, co-streams, "
                          "moments forts). Les passages en live (de 0 viewer à N) ne comptent pas."),
        table("Éditions précédentes",
              f"SELECT e.year AS \"Édition\", e.name AS \"Nom\", e.total AS \"Total\", "
              "       (SELECT min(ts) FROM snapshot WHERE donation_total >= e.total) AS \"Dépassé à\" "
              f"FROM {EDITIONS_VALUES} ORDER BY e.total DESC",
              0, 32, w=8, h=9, money_cols=("Total",),
              description="Total final de chaque édition et le moment où cette année l'a dépassé (vide : pas encore). "
                          "Pas d'édition en 2023."),
        barchart("Total par édition",
                 f"SELECT e.year::text AS \"Édition\", e.total AS \"Éditions précédentes\", NULL::numeric AS \"2026\" "
                 f"FROM {EDITIONS_VALUES} "
                 "UNION ALL (SELECT '2026', NULL, donation_total FROM snapshot ORDER BY ts DESC LIMIT 1) ORDER BY 1",
                 8, 32, w=10, x_field="Édition", unit="currencyEUR", stacking="normal", orientation="vertical",
                 series_colors={"Éditions précédentes": "blue", "2026": "green"},
                 description="Total final des éditions précédentes et total actuel de cette année. Pas d'édition en 2023."),
        stat(f"Record à battre ({RECORD_YEAR})", f"SELECT {RECORD}", 18, w=6, y=32, unit="currencyEUR", decimals=2, color="blue"),
        stat("Il manque, pour le battre",
             f"SELECT {RECORD} - donation_total FROM snapshot ORDER BY ts DESC LIMIT 1",
             18, w=6, y=36, h=5, unit="currencyEUR", decimals=2, thresholds=[(None, "green"), (1, "orange")],
             mappings=[{"type": "range", "options": {"from": -1e12, "to": 0, "result": {"text": "Record battu !", "color": "green"}}}],
             description=f"Écart entre le total actuel et le record de {RECORD_YEAR}."),

        row("Tendances", 41),
        # Global gain per snapshot split into what the streamer counters gained (all of them, no location
        # filter) and the rest (total gain minus streamer gains: tickets, shop, anonymous), then summed by
        # hour of the day. A snapshot without streamer samples counts fully as not tied to a streamer.
        barchart("Dons par heure de la journée (Europe/Paris)",
                 "SELECT to_char(h, 'FM00\"h\"') AS \"Heure\", sum(sg) AS \"À un streamer\", "
                 "       sum(g - sg) AS \"Sans streamer\" FROM ("
                 "  SELECT extract(hour FROM gl.ts AT TIME ZONE 'Europe/Paris') AS h, gl.g, coalesce(st.sg, 0) AS sg"
                 f"  FROM (SELECT ts, {gain_expr('donation_total')} AS g FROM snapshot WHERE $__timeFilter(ts)) gl"
                 "  LEFT JOIN (SELECT ts, sum(gain) AS sg FROM streamer_sample_v WHERE NOT derived AND $__timeFilter(ts)"
                 "             GROUP BY ts) st USING (ts)"
                 ") d WHERE g IS NOT NULL GROUP BY h ORDER BY h",
                 0, 42, x_field="Heure", unit="currencyEUR", orientation="vertical",
                 series_colors={"À un streamer": "green", "Sans streamer": "yellow"},
                 description="Gain du total de l'événement par heure de la journée, additionné sur la période sélectionnée "
                             "(tous lieux), séparé entre ce que les compteurs des streamers ont gagné et le reste. " + MIRROR_NOTE),
        table("Dons par heure visionnée par viewer",
              "WITH v AS ("
              f"  SELECT x.twitch_id, sum(x.viewers * {DT}) / 3600.0 AS viewer_hours, "
              f"         sum({DT}) FILTER (WHERE x.online) / 3600.0 AS hours"
              "  FROM streamer_sample_v x WHERE $__timeFilter(x.ts) AND x.gap_s IS NOT NULL GROUP BY x.twitch_id"
              "), " + GAIN_CTE +
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", g.gained / v.viewer_hours AS "Par heure visionnée par viewer", '
              'g.gained AS "Gagné", v.viewer_hours AS "Heures visionnées par viewer", v.hours AS "Heures", st.login AS login '
              "FROM g JOIN v USING (twitch_id) JOIN streamer_v st USING (twitch_id) "
              "WHERE NOT st.derived AND v.viewer_hours >= 100 AND " + LOC + " ORDER BY 3 DESC LIMIT 25",
              12, 42, w=12, h=9, money_cols=("Par heure visionnée par viewer", "Gagné"), hour_cols=("Heures",), image_cols=("Avatar",),
              streamer_links=True,
              description="Gagné sur la période sélectionnée divisé par les heures visionnées par viewer (viewers additionnés dans le "
                          "temps) : ce qu'une communauté donne par rapport à sa taille. Les streamers avec moins de "
                          "100 heures visionnées par viewer sont exclus."),
        # the final sprint started at 20:00 Paris time on the last evening (18:00 UTC)
        table("Le sprint final (de 20h à la fin de l'événement)",
              "WITH cur AS (SELECT max(ts) AS ts FROM snapshot), "
              "f AS (SELECT s.twitch_id, sum(s.gain) AS gained, max(s.viewers) AS peak FROM streamer_sample_v s, cur "
              "      WHERE s.ts > '2026-09-06 18:00:00+00'::timestamptz AND s.ts <= cur.ts AND NOT s.derived AND s.gain IS NOT NULL GROUP BY s.twitch_id) "
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", f.gained AS "Gagné", f.peak AS "Pic de viewers", st.login AS login '
              "FROM f JOIN streamer_v st USING (twitch_id) WHERE " + LOC + " ORDER BY f.gained DESC LIMIT 25",
              0, 51, w=12, h=9, money_cols=("Gagné",), image_cols=("Avatar",), streamer_links=True,
              description="Dons gagnés par chaque streamer depuis 20h (heure de Paris) le dernier soir jusqu'à la fin de "
                          "l'événement, et son pic de viewers sur cette période."),
        ts("Dons gagnés par intervalle, par lieu",
           "SELECT $__timeGroupAlias(d.ts, $__interval), CASE st.location WHEN 'LAN' THEN 'Sur place (LAN)' WHEN 'Online' THEN 'À distance (online)' ELSE 'Inconnu' END AS metric, sum(d.gain) AS value "
           "FROM streamer_sample_v d JOIN streamer_v st USING (twitch_id) "
           "WHERE $__timeFilter(d.ts) AND NOT d.derived AND d.gain IS NOT NULL AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           12, 51, unit="currencyEUR", bars=True, stack=True, min_interval="5m"),
        ts("Rythme des dons par minute (1 h glissante)",
           "SELECT a.ts AS time, (a.donation_total - b.donation_total) / (extract(epoch FROM a.ts - b.ts) / 60) AS \"Rythme sur 1 h\", "
           "       (SELECT (max(donation_total) - min(donation_total)) / greatest(extract(epoch FROM max(ts) - min(ts)) / 60, 1) "
           "        FROM snapshot) AS \"Moyenne de l'événement\" "
           "FROM snapshot a JOIN LATERAL (SELECT ts, donation_total FROM snapshot b WHERE b.ts <= a.ts - interval '1 hour' "
           "                              ORDER BY b.ts DESC LIMIT 1) b ON true "
           "WHERE $__timeFilter(a.ts) ORDER BY 1",
           0, 60, unit="currencyEUR",
           description="Gain du total de l'événement par minute, moyenné sur l'heure qui précède chaque point, comparé au "
                       "rythme moyen depuis le début. Un pic d'une heure suit chaque gros versement (boutique, billets)."),
        ts("Dons par heure visionnée par viewer au fil du temps",
           f"SELECT $__timeGroupAlias(x.ts, $__interval), sum(x.gain) / nullif(sum(x.viewers * {DT}) / 3600.0, 0) AS \"Dons par heure visionnée par viewer\" "
           "FROM streamer_sample_v x JOIN streamer_v st USING (twitch_id) "
           "WHERE $__timeFilter(x.ts) AND NOT st.derived AND x.gap_s IS NOT NULL AND " + ORG + " AND " + LOC + " "
           "GROUP BY 1 ORDER BY 1",
           12, 60, unit="currencyEUR", legend=False, min_interval="30m",
           description="Générosité par intervalle : dons gagnés par les streamers correspondant au filtre Lieu divisés par "
                       "leurs heures visionnées par viewer sur l'intervalle. Les chaînes de l'organisation (billets, boutique) sont exclues."),
        barchart("Viewers par heure de la journée (Europe/Paris)",
                 "SELECT to_char(h, 'FM00\"h\"') AS \"Heure\", round(avg(v)) AS \"Moyenne\", max(v) AS \"Pic\" FROM ("
                 "  SELECT extract(hour FROM s.ts AT TIME ZONE 'Europe/Paris') AS h, s.ts, sum(s.viewers) AS v"
                 "  FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)"
                 "  WHERE $__timeFilter(s.ts) AND NOT st.derived AND " + LOC + " GROUP BY s.ts"
                 ") x GROUP BY h ORDER BY h",
                 0, 69, x_field="Heure", unit="sishort", series_colors={"Moyenne": "purple", "Pic": "blue"}, orientation="vertical",
                 description="Viewers cumulés des streamers correspondant au filtre Lieu, par heure de la journée sur la "
                             "période sélectionnée : moyenne des relevés et plus haut relevé."),
        table("Jour par jour (Europe/Paris)",
              "WITH d AS ("
              f"  SELECT (ts AT TIME ZONE 'Europe/Paris')::date AS day, {RANGE_GAIN} AS gained, round(avg(viewers_total)) AS avg_v, "
              "         max(viewers_total) AS peak_v FROM snapshot WHERE $__timeFilter(ts) GROUP BY 1"
              "), h AS ("
              "  SELECT (s.ts AT TIME ZONE 'Europe/Paris')::date AS day, sum(least(s.gap_s, 300)) FILTER (WHERE s.online) / 3600.0 AS hours, "
              "         count(DISTINCT s.twitch_id) FILTER (WHERE s.online) AS live"
              "  FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)"
              "  WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.gap_s IS NOT NULL AND " + LOC + " GROUP BY 1"
              ") "
              "SELECT to_char(d.day, 'DD/MM') AS \"Jour\", d.gained AS \"Dons\", d.avg_v AS \"Viewers moyens\", d.peak_v AS \"Pic de viewers\", "
              "       h.hours AS \"Heures de stream\", h.live AS \"Streamers en live\" "
              "FROM d LEFT JOIN h USING (day) ORDER BY d.day",
              12, 69, w=12, h=9, money_cols=("Dons",), hour_cols=("Heures de stream",),
              description="Par jour calendaire : gain du total de l'événement et viewers (tous lieux), heures de stream et "
                          "streamers passés en live (filtre Lieu)."),

        row("Répartition", 78),
        barchart("Part des compteurs des streamers",
                 "WITH cur AS (SELECT s.twitch_id, s.donation_total FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
                 "             WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived AND " + LOC + "), "
                 "r AS (SELECT twitch_id, donation_total, row_number() OVER (ORDER BY donation_total DESC) AS rn, "
                 "             sum(donation_total) OVER () AS tot, count(*) OVER () AS n FROM cur) "
                 "SELECT CASE WHEN rn <= 10 THEN rn || '. ' || st.display ELSE 'Les ' || (n - 10) || ' autres' END AS \"Streamer\", "
                 "       sum(donation_total / nullif(tot, 0)) AS \"Part\" "
                 "FROM r JOIN streamer_v st USING (twitch_id) GROUP BY 1, rn <= 10 ORDER BY min(rn)",
                 0, 79, w=8, x_field="Streamer", unit="percentunit", orientation="horizontal",
                 description="Part de la somme des compteurs des streamers (filtre Lieu) détenue par chacun des dix premiers, "
                             "et par tous les autres réunis, à la fin de l'événement."),
        ts("Part détenue par le haut du classement",
           "SELECT ts AS time, sum(donation_total) FILTER (WHERE rank <= 1) / nullif(sum(donation_total), 0) AS \"Top 1\", "
           "       sum(donation_total) FILTER (WHERE rank <= 10) / nullif(sum(donation_total), 0) AS \"Top 10\", "
           "       sum(donation_total) FILTER (WHERE rank <= 25) / nullif(sum(donation_total), 0) AS \"Top 25\" "
           "FROM streamer_sample_v WHERE $__timeFilter(ts) AND NOT derived GROUP BY ts ORDER BY 1",
           8, 79, w=8, unit="percentunit",
           description="Part de la somme des compteurs des streamers détenue par le premier, les dix premiers et les "
                       "vingt-cinq premiers du classement, au fil du temps (tous lieux)."),
        ts("Streamers par palier de cagnotte",
           "SELECT s.ts AS time, CASE WHEN s.donation_total >= 1e6 THEN '1 M€ et plus' WHEN s.donation_total >= 1e5 THEN '100 K à 1 M€' "
           "       WHEN s.donation_total >= 1e4 THEN '10 K à 100 K€' WHEN s.donation_total >= 1e3 THEN '1 K à 10 K€' ELSE 'Moins de 1 K€' END AS metric, "
           "       count(*) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           16, 79, w=8, unit="sishort", stack=True,
           description="Nombre de streamers (filtre Lieu) dont la cagnotte est dans chaque tranche, au fil du temps."),
        xychart("Viewers et dons, un point par streamer",
                "WITH v AS ("
                f"  SELECT x.twitch_id, sum(x.viewers * {DT}) / 3600.0 AS viewer_hours, sum({DT}) FILTER (WHERE x.online) / 3600.0 AS hours"
                "  FROM streamer_sample_v x WHERE $__timeFilter(x.ts) AND x.gap_s IS NOT NULL GROUP BY x.twitch_id"
                "), " + GAIN_CTE +
                "SELECT st.display AS \"Streamer\", round(v.viewer_hours / nullif(v.hours, 0)) AS \"Viewers moyens en live\", "
                "       g.gained AS \"Gagné sur la période\" "
                "FROM g JOIN v USING (twitch_id) JOIN streamer_v st USING (twitch_id) "
                "WHERE NOT st.derived AND v.viewer_hours >= 100 AND v.hours > 0 AND g.gained > 0 AND " + LOC + " ORDER BY 3 DESC",
                0, 88, w=12, h=9, x_col="Viewers moyens en live", y_col="Gagné sur la période", y_unit="currencyEUR",
                description="Chaque point est un streamer : ses viewers moyens quand il est en live (horizontal) et ce qu'il a "
                            "gagné sur la période (vertical), échelles logarithmiques. Les points au-dessus du nuage sont les "
                            "communautés qui donnent le plus par rapport à leur taille. Survolez un point pour le nom."),
        stat("Dons reçus pendant le live et hors live",
             "SELECT coalesce(sum(s.gain) FILTER (WHERE s.online), 0) AS \"En live\", "
             "       coalesce(sum(s.gain) FILTER (WHERE NOT s.online), 0) AS \"Hors live\" "
             "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.gain IS NOT NULL AND " + LOC,
             12, w=12, y=88, unit="currencyEUR", decimals=0, color="green", text_mode="value_and_name",
             description="Dons gagnés par les compteurs des streamers (filtre Lieu) sur la période, selon que le streamer "
                         "était en live ou non au moment du relevé."),
        stat("Streamers dont la cagnotte dépasse",
             "SELECT count(*) FILTER (WHERE s.donation_total >= 1e3) AS \"1 K€\", count(*) FILTER (WHERE s.donation_total >= 1e4) AS \"10 K€\", "
             "       count(*) FILTER (WHERE s.donation_total >= 1e5) AS \"100 K€\", count(*) FILTER (WHERE s.donation_total >= 1e6) AS \"1 M€\" "
             "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived AND " + LOC,
             12, w=12, y=92, color="blue", text_mode="value_and_name", h=5,
             description="Nombre de streamers (filtre Lieu) dont la cagnotte dépasse chaque seuil, à la fin de l'événement."),

        row("Endurance et efficacité", 97),
        table("Plus longues sessions en live",
              "WITH s AS ("
              "  SELECT x.twitch_id, max(x.ts - coalesce(x.offline_at, st.first_seen)) AS longest"
              "  FROM streamer_sample_v x JOIN streamer_v st USING (twitch_id)"
              "  WHERE $__timeFilter(x.ts) AND x.online AND NOT st.derived AND " + LOC + " GROUP BY x.twitch_id"
              "), cur AS ("
              "  SELECT x.twitch_id, CASE WHEN x.online THEN x.ts - coalesce(x.offline_at, st.first_seen) END AS current"
              "  FROM streamer_sample_v x JOIN streamer_v st USING (twitch_id) WHERE x.ts = (SELECT max(ts) FROM snapshot)"
              ") "
              "SELECT st.profile_url AS \"Avatar\", st.display AS \"Streamer\", extract(epoch FROM s.longest) / 3600.0 AS \"Plus longue session\", "
              "       extract(epoch FROM cur.current) / 3600.0 AS \"Session en cours\", st.login AS login "
              "FROM s JOIN streamer_v st USING (twitch_id) LEFT JOIN cur USING (twitch_id) ORDER BY s.longest DESC LIMIT 25",
              0, 98, w=12, h=9, hour_cols=("Plus longue session", "Session en cours"), image_cols=("Avatar",),
              streamer_links=True,
              description="Plus long passage en live sans interruption observé sur la période (il peut avoir commencé "
                          "avant), et la durée de la session en cours si le streamer est en live."),
        table("Dons par heure de live",
              HOURS_CTE + ", " + GAIN_CTE +
              "SELECT st.profile_url AS \"Avatar\", st.display AS \"Streamer\", g.gained / nullif(h.hours, 0) AS \"Par heure de live\", "
              "       g.gained AS \"Gagné\", h.hours AS \"Heures\", st.login AS login "
              "FROM g JOIN h USING (twitch_id) JOIN streamer_v st USING (twitch_id) "
              "WHERE NOT st.derived AND h.hours >= 3 AND " + ORG + " AND " + LOC + " ORDER BY 3 DESC LIMIT 25",
              12, 98, w=12, h=9, money_cols=("Par heure de live", "Gagné"), hour_cols=("Heures",), image_cols=("Avatar",),
              streamer_links=True,
              description="Gagné sur la période divisé par les heures passées en live. Les streamers avec moins de 3 heures "
                          "de live et les chaînes de l'organisation sont exclus."),

        row("Sur place vs à distance", 107),
        ts("Streamers en live par lieu",
           "SELECT s.ts AS time, " + LOC_LABEL + " AS metric, count(*) FILTER (WHERE s.online) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           0, 108, unit="sishort", stack=True),
        ts("Viewers par lieu",
           "SELECT s.ts AS time, " + LOC_LABEL + " AS metric, sum(s.viewers) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           12, 108, unit="sishort", stack=True),
        ts("Cagnottes par lieu",
           "SELECT s.ts AS time, " + LOC_LABEL + " AS metric, sum(s.donation_total) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           0, 117, unit="currencyEUR", stack=True,
           description="Somme des compteurs des streamers sur place et à distance, au fil du temps."),
        stat("Part des dons sur place",
             "SELECT sum(s.donation_total) FILTER (WHERE st.location = 'LAN') / nullif(sum(s.donation_total), 0) "
             "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived",
             12, w=6, y=117, unit="percentunit", decimals=1, color="blue",
             description="Part de la somme des compteurs des streamers détenue par les streamers sur place, à la fin de l'événement."),
        stat("Cagnotte moyenne par streamer",
             "SELECT avg(s.donation_total) FILTER (WHERE st.location = 'LAN') AS \"Sur place\", "
             "       avg(s.donation_total) FILTER (WHERE st.location = 'Online') AS \"À distance\" "
             "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived",
             18, w=6, y=117, unit="currencyEUR", decimals=0, color="blue", text_mode="value_and_name"),
        stat("Dons gagnés sur la période, par lieu",
             "SELECT coalesce(sum(s.gain) FILTER (WHERE st.location = 'LAN'), 0) AS \"Sur place\", "
             "       coalesce(sum(s.gain) FILTER (WHERE st.location = 'Online'), 0) AS \"À distance\" "
             "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.gain IS NOT NULL",
             12, w=12, y=121, unit="currencyEUR", decimals=0, color="green", text_mode="value_and_name", h=5),

        row("Jeux", 126),
        ts("Streamers en live par jeu",
           "WITH top AS (SELECT game FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY game ORDER BY count(*) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Autre' END AS metric, count(*) AS value FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           0, 127, unit="sishort", stack=True),
        ts("Viewers par jeu",
           "WITH top AS (SELECT game FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY game ORDER BY sum(viewers) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Autre' END AS metric, sum(viewers) AS value FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           12, 127, unit="sishort", stack=True),
        table("Heures de stream par jeu",
              "SELECT coalesce(x.game, '(sans jeu)') AS \"Jeu\", sum(" + DT + ") / 3600.0 AS \"Heures\", "
              "       count(DISTINCT x.twitch_id) AS \"Streamers\", sum(x.viewers * " + DT + ") / 3600.0 AS \"Heures visionnées par viewer\" "
              "FROM streamer_sample_v x JOIN streamer_v st USING (twitch_id) "
              "WHERE $__timeFilter(x.ts) AND NOT st.derived AND " + LOC + " AND x.online AND x.gap_s IS NOT NULL "
              "GROUP BY 1 ORDER BY 2 DESC LIMIT 25",
              0, 136, w=24, h=9, hour_cols=("Heures",)),

        row("Dons sans streamer", 145),
        ts("Au fil du temps (total moins tous les streamers)",
           'SELECT sn.ts AS time, sn.donation_total - sum(s.donation_total) FILTER (WHERE NOT s.derived) AS "Sans streamer", '
           'coalesce(sum(s.donation_total) FILTER (WHERE s.derived), 0) AS "Cagnotte spéciale du Vieux Monsieur" '
           'FROM snapshot sn JOIN streamer_sample_v s USING (ts) '
           'WHERE $__timeFilter(sn.ts) GROUP BY sn.ts, sn.donation_total ORDER BY 1',
           0, 146, unit="currencyEUR", description=MIRROR_NOTE),
        ts("Par intervalle (gain total moins gains des streamers)",
           'SELECT $__timeGroupAlias(ts, $__interval), sum(g) - sum(sg) AS "Sans streamer" FROM ('
           f'  SELECT ts, {gain_expr("donation_total")} AS g FROM snapshot WHERE $__timeFilter(ts)'
           ') gl JOIN ('
           '  SELECT ts, sum(gain) AS sg FROM streamer_sample_v WHERE NOT derived AND $__timeFilter(ts) GROUP BY ts'
           ') st USING (ts) WHERE g IS NOT NULL GROUP BY 1 ORDER BY 1',
           12, 146, unit="currencyEUR", bars=True, legend=False, min_interval="5m", description=MIRROR_NOTE),

        row("Qualité des données", 155),
        stat("Relevés sur la période", "SELECT count(*) FROM snapshot WHERE $__timeFilter(ts)", 0, w=8, y=156,
             unit="sishort", color="blue", description="Nombre de relevés de l'API dans la plage de temps (un par minute)."),
        stat("Trous de collecte",
             "SELECT count(*) FROM (SELECT ts - lag(ts) OVER (ORDER BY ts) AS d FROM snapshot WHERE $__timeFilter(ts)) x "
             "WHERE d > interval '90 seconds'",
             8, w=8, y=156, unit="sishort", thresholds=[(None, "green"), (1, "orange"), (10, "red")],
             description="Nombre de fois où plus de 90 secondes séparent deux relevés (API injoignable ou collecteur arrêté)."),
        stat("Ratés de l'API",
             "SELECT count(*) FROM streamer_sample_v WHERE $__timeFilter(ts) AND NOT derived AND gain < -100",
             16, w=8, y=156, unit="sishort", thresholds=[(None, "green"), (1, "orange"), (50, "red")],
             description="Relevés où le compteur d'un streamer a perdu plus de 100 € (en général un compteur tombé à 0 et "
                         "rétabli quelques minutes plus tard)."),
    ]


# ---------------------------------------------------------------------------------------------------
# Hero header of the streamer dashboard: an HTML text panel fed by hidden query variables that follow the
# Streamer filter (chained variables re-run when $streamer changes, and on every refresh).
HERO_CUR = ("WITH cur AS (SELECT s.twitch_id, s.online, s.game, s.viewers, s.rank FROM streamer_sample_v s "
            "WHERE s.ts = (SELECT max(ts) FROM snapshot) AND s.twitch_id IN ($streamer) AND NOT s.derived "
            "ORDER BY s.donation_total DESC LIMIT 1) ")
HERO_VARS = {
    "hero_display": "SELECT st.display FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_login": "SELECT st.login FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_avatar": "SELECT coalesce(st.profile_url, '') FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_donation": "SELECT 'https://zevent.fr/don/' || st.display FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_location": "SELECT CASE st.location WHEN 'LAN' THEN 'Sur place au ZEVENT' WHEN 'Online' THEN "
                     "'En stream à distance' ELSE '' END FROM cur JOIN streamer_v st USING (twitch_id)",
    # after the event: the final rank instead of the live status and game
    "hero_status": "SELECT 'Rang final : ' || cur.rank FROM cur",
    "hero_color": "SELECT '#3fb950' FROM cur",
}


def hidden_var(name, sql):
    return {"name": name, "type": "query", "datasource": DS, "query": HERO_CUR + sql, "definition": name,
            "hide": 2, "refresh": 2, "multi": False, "includeAll": False, "sort": 0, "current": {}}


HERO_HTML = """
<div style="display:flex;align-items:center;gap:28px;height:100%;padding:8px 12px">
  <a href="https://twitch.tv/${hero_login}" target="_blank" rel="noopener" style="flex-shrink:0">
    <img src="${hero_avatar}" alt="${hero_display}"
         style="width:190px;height:190px;border-radius:50%;object-fit:cover;border:5px solid ${hero_color};display:block">
  </a>
  <div style="min-width:0">
    <div style="font-size:44px;font-weight:800;line-height:1.05;letter-spacing:-0.5px">
      <a href="https://twitch.tv/${hero_login}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">${hero_display}</a>
    </div>
    <div style="font-size:17px;opacity:.75;margin-top:6px">
      <a href="https://twitch.tv/${hero_login}" target="_blank" rel="noopener" style="color:inherit">twitch.tv/${hero_login}</a>
      &middot; ${hero_location}
    </div>
    <div style="font-size:22px;margin-top:14px;font-weight:700">
      <span style="color:${hero_color}">&#9679; ${hero_status}</span>
    </div>
    <div style="font-size:19px;margin-top:14px">
      <a href="${hero_donation}" target="_blank" rel="noopener" style="color:#3fb950;font-weight:600;text-decoration:none">Faire un don &#10084;</a>
    </div>
  </div>
</div>
"""


def text_panel(html, x, y, w, h):
    global _id
    _id += 1
    return {"id": _id, "type": "text", "title": "", "transparent": True, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "html", "content": html.strip()}}


# Turns a (name, value) row into a field named after the streamer, so a stat can show "Domingo: 12 345 €".
ROWS_TO_FIELDS = {"id": "rowsToFields", "options": {"mappings": [
    {"fieldName": "name", "handlerKey": "field.name"}, {"fieldName": "value", "handlerKey": "field.value"}]}}


def neighbour_sql(above):
    """Distance to the streamer one place above (positive: what is missing) or below (positive: the lead)
    the selected streamer in the donation ranking at the latest snapshot; no row for the first / last."""
    cmp, order, expr = ("<", "DESC", "s.donation_total - me.donation_total") if above else (">", "ASC", "me.donation_total - s.donation_total")
    return (
        "WITH cur AS (SELECT max(ts) AS ts FROM snapshot), "
        "me AS (SELECT s.rank, s.donation_total FROM streamer_sample_v s, cur WHERE s.ts = cur.ts "
        "       AND s.twitch_id IN ($streamer) AND NOT s.derived ORDER BY s.donation_total DESC LIMIT 1) "
        f"SELECT st.display AS name, {expr} AS value "
        "FROM me, cur, streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
        f"WHERE s.ts = cur.ts AND NOT s.derived AND s.rank {cmp} me.rank ORDER BY s.rank {order} LIMIT 1"
    )


# ZEVENT streamer: one or a few streamers in detail. The Streamer filter has no "All" and lists streamers
# by donations, so the dashboard opens on the current leader.
def streamer_panels():
    reset_ids()
    sel = ("FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
           "WHERE NOT st.derived AND s.twitch_id IN ($streamer)")
    return [
        text_panel(HERO_HTML, 0, 0, 9, 12),
        stat("Cagnotte finale",
             f"SELECT coalesce(sum(s.donation_total), 0) {sel} AND s.ts = (SELECT max(ts) FROM snapshot)",
             9, w=5, unit="currencyEUR", decimals=2),
        stat("Rang final",
             "SELECT min(rank) FROM streamer_sample_v WHERE ts = (SELECT max(ts) FROM snapshot) AND NOT derived "
             "AND twitch_id IN ($streamer)",
             14, w=5, unit="sishort", color="yellow",
             description="Position au classement des dons à la fin de l'événement."),
        stat("Meilleur rang",
             "SELECT min(rank) FROM streamer_sample_v WHERE $__timeFilter(ts) AND NOT derived AND twitch_id IN ($streamer)",
             19, w=5, unit="sishort", color="yellow",
             description="Meilleure position atteinte au classement des dons dans la plage de temps sélectionnée."),
        stat("Gagné sur la période",
             "WITH " + GAIN_CTE + "SELECT coalesce(sum(g.gained), 0) FROM g JOIN streamer_v st USING (twitch_id) "
             "WHERE NOT st.derived AND g.twitch_id IN ($streamer)",
             9, w=5, y=4, unit="currencyEUR", decimals=2, color="orange"),
        stat("Heures de stream", hours_streamed_sql("true", with_game="ZEVENT"), 14, w=5, y=4, unit="suffix: h", decimals=1,
             color="green", text_mode="value_and_name",
             description=HOURS_DESCRIPTION + " « Dont catégorie ZEVENT » : la part de ce temps passée avec la catégorie "
                                             "Twitch « ZEVENT »."),
        stat("Pic de viewers",
             f"SELECT coalesce(max(v), 0) FROM (SELECT s.ts, sum(s.viewers) AS v {sel} AND $__timeFilter(s.ts) GROUP BY s.ts) x",
             19, w=5, y=4, unit="sishort", color="purple", description="Plus grand nombre de viewers cumulés sur la période sélectionnée."),

        # third row of tiles next to the hero, y=8
        stat("Part du total de l'événement",
             f"SELECT coalesce(sum(s.donation_total), 0) / nullif((SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1), 0) "
             f"{sel} AND s.ts = (SELECT max(ts) FROM snapshot)",
             9, w=5, y=8, unit="percentunit", decimals=2, color="blue",
             description="Compteurs des streamers sélectionnés divisés par le total de l'événement, à la fin de l'événement."),
        stat("Viewers moyens en live",
             f"SELECT sum(x.viewers * {DT}) / nullif(sum({DT}) FILTER (WHERE x.online), 0) "
             "FROM streamer_sample_v x WHERE $__timeFilter(x.ts) AND x.twitch_id IN ($streamer) AND x.gap_s IS NOT NULL",
             14, w=5, y=8, unit="sishort", decimals=0, color="purple",
             description="Heures visionnées par viewer divisées par les heures en live, sur la période et les streamers sélectionnés."),
        stat("Heures visionnées", viewer_hours_sql("true"), 19, w=5, y=8, unit="sishort", decimals=1, color="purple",
             description=VIEWER_HOURS_DESCRIPTION),

        # fourth row of tiles, full width, y=12
        stat("Meilleure heure",
             f"SELECT {PARIS_HOUR} || ' : ' || {eur_text('g')} FROM ("
             f"  SELECT date_trunc('hour', s.ts AT TIME ZONE 'Europe/Paris') AS h, sum(s.gain) AS g {sel}"
             "  AND $__timeFilter(s.ts) AND s.gain IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
             ") x",
             0, w=12, y=12, color="orange", text_field=True,
             description="L'heure (heure de Paris) où la cagnotte du streamer a le plus progressé, dans la plage de temps sélectionnée."),
        stat("Dons par heure visionnée par viewer", per_viewer_hour_sql("true"), 12, w=12, y=12, unit="currencyEUR", decimals=2,
             color="green", description=PER_VIEWER_HOUR_DESCRIPTION),

        # fifth row of tiles, y=16: neighbours in the ranking, live/offline split, longest session
        stat("Écart avec le précédent", neighbour_sql(above=True), 0, w=6, y=16, unit="currencyEUR", decimals=0,
             color="yellow", text_mode="value_and_name", transformations=[ROWS_TO_FIELDS], no_value="En tête !",
             description="Combien il manque pour dépasser le streamer juste devant au classement des dons, à la fin de l'événement."),
        stat("Avance sur le suivant", neighbour_sql(above=False), 6, w=6, y=16, unit="currencyEUR", decimals=0,
             color="yellow", text_mode="value_and_name", transformations=[ROWS_TO_FIELDS], no_value="Dernier",
             description="Avance sur le streamer juste derrière au classement des dons, à la fin de l'événement."),
        stat("Dons reçus en live et hors live",
             "SELECT coalesce(sum(s.gain) FILTER (WHERE s.online), 0) AS \"En live\", "
             f"       coalesce(sum(s.gain) FILTER (WHERE NOT s.online), 0) AS \"Hors live\" {sel} "
             "AND $__timeFilter(s.ts) AND s.gain IS NOT NULL",
             12, w=6, y=16, unit="currencyEUR", decimals=0, color="green", text_mode="value_and_name",
             description="Dons gagnés sur la période selon que le streamer était en live ou non au moment du relevé."),
        stat("Plus longue session en live",
             f"SELECT extract(epoch FROM max(s.ts - coalesce(s.offline_at, st.first_seen))) / 3600.0 {sel} "
             "AND $__timeFilter(s.ts) AND s.online",
             18, w=6, y=16, unit="suffix: h", decimals=1, color="green",
             description="Plus long passage en live sans interruption observé sur la période (il peut avoir commencé avant)."),

        ts("Dons au fil du temps",
           'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.donation_total AS value '
           f'{sel} AND $__timeFilter(s.ts) ORDER BY 1',
           0, 20, unit="currencyEUR", streamer_links=True),
        ts("Viewers au fil du temps",
           'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.viewers AS value '
           f'{sel} AND $__timeFilter(s.ts) ORDER BY 1',
           12, 20, unit="sishort", streamer_links=True),
        ts("Dons gagnés par intervalle",
           'SELECT $__timeGroupAlias(d.ts, $__interval), st.display AS metric, st.login AS login, sum(d.gain) AS value '
           'FROM streamer_sample_v d JOIN streamer_v st USING (twitch_id) '
           'WHERE $__timeFilter(d.ts) AND d.twitch_id IN ($streamer) AND d.gain IS NOT NULL GROUP BY 1, 2, 3 ORDER BY 1',
           0, 29, unit="currencyEUR", bars=True, stack=True, min_interval="5m", streamer_links=True),
        ts("Rank au classement des dons au fil du temps",
           "SELECT r.ts AS time, st.display AS metric, st.login AS login, r.rank AS value "
           "FROM streamer_sample_v r JOIN streamer_v st USING (twitch_id) "
           "WHERE $__timeFilter(r.ts) AND r.twitch_id IN ($streamer) AND r.rank IS NOT NULL ORDER BY 1",
           12, 29, unit="sishort", streamer_links=True, description="1 est la première place ; plus c'est bas, mieux c'est."),
        game_timeline(0, 38),
    ]


def game_timeline(x, y):
    """State timeline of the games played by the selected streamers, one colour per game."""
    sql = LIVE_SQL.format(loc="true", filter=" AND s.twitch_id IN ($streamer)", game="true")
    return panel(
        "state-timeline", "Jeux joués", sql, x, y, 24, 8, fmt="table",
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
        description="Ce que chaque streamer sélectionné jouait en live ; les trous sont du temps hors ligne.",
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


# Streamer filter of the streamer dashboard: one streamer at a time, no "All", ordered by donations so
# the first (default) entry is the current leader; the derived mistermv row is not a streamer and is
# left out. A single-select value is inserted unquoted, so the queries use ${streamer:sqlstring}.
STREAMER_VAR_DETAIL = query_var(
    "streamer", "Streamer",
    "SELECT st.display AS __text, st.twitch_id AS __value FROM streamer_v st "
    "JOIN streamer_sample_v s USING (twitch_id) WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived "
    "ORDER BY s.donation_total DESC",
    multi=False, include_all=False,
)

LOCATION_VAR = query_var("location", "Lieu", LOC_QUERY, all_value=".*")
# Same Location filter, but "On site (LAN)" selected by default (live dashboard).
LOCATION_VAR_LAN = copy.deepcopy(LOCATION_VAR)
LOCATION_VAR_LAN["current"] = {"selected": True, "text": ["Sur place (LAN)"], "value": ["LAN"]}


DASHBOARDS = [  # (uid, button title)
    ("zevent-public", "Stats principales"),
    ("zevent-live-public", "Timeline"),
    ("zevent-insights-public", "Analyses"),
    ("zevent-streamer-public", "Streamer"),
    ("zevent-viewers-public", "Viewers"),
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


# Million crossings of the event total as annotation markers (running max, so a counter dip cannot cross
# the same million twice). Shown only on the panel(s) whose ids are given.
MILLIONS_ANNOTATION_SQL = (
    "WITH m AS (SELECT ts, floor(max(donation_total) OVER (ORDER BY ts) / 1e6) AS mil FROM snapshot WHERE $__timeFilter(ts)), "
    "c AS (SELECT ts, mil, lag(mil) OVER (ORDER BY ts) AS prev FROM m) "
    "SELECT ts AS time, mil::int || ' M€' AS text FROM c WHERE prev IS NOT NULL AND mil > prev ORDER BY ts"
)


def millions_annotation(panel_ids):
    return {
        "name": "Millions", "datasource": DS, "enable": True, "hide": False, "iconColor": "green",
        "target": {"format": "table", "rawQuery": True, "rawSql": MILLIONS_ANNOTATION_SQL, "refId": "Anno"},
        "filter": {"exclude": False, "ids": list(panel_ids)},
    }


def dashboard_base(uid, title, variables, panels_, annotations=()):
    variables = copy.deepcopy(variables)
    for v in variables:
        if v.get("type") == "query":
            v["query"] = freeze_sql(v["query"])
            v["definition"] = freeze_sql(v.get("definition", ""))
    for pnl in panels_:
        for t in pnl.get("targets", []):
            t["rawSql"] = freeze_sql(t["rawSql"])
    return {
        "uid": uid,
        "title": title,
        "tags": ["zevent"],
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,
        "refresh": "",   # the event is over: nothing to refresh
        # hidden time picker (it also hides the refresh button); from/to stay fixed at TIME_RANGE
        "timepicker": {"hidden": True, "refresh_intervals": []},
        "time": TIME_RANGE,
        "schemaVersion": 39,
        "version": 1,
        "templating": {"list": variables},
        "links": links_from(uid),
        "annotations": {"list": list(annotations)},
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
  SELECT s.ts, s.twitch_id, st.display, CASE WHEN s.online AND {game} THEN coalesce(s.game, '(sans jeu)') END AS state
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
SELECT ts AS time, display AS "Streamer", state AS "État", first_live
FROM r ORDER BY first_live NULLS LAST, lower(display), ts
"""

LIVE_DESCRIPTION = (
    "Vert pendant le live. Survolez une barre pour voir le jeu. Les lignes sont triées par le premier passage en "
    "live du streamer sur la période sélectionnée ; les streamers qui n'ont pas été en live sur la période sont en bas."
)


def live_timeline(title, y, h, loc, filtered, description, per_page, game="true"):
    sql = LIVE_SQL.format(loc=loc, filter=" AND s.twitch_id IN ($streamer)" if filtered else "", game=game)
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


# "Catégorie" switch of the live dashboard: every panel counts a sample as live only when this holds.
ZEVENT_ONLY = "('$zevent_only' = '0' OR s.game = 'ZEVENT')"
ZEVENT_ONLY_VAR = {
    "name": "zevent_only", "label": "Catégorie", "type": "custom",
    "query": "Tous les jeux : 0, ZEVENT uniquement : 1",
    "options": [{"text": "Tous les jeux", "value": "0", "selected": True},
                {"text": "ZEVENT uniquement", "value": "1", "selected": False}],
    "current": {"selected": True, "text": "Tous les jeux", "value": "0"},
    "multi": False, "includeAll": False, "hide": 0,
}
SWITCH_NOTE = " Avec le sélecteur Catégorie sur « ZEVENT uniquement », seul le temps passé avec la catégorie Twitch « ZEVENT » compte."


def live_panels(loc, scope):
    """Panels of the live dashboard. `loc` is the SQL location filter (st = streamer_v), `scope` a label."""
    reset_ids()
    latest = "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE NOT st.derived AND " + loc
    live = "s.online AND " + ZEVENT_ONLY
    return [
        stat(f"Streamers ({scope})",
             f'SELECT count(DISTINCT s.twitch_id) FILTER (WHERE {live}) AS "Passés en live", count(DISTINCT s.twitch_id) AS "Inscrits" {latest} '
             "AND $__timeFilter(s.ts)",
             0, w=4, color="blue", text_mode="value_and_name",
             description="Streamers passés en live au moins une fois dans la plage de temps, et tous ceux listés." + SWITCH_NOTE),
        stat(f"Pic de streamers en live ({scope})",
             f"SELECT max(n) FROM (SELECT count(*) AS n {latest} AND {live} GROUP BY s.ts) x",
             4, w=4, unit="sishort", color="blue",
             description="Plus grand nombre de streamers en live en même temps, sur la période." + SWITCH_NOTE),
        stat("Heures de stream", hours_streamed_sql(loc, with_game="ZEVENT", live=live), 8, w=6, unit="suffix: h",
             decimals=1, color="green", text_mode="value_and_name",
             description=HOURS_DESCRIPTION + " « Dont catégorie ZEVENT » : la part de ce temps passée avec la catégorie "
                                             "Twitch « ZEVENT »." + SWITCH_NOTE),
        ts(f"Streamers en live au fil du temps ({scope})",
           f'SELECT s.ts AS time, count(*) FILTER (WHERE {live}) AS "En live" {latest} AND $__timeFilter(s.ts) '
           "GROUP BY 1 ORDER BY 1",
           14, 0, w=10, h=4, unit="sishort", legend=False),
        # paginated, follows the Location and Streamer filters and the Catégorie switch
        live_timeline(f"Streamers ({scope}, $streamer)", 4, 34, loc, filtered=True, per_page=50, game=ZEVENT_ONLY,
                      description=LIVE_DESCRIPTION + " Suit les filtres Lieu et Streamer." + SWITCH_NOTE),
    ]


# ---------------------------------------------------------------------------------------------------
# ZEVENT viewers: one chart, the viewers of every streamer stacked over time. Rows are bucketed by the
# chart interval (15 minutes at least: ~340 streamers x 300 buckets is what a browser stacks comfortably)
# and each bucket holds the average of the per-minute counts, so the top of the stack is the total viewer
# count. The datasource orders the series by name whatever the query's order, so the stack is alphabetical
# bottom to top; the legend is off (hover a band for the name), and the Location and Streamer filters apply.
def viewers_panels():
    reset_ids()
    chart = ts("Viewers de chaque streamer au fil du temps ($location, $streamer)",
           "SELECT $__timeGroupAlias(s.ts, $__interval), st.display AS metric, sum(s.viewers)::float / count(DISTINCT s.ts) AS value "
           "FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
           "WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC + " "
           "GROUP BY 1, 2 ORDER BY 1",
           0, 0, w=24, h=22, unit="sishort", stack=True, legend=False, min_interval="15m",
           description="Les viewers de chaque streamer, empilés : le haut de la pile est le total des viewers des streamers "
                       "correspondant aux filtres Lieu et Streamer. Survolez une bande pour voir le streamer. Chaque point "
                       "est la moyenne sur l'intervalle (15 minutes au moins).")
    # every streamer live at the hovered instant, biggest first; the streamers at 0 are left out of the list
    chart["options"]["tooltip"] = {"mode": "multi", "sort": "desc", "hideZeros": True, "maxHeight": 600}
    return [chart]


# ---------------------------------------------------------------------------------------------------
here = Path(__file__).parent


def write(dash):
    out = here / "provisioning" / "dashboards" / f"{dash['uid']}.json"
    out.write_text(json.dumps(dash, indent=2) + "\n")
    print(f"wrote {out} ({len(dash['panels'])} panels)")


main = main_panels()
total_chart = next(p["id"] for p in main if p["title"] == "Total des dons au fil du temps")
write(dashboard_base("zevent-public", "ZEVENT", [LOCATION_VAR, streamer_var(LOC)], main,
                     annotations=[millions_annotation([total_chart])]))
write(dashboard_base("zevent-live-public", "ZEVENT timeline", [LOCATION_VAR_LAN, streamer_var(LOC), ZEVENT_ONLY_VAR],
                     live_panels(LOC, "$location")))
write(dashboard_base("zevent-insights-public", "ZEVENT analyses", [LOCATION_VAR], insights_panels()))
write(dashboard_base("zevent-viewers-public", "ZEVENT viewers", [LOCATION_VAR, streamer_var(LOC)], viewers_panels()))
def single_select(dash, name):
    """Rewrite IN ($name) as IN (${name:sqlstring}) in every query: a single-select value is inserted unquoted."""
    a, b = f"IN (${name})", f"IN (${{{name}:sqlstring}})"
    for pnl in dash["panels"]:
        for t in pnl.get("targets", []):
            t["rawSql"] = t["rawSql"].replace(a, b)
    for v in dash["templating"]["list"]:
        if v["name"] != name:
            v["query"] = v["query"].replace(a, b)
    return dash


write(single_select(dashboard_base("zevent-streamer-public", "ZEVENT streamer",
                                   [STREAMER_VAR_DETAIL] + [hidden_var(n, q) for n, q in HERO_VARS.items()],
                                   streamer_panels()), "streamer"))
