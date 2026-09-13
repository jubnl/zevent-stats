from grafana.associations import POLE_ENFANCE, shares


def _assoc(name, edition):
    return {"name": name, "edition": edition, "link": "https://x", "logo": "x.png", "html": "<p>x</p>"}


ASSOCS = [
    _assoc("Fondation de France", None),
    _assoc("Save the Children", 2016),
    _assoc("WWF", 2022), _assoc("LPO", 2022), _assoc("Sea Shepherd France", 2022),
    _assoc("Helebor", 2025), _assoc("Nightline", 2025), _assoc("La Ligue contre le Cancer", 2025),
    _assoc("Association Française des Aidants", 2025),
    *[_assoc(n, 2025) for n in sorted(POLE_ENFANCE)],
]


def test_shares_split_by_edition_then_by_beneficiary_with_the_pole_enfance_as_one():
    s = {r["name"]: r for r in shares(ASSOCS, editions=3)}
    assert "Fondation de France" not in s            # collects, does not receive
    assert s["Save the Children"]["weight"] == 1 / 3  # alone in 2016
    assert s["WWF"]["weight"] == 1 / 3 / 3            # three beneficiaries in 2022
    assert s["Helebor"]["weight"] == 1 / 3 / 5        # five beneficiaries in 2025: four + the collective
    assert s["Sparadrap"]["weight"] == 1 / 3 / 5 / 4  # a quarter of the collective's fifth
    assert s["Sparadrap"]["group"] == "Pôle enfance" and s["Helebor"]["group"] is None
    assert s["WWF"]["beneficiaries"] == 3 and s["Sparadrap"]["beneficiaries"] == 5
    assert s["WWF"]["parts"] == 9 and s["Sparadrap"]["parts"] == 60
    assert abs(sum(r["weight"] for r in s.values()) - 1) < 1e-12


def test_shares_default_to_the_nine_previous_editions():
    s = {r["name"]: r for r in shares(ASSOCS)}
    assert s["Save the Children"]["weight"] == 1 / 9
