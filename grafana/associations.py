"""The 2026 beneficiaries and how the total is split between them.

zevent.fr says the Fondation de France collects the 2026 donations and hands them to the 22 associations
"selon les répartitions des 9 éditions précédentes": the total is split equally between the nine previous
editions (2016 to 2025, no edition in 2023), and each edition's ninth is split equally between the
beneficiaries of that edition. In 2025 the four children's health associations (POLE_ENFANCE) were one
beneficiary, the "Pôle enfance" collective, alongside four others: the collective's fifth is split in four.
The SeaCleaners, a 2022 beneficiary, is not among the 22 (the association was wound up), so the 2022 ninth
goes to the three others.

The associations themselves (name, edition, official site, logo, description) come from zevent.fr and are
kept in associations.json, written by pull_associations.py.
"""
import json
from pathlib import Path

ASSOCIATIONS_JSON = Path(__file__).parent / "associations.json"
PREVIOUS_EDITIONS = 9
POLE_ENFANCE = {"Le Rire Médecin", "Sourire à la Vie", "Sparadrap", "L'envol"}
POLE_ENFANCE_NAME = "Pôle enfance"


def load():
    return json.loads(ASSOCIATIONS_JSON.read_text())


def shares(associations, editions=PREVIOUS_EDITIONS):
    """One row per beneficiary (entries without an edition, the collector, are left out) with `parts`, the
    number of equal parts the total is cut into for it (its share is 1/parts, also given as `weight`), plus
    `beneficiaries` (of its edition, a collective counting once) and `group` (the collective's name, or None)."""
    def group(a):
        return POLE_ENFANCE_NAME if a["name"] in POLE_ENFANCE else None

    per_edition = {}
    for a in associations:
        if a["edition"]:
            per_edition.setdefault(a["edition"], set()).add(group(a) or a["name"])
    members = sum(1 for a in associations if group(a))
    out = []
    for a in associations:
        if not a["edition"]:
            continue
        n = len(per_edition[a["edition"]])
        parts = editions * n * (members if group(a) else 1)
        out.append({**a, "group": group(a), "beneficiaries": n, "parts": parts, "weight": 1 / parts})
    return out
