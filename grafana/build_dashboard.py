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
    return {"title": "Ouvrir sur Twitch", "url": "https://twitch.tv/${" + var + "}", "targetBlank": True}


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
    "Charity sans membre), les billets du concert de jeudi, tous les dons de la boutique, etc. La ligne dérivée "
    "\"Cagnotte spéciale du Vieux Monsieur\" est exclue de la somme des streamers : depuis le 5 sept. à 01:08 UTC, elle "
    "reflète le compteur de Domingo, qui est déjà compté une fois."
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


def hours_streamed_sql(loc):
    return (
        f"SELECT coalesce(sum({LIVE_SECONDS}) FILTER (WHERE s.online), 0) / 3600.0 "
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
    "filtres Lieu et Streamer sur la période sélectionnée, divisés par leurs viewer-heures (le nombre de viewers "
    "additionné minute par minute sur la même période, en heures). Un viewer qui regarde pendant une heure vaut "
    "une viewer-heure : 5 \u20ac signifie 5 \u20ac récoltés pour chaque heure regardée par un viewer. Les dons sans "
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
    "La taille de l'audience dans le temps, en viewer-heures : le nombre de viewers des streamers correspondant "
    "aux filtres Lieu et Streamer, additionné minute par minute sur la période sélectionnée (chaque relevé compte "
    "le temps écoulé depuis le précédent, plafonné à 5 minutes pour que les trous dans les données ne comptent "
    "pas). Un viewer qui regarde pendant une heure vaut une viewer-heure ; 1 000 viewers pendant 2 heures font "
    "2 000 viewer-heures."
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
        f'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", {extra_col}cur.donation_total AS "Cagnotte", '
        'cur.viewers AS "Viewers", coalesce(h.hours, 0) AS "Heures de stream", st.login AS login '
        "FROM cur JOIN streamer_v st USING (twitch_id) LEFT JOIN h USING (twitch_id) " + extra_join +
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
             stacking="none"):
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
    return panel(
        "barchart", title, sql, x, y, w, h, fmt="table",
        fieldConfig={"defaults": d, "overrides": overrides},
        options={"orientation": "auto", "xField": x_field, "showValue": "never", "barWidth": 0.8, "stacking": stacking,
                 "legend": {"showLegend": multi, "displayMode": "list", "placement": "bottom"},
                 "tooltip": {"mode": "multi" if multi else "single", "sort": "none"}},
        **extra,
    )


# ---------------------------------------------------------------------------------------------------
# ZEVENT (main): headline tiles, the global graphs and the leaderboards. Everything else lives in the
# insights and streamer dashboards.
def main_panels():
    reset_ids()
    return [
        stat("Total des dons", "SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1", 0, w=6,
             unit="currencyEUR", decimals=2),
        stat("Dons, dernière heure",
             "SELECT max(donation_total) - min(donation_total) FROM snapshot WHERE ts > now() - interval '1 hour'", 6,
             w=6, unit="currencyEUR", decimals=2, color="orange"),
        stat("Dons sans streamer",
             "SELECT sn.donation_total - sum(s.donation_total) FROM snapshot sn JOIN streamer_sample_v s USING (ts) "
             "WHERE NOT s.derived AND sn.ts = (SELECT max(ts) FROM snapshot) GROUP BY sn.donation_total",
             12, w=6, unit="currencyEUR", decimals=2, color="yellow", description=MIRROR_NOTE),
        stat("Cagnotte spéciale du Vieux Monsieur",
             "SELECT coalesce(sum(donation_total), 0) FROM streamer_sample_v "
             "WHERE derived AND ts = (SELECT max(ts) FROM snapshot)",
             18, w=6, unit="currencyEUR", decimals=2, color="red",
             description="tkt"),
        # Second and third tile rows: audience and rate tiles, three per row. Viewers of the streamers
        # matching the Location and Streamer filters (sum of the per-streamer counts, which equals the API's
        # global viewer count).
        stat("Viewers actuels",
             "SELECT coalesce(sum(s.viewers), 0) FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
             "WHERE s.ts = (SELECT max(ts) FROM snapshot) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC,
             0, w=8, y=4, unit="sishort", color="purple",
             description="Viewers des streamers correspondant aux filtres Lieu et Streamer, au dernier relevé."),
        stat("Pic de viewers",
             "SELECT coalesce(max(v), 0) FROM ("
             "  SELECT s.ts, sum(s.viewers) AS v FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id)"
             "  WHERE $__timeFilter(s.ts) AND NOT st.derived AND s.twitch_id IN ($streamer) AND " + LOC +
             "  GROUP BY s.ts"
             ") x",
             8, w=8, y=4, unit="sishort", color="purple",
             description="Plus grand nombre de viewers cumulés des streamers correspondant aux filtres Lieu et Streamer, "
                         "dans la plage de temps sélectionnée."),
        stat("Streamers en live",
             'SELECT streamers_online AS "En live", streamers_total AS "Total" FROM snapshot ORDER BY ts DESC LIMIT 1',
             0, w=8, y=8, color="blue", text_mode="value_and_name"),
        stat("Viewer-heures", viewer_hours_sql(LOC), 16, w=8, y=4, unit="sishort", decimals=1, color="purple",
             description=VIEWER_HOURS_DESCRIPTION),
        hours_stat(8, 8, LOC, y=8),
        stat("Dons par viewer-heure", per_viewer_hour_sql(LOC), 16, w=8, y=8, unit="currencyEUR", decimals=2,
             color="green", description=PER_VIEWER_HOUR_DESCRIPTION),

        row("Global", 12),
        ts("Total des dons au fil du temps",
           'SELECT ts AS time, donation_total AS "Total" FROM snapshot WHERE $__timeFilter(ts) ORDER BY 1',
           0, 13, unit="currencyEUR", legend=False),
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

        row("Classements (dernier relevé, $location)", 31),
        # 2x2 grid
        leaderboard("Top par dons", 0, 32, order_by="cur.donation_total DESC"),
        leaderboard("Top par viewers", 12, 32, order_by="cur.viewers DESC", where="cur.online"),
        leaderboard("Top des gains sur la période", 0, 44, order_by="g.gained DESC",
                    extra_cte=", " + GAIN_CTE, extra_col='g.gained AS "Gagné", ',
                    extra_join="JOIN g USING (twitch_id) ", money_cols=("Gagné",)),
        leaderboard("Top des heures de stream sur la période", 12, 44,
                    order_by="coalesce(h.hours, 0) DESC, cur.donation_total DESC"),
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
    rate = (
        "WITH cur AS (SELECT ts, donation_total FROM snapshot ORDER BY ts DESC LIMIT 1), "
        "past AS (SELECT sn.donation_total FROM snapshot sn, cur WHERE sn.ts <= cur.ts - interval '10 minutes' "
        "         ORDER BY sn.ts DESC LIMIT 1) "
    )
    by_loc = ("FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
              "WHERE $__timeFilter(s.ts) AND NOT st.derived AND " + LOC)
    return [
        stat("Rythme des dons, 10 dernières min",
             rate + "SELECT (cur.donation_total - past.donation_total) / 10 FROM cur, past",
             0, w=6, unit="currencyEUR", decimals=0, color="orange",
             description="Gain du total de l'événement par minute sur les 10 dernières minutes de données."),
        stat("Rythme moyen depuis le début",
             "SELECT (max(donation_total) - min(donation_total)) / greatest(extract(epoch FROM max(ts) - min(ts)) / 60, 1) "
             "FROM snapshot",
             6, w=6, unit="currencyEUR", decimals=0, color="orange", description="Total de l'événement par minute, sur tout l'événement."),
        stat("Prochain million",
             "SELECT (floor(donation_total / 1e6) + 1) * 1e6 FROM snapshot ORDER BY ts DESC LIMIT 1",
             12, w=6, unit="currencyEUR", decimals=0, color="green"),
        stat("Prochain million dans",
             rate + "SELECT CASE WHEN cur.donation_total > past.donation_total THEN "
                    "((floor(cur.donation_total / 1e6) + 1) * 1e6 - cur.donation_total) "
                    "/ ((cur.donation_total - past.donation_total) / 600.0) END FROM cur, past",
             18, w=6, unit="dtdurations", decimals=0, color="green",
             description="Au rythme des dons des 10 dernières minutes."),

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

        row("Tendances", 14),
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
                 0, 15, x_field="Heure", unit="currencyEUR",
                 series_colors={"À un streamer": "green", "Sans streamer": "yellow"},
                 description="Gain du total de l'événement par heure de la journée, additionné sur la période sélectionnée "
                             "(tous lieux), séparé entre ce que les compteurs des streamers ont gagné et le reste. " + MIRROR_NOTE),
        table("Dons par viewer-heure",
              "WITH v AS ("
              f"  SELECT x.twitch_id, sum(x.viewers * {DT}) / 3600.0 AS viewer_hours, "
              f"         sum({DT}) FILTER (WHERE x.online) / 3600.0 AS hours"
              "  FROM streamer_sample_v x WHERE $__timeFilter(x.ts) AND x.gap_s IS NOT NULL GROUP BY x.twitch_id"
              "), " + GAIN_CTE +
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", g.gained / v.viewer_hours AS "Par viewer-heure", '
              'g.gained AS "Gagné", v.viewer_hours AS "Viewer-heures", v.hours AS "Heures", st.login AS login '
              "FROM g JOIN v USING (twitch_id) JOIN streamer_v st USING (twitch_id) "
              "WHERE NOT st.derived AND v.viewer_hours >= 100 AND " + LOC + " ORDER BY 3 DESC LIMIT 25",
              12, 15, w=12, h=9, money_cols=("Par viewer-heure", "Gagné"), hour_cols=("Heures",), image_cols=("Avatar",),
              streamer_links=True,
              description="Gagné sur la période sélectionnée divisé par les viewer-heures (viewers additionnés dans le "
                          "temps) : ce qu'une communauté donne par rapport à sa taille. Les streamers avec moins de "
                          "100 viewer-heures sont exclus."),
        table("Tendance du moment (15 dernières minutes)",
              "WITH cur AS (SELECT max(ts) AS ts FROM snapshot), "
              "n AS (SELECT twitch_id, donation_total, viewers FROM streamer_sample_v, cur WHERE streamer_sample_v.ts = cur.ts), "
              "p AS (SELECT DISTINCT ON (ss.twitch_id) ss.twitch_id, ss.donation_total, ss.viewers FROM streamer_sample_v ss, cur "
              "      WHERE ss.ts BETWEEN cur.ts - interval '60 minutes' AND cur.ts - interval '15 minutes' "
              "      ORDER BY ss.twitch_id, ss.ts DESC) "
              'SELECT st.profile_url AS "Avatar", st.display AS "Streamer", n.donation_total - p.donation_total AS "Gagné", '
              'n.viewers AS "Viewers", n.viewers - p.viewers AS "Variation viewers", st.login AS login '
              "FROM n JOIN p USING (twitch_id) JOIN streamer_v st USING (twitch_id) "
              "WHERE NOT st.derived AND " + LOC + " ORDER BY 3 DESC LIMIT 25",
              0, 24, w=12, h=9, money_cols=("Gagné",), image_cols=("Avatar",), streamer_links=True,
              description="Streamers classés par dons gagnés sur les 15 dernières minutes de données, avec la variation "
                          "du nombre de viewers (par rapport au dernier relevé vieux d'au moins 15 minutes, en remontant "
                          "jusqu'à une heure)."),
        ts("Dons gagnés par intervalle, par lieu",
           "SELECT $__timeGroupAlias(d.ts, $__interval), CASE st.location WHEN 'LAN' THEN 'Sur place (LAN)' WHEN 'Online' THEN 'À distance (online)' ELSE 'Inconnu' END AS metric, sum(d.gain) AS value "
           "FROM streamer_sample_v d JOIN streamer_v st USING (twitch_id) "
           "WHERE $__timeFilter(d.ts) AND NOT d.derived AND d.gain IS NOT NULL AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           12, 24, unit="currencyEUR", bars=True, stack=True, min_interval="5m"),

        row("Sur place vs à distance", 33),
        ts("Streamers en live par lieu",
           "SELECT s.ts AS time, CASE st.location WHEN 'LAN' THEN 'Sur place (LAN)' WHEN 'Online' THEN 'À distance (online)' ELSE 'Inconnu' END AS metric, count(*) FILTER (WHERE s.online) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           0, 34, unit="sishort", stack=True),
        ts("Viewers par lieu",
           "SELECT s.ts AS time, CASE st.location WHEN 'LAN' THEN 'Sur place (LAN)' WHEN 'Online' THEN 'À distance (online)' ELSE 'Inconnu' END AS metric, sum(s.viewers) AS value "
           + by_loc + " GROUP BY 1, 2 ORDER BY 1",
           12, 34, unit="sishort", stack=True),

        row("Jeux", 43),
        ts("Streamers en live par jeu",
           "WITH top AS (SELECT game FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY game ORDER BY count(*) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Autre' END AS metric, count(*) AS value FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           0, 44, unit="sishort", stack=True),
        ts("Viewers par jeu",
           "WITH top AS (SELECT game FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY game ORDER BY sum(viewers) DESC LIMIT 8) SELECT ts AS time, CASE WHEN game IN (SELECT game FROM top) THEN game ELSE 'Autre' END AS metric, sum(viewers) AS value FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) WHERE $__timeFilter(s.ts) AND s.online AND " + LOC + " GROUP BY 1, 2 ORDER BY 1",
           12, 44, unit="sishort", stack=True),
        table("Heures de stream par jeu",
              "SELECT coalesce(x.game, '(sans jeu)') AS \"Jeu\", sum(" + DT + ") / 3600.0 AS \"Heures\", "
              "       count(DISTINCT x.twitch_id) AS \"Streamers\", sum(x.viewers * " + DT + ") / 3600.0 AS \"Viewer-heures\" "
              "FROM streamer_sample_v x JOIN streamer_v st USING (twitch_id) "
              "WHERE $__timeFilter(x.ts) AND NOT st.derived AND " + LOC + " AND x.online AND x.gap_s IS NOT NULL "
              "GROUP BY 1 ORDER BY 2 DESC LIMIT 25",
              0, 53, w=24, h=9, hour_cols=("Heures",)),

        row("Dons sans streamer", 62),
        ts("Au fil du temps (total moins tous les streamers)",
           'SELECT sn.ts AS time, sn.donation_total - sum(s.donation_total) FILTER (WHERE NOT s.derived) AS "Sans streamer", '
           'coalesce(sum(s.donation_total) FILTER (WHERE s.derived), 0) AS "Cagnotte spéciale du Vieux Monsieur" '
           'FROM snapshot sn JOIN streamer_sample_v s USING (ts) '
           'WHERE $__timeFilter(sn.ts) GROUP BY sn.ts, sn.donation_total ORDER BY 1',
           0, 63, unit="currencyEUR", description=MIRROR_NOTE),
        ts("Par intervalle (gain total moins gains des streamers)",
           'SELECT $__timeGroupAlias(ts, $__interval), sum(g) - sum(sg) AS "Sans streamer" FROM ('
           f'  SELECT ts, {gain_expr("donation_total")} AS g FROM snapshot WHERE $__timeFilter(ts)'
           ') gl JOIN ('
           '  SELECT ts, sum(gain) AS sg FROM streamer_sample_v WHERE NOT derived AND $__timeFilter(ts) GROUP BY ts'
           ') st USING (ts) WHERE g IS NOT NULL GROUP BY 1 ORDER BY 1',
           12, 63, unit="currencyEUR", bars=True, legend=False, min_interval="5m", description=MIRROR_NOTE),
    ]


# ---------------------------------------------------------------------------------------------------
# Hero header of the streamer dashboard: an HTML text panel fed by hidden query variables that follow the
# Streamer filter (chained variables re-run when $streamer changes, and on every refresh).
HERO_CUR = ("WITH cur AS (SELECT s.twitch_id, s.online, s.game, s.viewers FROM streamer_sample_v s "
            "WHERE s.ts = (SELECT max(ts) FROM snapshot) AND s.twitch_id IN ($streamer) AND NOT s.derived "
            "ORDER BY s.donation_total DESC LIMIT 1) ")
HERO_VARS = {
    "hero_display": "SELECT st.display FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_login": "SELECT st.login FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_avatar": "SELECT coalesce(st.profile_url, '') FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_location": "SELECT CASE st.location WHEN 'LAN' THEN 'Sur place au ZEVENT' WHEN 'Online' THEN "
                     "'En stream à distance' ELSE '' END FROM cur JOIN streamer_v st USING (twitch_id)",
    "hero_status": "SELECT CASE WHEN cur.online THEN 'LIVE' ELSE 'HORS LIGNE' END FROM cur",
    "hero_color": "SELECT CASE WHEN cur.online THEN '#3fb950' ELSE '#8b949e' END FROM cur",
    "hero_game": "SELECT CASE WHEN cur.online THEN coalesce(cur.game, '') ELSE '' END FROM cur",
}


def hidden_var(name, sql):
    return {"name": name, "type": "query", "datasource": DS, "query": HERO_CUR + sql, "definition": name,
            "hide": 2, "refresh": 2, "multi": False, "includeAll": False, "sort": 0, "current": {}}


HERO_HTML = """
<div style="display:flex;align-items:center;gap:28px;height:100%;padding:8px 12px">
  <a href="https://twitch.tv/${hero_login}" target="_blank" rel="noopener">
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
      <span style="font-weight:500;opacity:.9;margin-left:8px">${hero_game}</span>
    </div>
  </div>
</div>
"""


def text_panel(html, x, y, w, h):
    global _id
    _id += 1
    return {"id": _id, "type": "text", "title": "", "transparent": True, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "html", "content": html.strip()}}


# ZEVENT streamer: one or a few streamers in detail. The Streamer filter has no "All" and lists streamers
# by donations, so the dashboard opens on the current leader.
def streamer_panels():
    reset_ids()
    sel = ("FROM streamer_sample_v s JOIN streamer_v st USING (twitch_id) "
           "WHERE NOT st.derived AND s.twitch_id IN ($streamer)")
    return [
        text_panel(HERO_HTML, 0, 0, 9, 12),
        stat("Cagnotte perso",
             f"SELECT coalesce(sum(s.donation_total), 0) {sel} AND s.ts = (SELECT max(ts) FROM snapshot)",
             9, w=5, unit="currencyEUR", decimals=2),
        stat("Rank",
             "SELECT min(rank) FROM streamer_sample_v WHERE ts = (SELECT max(ts) FROM snapshot) AND NOT derived "
             "AND twitch_id IN ($streamer)",
             14, w=5, unit="sishort", color="yellow",
             description="Position au classement des dons au dernier relevé (la meilleure des streamers sélectionnés)."),
        stat("Viewers actuels",
             f"SELECT coalesce(sum(s.viewers), 0) {sel} AND s.ts = (SELECT max(ts) FROM snapshot)",
             19, w=5, unit="sishort", color="purple"),
        stat("Gagné sur la période",
             "WITH " + GAIN_CTE + "SELECT coalesce(sum(g.gained), 0) FROM g JOIN streamer_v st USING (twitch_id) "
             "WHERE NOT st.derived AND g.twitch_id IN ($streamer)",
             9, w=5, y=4, unit="currencyEUR", decimals=2, color="orange"),
        stat("Heures de stream", hours_streamed_sql("true"), 14, w=5, y=4, unit="suffix: h", decimals=1, color="green",
             description=HOURS_DESCRIPTION),
        stat("Pic de viewers",
             f"SELECT coalesce(max(v), 0) FROM (SELECT s.ts, sum(s.viewers) AS v {sel} AND $__timeFilter(s.ts) GROUP BY s.ts) x",
             19, w=5, y=4, unit="sishort", color="purple", description="Plus grand nombre de viewers cumulés sur la période sélectionnée."),

        # third row of tiles next to the hero, y=8
        stat("Part du total de l'événement",
             f"SELECT coalesce(sum(s.donation_total), 0) / nullif((SELECT donation_total FROM snapshot ORDER BY ts DESC LIMIT 1), 0) "
             f"{sel} AND s.ts = (SELECT max(ts) FROM snapshot)",
             9, w=5, y=8, unit="percentunit", decimals=2, color="blue",
             description="Compteurs des streamers sélectionnés divisés par le total de l'événement, au dernier relevé."),
        stat("Viewers moyens en live",
             f"SELECT sum(x.viewers * {DT}) / nullif(sum({DT}) FILTER (WHERE x.online), 0) "
             "FROM streamer_sample_v x WHERE $__timeFilter(x.ts) AND x.twitch_id IN ($streamer) AND x.gap_s IS NOT NULL",
             14, w=5, y=8, unit="sishort", decimals=0, color="purple",
             description="Viewer-heures divisées par les heures en live, sur la période et les streamers sélectionnés."),
        stat("Viewer-heures", viewer_hours_sql("true"), 19, w=5, y=8, unit="sishort", decimals=1, color="purple",
             description=VIEWER_HOURS_DESCRIPTION),

        # fourth row of tiles, full width, y=12
        stat("Dans cet état depuis",
             "WITH cur AS ("
             "  SELECT twitch_id, ts, online, game FROM streamer_sample_v"
             "  WHERE ts = (SELECT max(ts) FROM snapshot) AND twitch_id IN ($streamer) AND NOT derived"
             "  ORDER BY donation_total DESC LIMIT 1"
             "), changed AS ("
             "  SELECT max(s.ts) AS at FROM cur c JOIN streamer_sample_v s USING (twitch_id)"
             "  WHERE s.online <> c.online OR s.game IS DISTINCT FROM c.game"
             ") "
             "SELECT extract(epoch FROM c.ts - coalesce(ch.at, (SELECT first_seen FROM streamer_v st WHERE st.twitch_id = c.twitch_id))) "
             "FROM cur c, changed ch",
             0, w=12, y=12, unit="dtdhms", decimals=0, color="orange",
             description="Depuis combien de temps le streamer affiché est dans son état actuel (live ou hors ligne) avec le jeu actuel."),
        stat("Dons par viewer-heure", per_viewer_hour_sql("true"), 12, w=12, y=12, unit="currencyEUR", decimals=2,
             color="green", description=PER_VIEWER_HOUR_DESCRIPTION),

        ts("Dons au fil du temps",
           'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.donation_total AS value '
           f'{sel} AND $__timeFilter(s.ts) ORDER BY 1',
           0, 16, unit="currencyEUR", streamer_links=True),
        ts("Viewers au fil du temps",
           'SELECT s.ts AS time, st.display AS metric, st.login AS login, s.viewers AS value '
           f'{sel} AND $__timeFilter(s.ts) ORDER BY 1',
           12, 16, unit="sishort", streamer_links=True),
        ts("Dons gagnés par intervalle",
           'SELECT $__timeGroupAlias(d.ts, $__interval), st.display AS metric, st.login AS login, sum(d.gain) AS value '
           'FROM streamer_sample_v d JOIN streamer_v st USING (twitch_id) '
           'WHERE $__timeFilter(d.ts) AND d.twitch_id IN ($streamer) AND d.gain IS NOT NULL GROUP BY 1, 2, 3 ORDER BY 1',
           0, 25, unit="currencyEUR", bars=True, stack=True, min_interval="5m", streamer_links=True),
        ts("Rank au classement des dons au fil du temps",
           "SELECT r.ts AS time, st.display AS metric, st.login AS login, r.rank AS value "
           "FROM streamer_sample_v r JOIN streamer_v st USING (twitch_id) "
           "WHERE $__timeFilter(r.ts) AND r.twitch_id IN ($streamer) AND r.rank IS NOT NULL ORDER BY 1",
           12, 25, unit="sishort", streamer_links=True, description="1 est la première place ; plus c'est bas, mieux c'est."),
        game_timeline(0, 34),
    ]


def game_timeline(x, y):
    """State timeline of the games played by the selected streamers, one colour per game."""
    sql = LIVE_SQL.format(loc="true", filter=" AND s.twitch_id IN ($streamer)")
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

# First datapoint until now; grows as data arrives. Data before 2026-09-04 20:58 UTC is backfilled from
# third-party sources (raw-backfill/, see zevent_tracker/external.py); its first tick is 17:01 UTC.
TIME_RANGE = {"from": "2026-09-03T17:01:00.000Z", "to": "now"}

DASHBOARDS = [  # (uid, button title)
    ("zevent-public", "Stats principales"),
    ("zevent-live-public", "Timeline live"),
    ("zevent-insights-public", "Analyses"),
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
  SELECT s.ts, s.twitch_id, st.display, CASE WHEN s.online THEN coalesce(s.game, '(sans jeu)') END AS state
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
        stat(f"Streamers en live ({scope})",
             f'SELECT count(*) FILTER (WHERE s.online) AS "En live", count(*) AS "Total" {latest} '
             "AND s.ts = (SELECT max(ts) FROM snapshot)",
             0, w=4, color="blue", text_mode="value_and_name"),
        stat(f"Pic de streamers en live ({scope})",
             f"SELECT max(n) FROM (SELECT count(*) AS n {latest} AND s.online GROUP BY s.ts) x",
             4, w=4, unit="sishort", color="blue"),
        stat("Heures de stream", hours_streamed_sql(loc), 8, w=4, unit="suffix: h", decimals=1, color="green",
             description=HOURS_DESCRIPTION),
        ts(f"Streamers en live au fil du temps ({scope})",
           f'SELECT s.ts AS time, count(*) FILTER (WHERE s.online) AS "En live" {latest} AND $__timeFilter(s.ts) '
           "GROUP BY 1 ORDER BY 1",
           12, 0, w=12, h=4, unit="sishort", legend=False),
        # paginated, follows the Location and Streamer filters
        live_timeline(f"Streamers ({scope}, $streamer)", 4, 34, loc, filtered=True, per_page=50,
                      description=LIVE_DESCRIPTION + " Suit les filtres Lieu et Streamer."),
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
write(dashboard_base("zevent-insights-public", "ZEVENT analyses", [LOCATION_VAR], insights_panels()))
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
