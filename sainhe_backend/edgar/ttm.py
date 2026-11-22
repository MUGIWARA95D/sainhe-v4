"""Quatre derniers trimestres (TTM) à partir des 10-Q, pour les déposants 10-K.

Les flux des 10-Q sont publiés en CUMUL depuis le début de l'exercice (YTD). Pour une société
dont le dernier 10-K couvre l'exercice N et dont le dernier 10-Q couvre k trimestres de N+1 :
    TTM = exercice N + YTD(N+1, k trimestres) − YTD(N, mêmes k trimestres)
Les trois valeurs sont publiées par la société (le YTD de N figure en comparatif dans le 10-Q) :
rien n'est estimé. Une valeur manquante → la série TTM de ce poste est N/D.
NVIDIA (FY clos fin janvier 2026, 10-Q à fin juillet 2026) : la base « dernier exercice »
ignorait deux trimestres de croissance du FCF.
"""
from __future__ import annotations
import datetime

from edgar.xbrl_parser import TAG_MAP

FLOWS = ("revenue", "ocf", "capex", "sbc")
_TOL_DAYS = 10


def _d(s: str) -> datetime.date:
    return datetime.date.fromisoformat(str(s)[:10])


def _items(facts: dict, tag: str, unit: str) -> list[dict]:
    node = facts.get("facts", {}).get("us-gaap", {}).get(tag) or {}
    return list(node.get("units", {}).get(unit, []))


def compute(facts: dict, cd) -> dict | None:
    """{period_end, quarters_after_fy, values{revenue, ocf, capex, sbc}, fcf_adj, revenue_growth_vs_fy,
    accn, fy} ou None si aucun 10-Q postérieur au dernier exercice."""
    last = cd.latest("revenue") or cd.latest("ocf")
    if not last:
        return None
    unit = (cd.reporting_currency() or "USD")
    fy_end = _d(last.end)
    cur_start = fy_end + datetime.timedelta(days=1)
    prev_start = cur_start - datetime.timedelta(days=365)
    values, meta = {}, None
    for name in FLOWS:
        annual = cd.value(name, last.fy)
        got = None
        for tag in TAG_MAP.get(name, []):
            items = [x for x in _items(facts, tag, unit) if x.get("start") and x.get("end") and x.get("val") is not None]
            cur = [x for x in items if x.get("form") == "10-Q" and abs((_d(x["start"]) - cur_start).days) <= _TOL_DAYS
                   and _d(x["end"]) > fy_end]
            if not cur:
                continue
            c = max(cur, key=lambda x: (x["end"], x.get("accn", "")))
            target_end = _d(c["end"]) - datetime.timedelta(days=365)
            prev = [x for x in items if abs((_d(x["start"]) - prev_start).days) <= _TOL_DAYS
                    and abs((_d(x["end"]) - target_end).days) <= _TOL_DAYS]
            if not prev or annual is None:
                continue
            p = max(prev, key=lambda x: x.get("accn", ""))
            got = annual + float(c["val"]) - float(p["val"])
            if name == "revenue" or meta is None:
                months = round((_d(c["end"]) - cur_start).days / 30.4)
                meta = {"period_end": c["end"], "quarters_after_fy": max(1, round(months / 3)),
                        "accn": c.get("accn"), "fy": last.fy, "fy_end": last.end}
            break
        values[name] = got
    if not meta:
        return None
    ocf, capex, sbc = values.get("ocf"), values.get("capex"), values.get("sbc")
    fcf = None if (ocf is None or capex is None) else ocf - capex - (sbc or 0.0)
    rev, rev_fy = values.get("revenue"), cd.value("revenue", last.fy)
    return {**meta, "values": values, "fcf_adj": fcf,
            "sbc_deducted": sbc is not None,
            "revenue_growth_vs_fy": (rev / rev_fy - 1) if (rev and rev_fy) else None}
