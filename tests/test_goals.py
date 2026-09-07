import httpx

from zevent_tracker.goals import fetch, rows, to_sql

PART = [
    {"participation_id": "p1", "name": "Foo", "location": "lan", "twitch_id": "1", "twitch_login": "foo",
     "goals": [
         {"id": "g1", "name": "Le MICRO", "amount": 100, "category": "donation", "accomplished": True, "reached": True, "links": []},
         {"id": "g2", "name": "J'ouvre un \"truc\" ; DROP", "amount": 690050, "category": "donation_equal",
          "accomplished": False, "reached": False, "links": ["https://a/b?x=1", "https://c/d'e"]},
     ]},
    {"participation_id": "p2", "name": "No twitch", "location": "remote", "twitch_id": None, "twitch_login": None,
     "goals": [{"id": "g3", "name": "x", "amount": 1, "category": "donation", "accomplished": False, "reached": False, "links": []}]},
    {"participation_id": "p3", "name": "Clara", "location": "remote", "twitch_id": "999", "twitch_login": "Clara_Jones",
     "goals": [{"id": "g4", "name": "x", "amount": 1, "category": "donation", "accomplished": False, "reached": False, "links": []}]},
    {"participation_id": "p4", "name": "Empty", "location": "remote", "twitch_id": "2", "twitch_login": "empty", "goals": None},
]


def test_rows_convert_cents_keep_site_order_and_skip_unidentified_or_excluded():
    r = rows(PART)
    assert [x[0] for x in r] == ["g1", "g2"]           # g3 has no twitch id, g4 is clara_jones (excluded), p4 has no goals
    assert r[0][1:4] == ("1", 1, 1.0)
    assert r[1][2:4] == (2, 6900.5)
    assert r[1][6:9] == (False, False, ["https://a/b?x=1", "https://c/d'e"])


def test_sql_is_one_transaction_with_escaped_literals():
    sql = to_sql(PART, fetched_at="2026-09-07T10:00:00Z")
    assert sql.startswith("-- Donation goals")
    assert "2026-09-07T10:00:00Z" in sql
    assert sql.index("BEGIN;") < sql.index("CREATE TABLE IF NOT EXISTS donation_goal") < sql.index("TRUNCATE donation_goal;")
    assert sql.index("TRUNCATE donation_goal;") < sql.index("INSERT INTO donation_goal") < sql.index("COMMIT;")
    assert "('g1', '1', 1, 1.00, 'Le MICRO', 'donation', true, true, '{}'::text[])" in sql
    assert """('g2', '1', 2, 6900.50, 'J''ouvre un "truc" ; DROP', 'donation_equal', false, false, ARRAY['https://a/b?x=1', 'https://c/d''e']::text[])""" in sql
    assert "-- 2 goals, 1 streamers." in sql


def test_sql_without_goals_still_creates_and_empties_the_table():
    sql = to_sql([])
    assert "INSERT" not in sql and "TRUNCATE donation_goal;" in sql and sql.rstrip().endswith("COMMIT;")


def test_fetch_walks_overview_then_each_participation():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/donation_goals/overview"):
            return httpx.Response(200, json=[
                {"id": "p1", "name": "Foo", "location": "lan", "socials": {"twitch": {"id": 1, "login": "foo"}}},
                {"id": "p2", "name": "Bar", "location": "remote", "socials": {}},
            ])
        return httpx.Response(200, json=[{"id": "g-" + req.url.path.split("/")[2], "name": "n", "amount": 100,
                                          "category": "donation", "accomplished": False, "reached": True, "links": []}])

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x") as c:
        out = fetch("ev", api="https://x", pause=0, client=c)
    assert calls == ["/events/ev/donation_goals/overview", "/participations/p1/donation_goals", "/participations/p2/donation_goals"]
    assert out[0]["twitch_id"] == "1" and out[0]["twitch_login"] == "foo" and out[0]["goals"][0]["id"] == "g-p1"
    assert out[1]["twitch_id"] is None
