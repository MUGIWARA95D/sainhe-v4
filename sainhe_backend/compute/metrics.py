"""Couche 2 — Métriques dérivées + reformulation du bilan (.txt MÉTRIQUES STANDARD,
REFORMULATION DU BILAN, NOTE ASC 842, FCF AJUSTÉ SBC).

Toutes les fonctions retournent None si un input manque ("N/D" propagé, jamais d'imputation).
"""
from __future__ import annotations
import config
from edgar.xbrl_parser import CompanyData


def _nz(x, default=0.0):
    """None → default (uniquement pour les composants OPTIONNELS, ex: leases absents pré-2019)."""
    return default if x is None else x


def effective_tax(cd: CompanyData, fy: int | None) -> dict:
    """Taux d'impôt retenu pour l'exercice fy : {rate, source, raw, years}.
    source = "year" (taux de l'exercice, dans TAX_RATE_VALID_RANGE), "median" (médiane des
    exercices normaux précédents : le taux de l'année est aberrant ou la société est en perte)
    , "cash" (impôt payé en trésorerie / résultat avant impôt, même dépôt, si aucun exercice
    normal ne suffit) ou "N/D". Jamais de taux légal ni de moyenne sectorielle : uniquement les dépôts de la société."""
    lo, hi = config.TAX_RATE_VALID_RANGE

    def rate(y):
        tax, pre = cd.value("tax_expense", y), cd.value("pretax_income", y)
        if tax is None or pre is None or pre <= 0:
            return None
        return tax / pre

    tax, pre = cd.value("tax_expense", fy), cd.value("pretax_income", fy)
    raw = (tax / pre) if (tax is not None and pre) else None
    cur = rate(fy)
    if cur is not None and lo <= cur <= hi:
        return {"rate": cur, "source": "year", "raw": raw, "years": [fy]}
    prev = [y for y in (cd.years("pretax_income") or []) if fy is not None and y < fy][-config.TAX_RATE_MEDIAN_YEARS:]
    ok = [(y, rate(y)) for y in prev]
    ok = [(y, r) for y, r in ok if r is not None and lo <= r <= hi]
    if len(ok) >= config.TAX_RATE_MIN_YEARS:
        vals = sorted(r for _, r in ok)
        m = len(vals) // 2
        med = vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2
        return {"rate": med, "source": "median", "raw": raw, "years": [y for y, _ in ok]}
    paid = cd.value("income_taxes_paid", fy)
    if getattr(config, "TAX_RATE_CASH_FALLBACK", False) and paid is not None and pre and pre > 0 \
            and lo <= paid / pre <= hi:
        return {"rate": paid / pre, "source": "cash", "raw": raw, "years": [fy], "paid": paid}
    return {"rate": None, "source": "N/D", "raw": raw, "years": []}


def _div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


# ------------------------------------------------------------------ Net Debt ASC 842
def _debt_component(cd: CompanyData, name: str, fy: int | None):
    """Composant de dette pour l'année fy.
    - publié cette année → valeur ;
    - jamais publié par la société (aucune année) → 0 (pas de dette de ce type déclarée,
      ex. ISRG/GEV sans dette LT) ;
    - publié d'autres années mais PAS cette année → None (N/D) : tag migré vers un tag
      propre à la société (ex. DE `de:` non exposé par l'API SEC). Avant : traité comme 0
      → dette nette sous-estimée sans avertissement.
    """
    if not cd.series.get(name):
        return 0.0
    return cd.value(name, fy)


def net_debt_detail(cd: CompanyData, fy: int | None = None) -> dict:
    """Composants de la dette nette pour l'exercice fy (affichés tels quels sur la page).
    Net Debt = LT Debt + ST Debt + FinanceLeaseLiability − Cash − placements à court terme
    (.txt NOTE ASC 842). OperatingLeaseLiability est EXCLU (obligation opérationnelle).
    Règles N/D, volontairement ASYMÉTRIQUES (21 nov. 2026) :
      - composant de DETTE ou cash publié d'autres années mais pas cette année → dette nette N/D
        (le compter à 0 sous-estimerait la dette : erreur dans le sens agressif) ;
      - placements CT non publiés cette année → NON déduits, et signalé
        (st_investments_deducted=False) : ne pas déduire un actif ne peut que SURESTIMER la dette
        nette (sens prudent). Avant le 21 nov. ils n'étaient jamais déduits.
    Tags US GAAP seulement (ShortTermInvestments, MarketableSecuritiesCurrent,
    OtherShortTermInvestments) : pas d'équivalent IFRS homogène → non déduits pour les IFRS.
    """
    if fy is None:
        fy = (cd.years("revenue") or cd.years("net_income") or [None])[-1]
    lt, st = _debt_component(cd, "lt_debt", fy), _debt_component(cd, "st_debt", fy)
    cash = _debt_component(cd, "cash", fy)
    sti = cd.value("st_investments", fy)
    # Si la dette LT vient d'un tag incluant déjà les finance leases, ne pas les rajouter
    lt_dp = cd.series.get("lt_debt", {}).get(fy)
    fl = 0.0 if (lt_dp and ("CapitalLease" in lt_dp.tag or "FinanceLease" in lt_dp.tag)) \
        else _nz(cd.value("finance_lease_liab", fy))
    nd = None if (lt is None or st is None or cash is None) else lt + st + fl - cash - (sti or 0.0)
    return {"fy": fy, "lt_debt": lt, "st_debt": st, "finance_leases": fl, "cash": cash,
            "st_investments": sti, "st_investments_deducted": sti is not None, "net_debt": nd}


def net_debt(cd: CompanyData, fy: int | None = None):
    """Dette nette de l'exercice (voir net_debt_detail pour les règles et les composants)."""
    return net_debt_detail(cd, fy)["net_debt"]


# ------------------------------------------------------------------ Reformulation Penman
def reformulate(cd: CompanyData, fy: int | None = None) -> dict:
    """OA/FA/OO/FO → NOA, NFO (.txt REFORMULATION DU BILAN).
    Proxy par défaut : FA = Cash + ST investments ; OA = Total Assets − FA.
    OO = Total Liabilities − FO ; FO = dettes financières + finance leases.
    OperatingLeaseLiability reste dans OO (ASC 842).
    """
    ta, tl = cd.value("total_assets", fy), cd.value("total_liabilities", fy)
    if ta is None or tl is None:
        return {"NOA": None, "NFO": None, "OA": None, "FA": None, "OO": None, "FO": None,
                "equity_check": None}
    fa = _nz(cd.value("cash", fy)) + _nz(cd.value("st_investments", fy))
    fo = _nz(cd.value("lt_debt", fy)) + _nz(cd.value("st_debt", fy)) \
        + _nz(cd.value("finance_lease_liab", fy))
    oa, oo = ta - fa, tl - fo
    noa, nfo = oa - oo, fo - fa
    eq = cd.value("equity", fy)
    return {"OA": oa, "FA": fa, "OO": oo, "FO": fo, "NOA": noa, "NFO": nfo,
            "equity_check": (None if eq is None else round(noa - nfo - eq, 2))}


# ------------------------------------------------------------------ Bloc métriques annuelles
def derive(cd: CompanyData, fy: int | None = None) -> dict:
    """Toutes les métriques dérivées du .txt pour une année donnée."""
    fys = cd.years("revenue") or cd.years("net_income")
    if not fys:
        return {}
    fy = fy or fys[-1]

    rev = cd.value("revenue", fy)
    ebit = cd.value("ebit", fy)
    da = cd.value("dep_amort", fy)
    ni = cd.value("net_income", fy)
    ocf = cd.value("ocf", fy)
    capex = cd.value("capex", fy)
    sbc = cd.value("sbc", fy)
    tax_info = effective_tax(cd, fy)
    t_eff = tax_info["rate"]
    nd = net_debt(cd, fy)
    eq = cd.value("equity", fy)
    interest = cd.value("interest_expense", fy)

    ebitda = None if (ebit is None or da is None) else ebit + da
    nopat = None if (ebit is None or t_eff is None) else ebit * (1 - t_eff)
    invested = None if (eq is None or nd is None) else eq + nd
    fcf_brut = None if (ocf is None or capex is None) else ocf - capex
    # FCF AJUSTÉ SBC — valorisations basées sur celui-ci UNIQUEMENT (.txt)
    fcf_adj = None if fcf_brut is None else fcf_brut - _nz(sbc)
    # Owner Earnings = OCF − D&A(proxy maintenance capex) − SBC (.txt Buffett + SBC)
    owner_earnings = None if (ocf is None or da is None) else ocf - da - _nz(sbc)

    ca, cl = cd.value("current_assets", fy), cd.value("current_liabilities", fy)
    inv, ar, ap = cd.value("inventory", fy), cd.value("ar", fy), cd.value("ap", fy)
    cogs = cd.value("cogs", fy)
    gp = cd.value("gross_profit", fy)
    if gp is None and rev is not None and cogs is not None:
        gp = rev - cogs

    dso = _div(ar, rev) and _div(ar, rev) * 365
    dio = _div(inv, cogs) and _div(inv, cogs) * 365
    dpo = _div(ap, cogs) and _div(ap, cogs) * 365

    total_debt = _nz(cd.value("lt_debt", fy)) + _nz(cd.value("st_debt", fy)) or None

    out = {
        "fy": fy,
        "ebitda": ebitda,
        "net_debt": nd,
        "nopat": nopat,
        "effective_tax_rate": t_eff,
        "tax_rate_detail": tax_info,
        "invested_capital": invested,
        "roic": _div(nopat, invested),
        "kd": _div(interest, total_debt),
        "interest_coverage": _div(ebit, interest),
        "gross_margin": _div(gp, rev),
        "op_margin": _div(ebit, rev),
        "nopat_margin": _div(nopat, rev),
        "fcf_brut": fcf_brut,
        "fcf_adj": fcf_adj,
        "sbc": sbc,
        "sbc_pct_fcf": _div(sbc, fcf_brut),
        "owner_earnings": owner_earnings,
        "fcf_margin": _div(fcf_adj, rev),
        "asset_turnover": _div(rev, cd.value("total_assets", fy)),
        "current_ratio": _div(ca, cl),
        "quick_ratio": _div(None if ca is None else ca - _nz(inv), cl),
        "de_ratio": _div(total_debt, eq),
        "net_debt_ebitda": _div(nd, ebitda),
        "dso": dso, "dio": dio, "dpo": dpo,
        "ccc": (dso + dio - dpo) if None not in (dso, dio, dpo) else None,
        "ocf_ni": _div(ocf, ni),
        "accruals": None if (ni is None or ocf is None) else ni - ocf,
        "accruals_pct_rev": _div(None if (ni is None or ocf is None) else ni - ocf, rev),
        # Fonds propres ≤ 0 (rachats massifs : LMT, années de fonds propres négatifs) →
        # ROE sans signification (ex. −231 %) → N/D
        "roe": _div(ni, eq) if (eq is not None and eq > 0) else None,
        "reformulation": reformulate(cd, fy),
    }
    return out


def derive_history(cd: CompanyData, n: int = 10) -> dict[int, dict]:
    years = cd.years("revenue")[-n:] or cd.years("net_income")[-n:]
    return {y: derive(cd, y) for y in years}
