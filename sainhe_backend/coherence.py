"""Contrôles de cohérence d'un rapport — complètent le recoupement avec une seconde source.

1. identity_checks : identités comptables que tout jeu de comptes doit respecter, quelle que soit
   la source (XBRL ou Yahoo) : actif ≈ passif + capitaux propres, marge brute = CA − coût des
   ventes, résultat net ≈ BPA dilué × actions diluées, trésorerie ≤ actif courant ≤ actif total.
   C'est le seul contrôle possible pour les sociétés en source secondaire (Suisse, Japon…).
   Une identité calculée à partir de la valeur qu'elle teste (tag « derived: ») est sautée.
2. esef_cross_check : quand une société ESEF incomplète bascule sur Yahoo (Leonardo, Coats),
   les chiffres ESEF des mêmes exercices sont gardés et comparés : vraie seconde source.
3. net_debt_tag_audit : chaque composante de la dette nette — publiée pour le dernier exercice ?
   avec quel tag ? Une composante publiée les années précédentes mais pas la dernière signale une
   migration de tag (Deere 2022 : dette LT passée sur un élément propre à la société).
Fonctions pures (rapport → liste de constats), testées sans réseau.
"""
from __future__ import annotations
import config


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _series(report: dict, name: str) -> dict[str, float]:
    s = (report.get("drill_down", {}).get("three_statement_10y", {}) or {}).get(name, {}) or {}
    return {str(y): v for y, v in s.items() if _num(v) is not None}


def _tag(report: dict, name: str) -> str:
    return str(((report.get("drill_down", {}).get("source_map_per_tag", {}) or {}).get(name) or {}).get("xbrl_tag") or "")


def _check(name: str, year: str, lhs: float, rhs: float, tol: float, note: str = "") -> dict:
    gap = abs(lhs - rhs) / abs(rhs) if rhs else (0.0 if lhs == rhs else float("inf"))
    return {"check": name, "year": year, "lhs": lhs, "rhs": rhs, "gap_pct": round(gap * 100, 2),
            "tolerance_pct": round(tol * 100, 2), "status": "ok" if gap <= tol else "gap", "note": note}


def identity_checks(report: dict, years: int | None = None) -> list[dict]:
    """Identités comptables sur les `years` derniers exercices (config.COHERENCE_YEARS)."""
    years = years or config.COHERENCE_YEARS
    tol = config.COHERENCE_TOL
    s = {n: _series(report, n) for n in ("total_assets", "total_liabilities", "equity", "minority_interest", "revenue", "cogs",
                                         "gross_profit", "net_income", "eps_diluted", "shares_diluted",
                                         "cash", "current_assets")}
    yrs = sorted(s["revenue"] or s["total_assets"])[-years:]
    out = []
    for y in yrs:
        a, l, e = s["total_assets"].get(y), s["total_liabilities"].get(y), s["equity"].get(y)
        if None not in (a, l, e) and not _tag(report, "total_liabilities").startswith("derived:"):
            e = e + (s["minority_interest"].get(y) or 0.0)      # intérêts minoritaires publiés
            out.append(_check("assets = liabilities + equity", y, l + e, a, tol["balance_sheet"],
                              "gap up to the tolerance = minority interests and mezzanine items"))
        r, c, g = s["revenue"].get(y), s["cogs"].get(y), s["gross_profit"].get(y)
        if None not in (r, c, g):
            out.append(_check("gross profit = revenue − cost of revenue", y, r - c, g, tol["gross_profit"]))
        ni, eps, sh = s["net_income"].get(y), s["eps_diluted"].get(y), s["shares_diluted"].get(y)
        if None not in (ni, eps, sh) and not _tag(report, "shares_diluted").startswith("derived:"):
            out.append(_check("net income ≈ diluted EPS × diluted shares", y, eps * sh, ni, tol["eps"]))
        cash, ca = s["cash"].get(y), s["current_assets"].get(y)
        if None not in (cash, ca, a):
            ok = 0 <= cash <= ca <= a
            out.append({"check": "cash ≤ current assets ≤ total assets", "year": y, "lhs": cash, "rhs": a,
                        "gap_pct": 0.0, "tolerance_pct": 0.0, "status": "ok" if ok else "gap", "note": ""})
    return out


def esef_cross_check(report: dict) -> list[dict]:
    """Yahoo (retenu) vs ESEF (conservé) sur les exercices communs — format des recoupements."""
    overlap = report.get("drill_down", {}).get("esef_overlap") or {}
    out = []
    for field, by_year in overlap.items():
        mine = _series(report, field)
        for y, v in (by_year or {}).items():
            y = str(y)
            if _num(v) is None or mine.get(y) is None or not v:
                continue
            gap = abs(mine[y] - v) / abs(v)
            out.append({"field": field, "fiscal_end": y, "xbrl": v, "xbrl_tag": "ESEF", "second_source": "Yahoo statements",
                        "second_source_row": field, "second_value": mine[y], "gap_pct": round(gap * 100, 2),
                        "tolerance_pct": round(config.CROSS_CHECK_TOL * 100, 2),
                        "status": "gap" if gap > config.CROSS_CHECK_TOL else "ok"})
    return out


def net_debt_tag_audit(report: dict) -> list[dict]:
    """Composantes de la dette nette du dernier exercice : valeur, tag, et trou de publication."""
    dd = report.get("drill_down", {}) or {}
    fy = str(report.get("fiscal_year"))
    det = dd.get("net_debt_detail", {}) or {}
    out = []
    for comp in ("lt_debt", "st_debt", "cash", "st_investments"):
        s = _series(report, comp)
        last = max(s) if s else None
        if not s:
            status = "never_filed"           # jamais publié : compté 0 (dette) / non déduit (placements)
        elif fy in s:
            status = "ok"
        elif last and fy.isdigit() and int(last) >= int(fy) - config.NET_DEBT_TAG_GAP_YEARS:
            status = "gap"                   # publié récemment, pas cette année : migration de tag probable
        else:
            status = "discontinued"          # la société a cessé d'en publier il y a longtemps
        out.append({"component": comp, "fiscal_year": fy, "value": _num(det.get(comp)),
                    "tag": _tag(report, comp) if fy in s else "", "last_filed_year": last, "status": status})
    if _num(det.get("net_debt")) is None:
        out.append({"component": "net_debt", "fiscal_year": fy, "value": None, "tag": "",
                    "last_filed_year": None, "status": "gap"})
    return out
