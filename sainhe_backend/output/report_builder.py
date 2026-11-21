"""Couche 4 — Output normalisé (.txt Couche 4 + contrat JSON du prompt).
Un JSON par ticker, structuré EXACTEMENT selon les blocs front A→I,
plus valuation_map.json agrégé (Niveau 1, les 30 tickers).
Chaque chiffre traçable ; tag manquant → "N/D".
"""
from __future__ import annotations
import json, datetime
import config
from edgar.xbrl_parser import CompanyData
from compute import metrics, valuation, dupont, buffett, scores, external, segments
from compute.adjustments import adjust
from output.traceability import build_source_map, edgar_filings_index_url


def _nd(x):
    """None → 'N/D' pour le front (.txt garde-fous)."""
    if isinstance(x, dict):
        return {k: _nd(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_nd(v) for v in x]
    if isinstance(x, float):
        return round(x, 6)
    return "N/D" if x is None else x


def composite_score(blocks: dict) -> dict:
    """Score 0-100, pondérations config.SCORE_WEIGHTS AFFICHÉES (pas de black box, .txt).
    Chaque pilier noté 0-100 par règles simples et transparentes."""
    s = {}
    # valuation : EPV vs prix
    va = blocks["bloc_A_verdict"]
    price, epv_ps = va.get("prix"), va.get("EPV", {}).get("epv_per_share")
    if isinstance(price, (int, float)) and isinstance(epv_ps, (int, float)) and price > 0:
        upside = (epv_ps - price) / price
        s["valuation"] = max(0, min(100, 50 + upside * 200))   # +25% upside → 100
    else:
        s["valuation"] = 50
    # balance sheet health
    h = blocks["bloc_E_health"]
    pts = 50
    nde = h.get("net_debt_ebitda")
    if isinstance(nde, (int, float)):
        pts += 20 if nde < 1 else (10 if nde < 3 else (-20 if nde > 5 else 0))
    ic = h.get("interest_coverage")
    if isinstance(ic, (int, float)):
        pts += 15 if ic > 8 else (-15 if ic < 2 else 0)
    s["balance_sheet_health"] = max(0, min(100, pts))
    # earnings quality
    pts = 50
    ocfni = h.get("ocf_ni")
    if isinstance(ocfni, (int, float)):
        pts += 20 if ocfni > 1.1 else (-25 if ocfni < 0.7 else 0)
    flags = {f["id"] for f in blocks["bloc_B_flags"]}
    if "beneish_manipulation" in flags:
        pts -= 30
    s["earnings_quality"] = max(0, min(100, pts))
    # smart money (v1 neutre, Phase 6 affinera)
    s["smart_money"] = 50
    # momentum (rempli côté portfolio/yfinance ; neutre ici)
    s["momentum"] = 50

    total = sum(s[k] * w for k, w in config.SCORE_WEIGHTS.items())
    return {"value_0_100": round(total, 1), "pillars": s,
            "weights": config.SCORE_WEIGHTS,
            "note": "pondérations modifiables par l'utilisateur (.txt Couche 4)"}


def build_report(cd: CompanyData, price: float | None, yf_info: dict | None,
                 edgar_meta: dict, macro_signals: dict | None = None,
                 gics_sector: str | None = None,
                 price_history: dict[int, float] | None = None,
                 peer_block: dict | None = None, currency: str | None = None) -> dict:
    """currency = devise des COMPTES : fixe le taux sans risque du WACC (Bund pour l'EUR…)."""
    fy = (cd.years("revenue") or cd.years("net_income") or [None])[-1]
    m = metrics.derive(cd, fy)
    adj = adjust(cd, fy)
    shares = cd.value("shares_diluted", fy) or cd.value("shares_outstanding", fy)
    mcap = price * shares if (price and shares) else None
    beta = (yf_info or {}).get("beta")

    w = valuation.wacc(cd, price, beta=beta, currency=currency)
    epv_ = valuation.epv(cd, w["wacc"], fy)
    rdcf = valuation.reverse_dcf(cd, price, w["wacc"]) if price else {"implied_g": None}
    sgr_ = valuation.sgr(cd, fy)
    # SGR non significatif (n/m) → aucune croissance démontrée : g terminal = 0 dans le RE
    sgr_ok = sgr_.get("status") == "ok"
    re_ = valuation.residual_earnings_static(cd, w["ke"], sgr_.get("sgr") if sgr_ok else None, rf=w["rf"])
    cons = external.consensus(yf_info)
    conf = valuation.confidence_range(cd, w["wacc"])
    wsens = valuation.wacc_sensitivity(cd, w["wacc"])
    flags = scores.all_flags(cd, edgar_meta, mcap)
    if adj["gap_flag"]:
        flags.append({"id": "transitory_items", "severity": "info",
                      "message": f"Écart NI brut vs ajusté = {adj['gap_pct']:.0%} (>10%) — "
                                 "earnings affectés par items one-time",
                      "details": {"adjustments": adj["adjustments"]}})

    impl_g, sgr_v = rdcf.get("implied_g"), sgr_.get("sgr")
    # Comparaison impossible quand le SGR est n/m : None (N/D), pas False
    growth_unfinanceable = ((isinstance(impl_g, float) and isinstance(sgr_v, float) and impl_g > sgr_v)
                            if sgr_ok else None)
    # Règles ajoutées le 21 nov. 2026 : un ratio artefact ne doit pas passer sans signal
    if sgr_.get("nm_reason") == "invested_capital_near_zero":
        flags.append({"id": "invested_capital_near_zero", "severity": "medium",
                      "message": (f"Capital investi = {sgr_.get('invested_capital_share_of_assets', 0):.0%} de "
                                  f"l'actif (< {config.IC_NM_MIN_SHARE_OF_ASSETS:.0%}) : ROIC et SGR non significatifs"),
                      "details": {"invested_capital_share_of_assets": sgr_.get("invested_capital_share_of_assets"),
                                  "roic": sgr_.get("roic"), "sgr": sgr_v}})
    elif sgr_.get("status") == "n/m":
        flags.append({"id": "sgr_not_meaningful", "severity": "info",
                      "message": f"SGR non significatif ({sgr_.get('nm_reason')}) : taux de réinvestissement "
                                 f"{sgr_.get('rr', 0):.0%}",
                      "details": {"reason": sgr_.get("nm_reason"), "rr": sgr_.get("rr"), "sgr": sgr_v}})
    tax_info = metrics.effective_tax(cd, fy)
    if tax_info["source"] != "year":
        flags.append({"id": "tax_rate_abnormal", "severity": "info",
                      "message": ("Taux d'impôt de l'exercice hors de [0 %, 50 %] ou perte avant impôt : "
                                  + ("médiane des exercices normaux retenue" if tax_info["source"] == "median"
                                     else "aucun exercice normal → N/D")),
                      "details": tax_info})
    if w.get("rf_proxy"):
        peg = w.get("rf_proxy_reason") == "usd_peg"
        flags.append({"id": "rf_currency_proxy", "severity": "info" if peg else "medium",
                      "message": (f"{w.get('rf_currency')} ancré au dollar : US 10Y utilisé" if peg else
                                  f"Aucun taux 10 ans {w.get('rf_currency')} publié : US 10Y utilisé en substitut"),
                      "details": {"currency": w.get("rf_currency"), "rf": w.get("rf"), "reason": w.get("rf_proxy_reason")}})

    report = {
        "ticker": cd.ticker, "cik": cd.cik,
        "as_of": datetime.date.today().isoformat(),
        "fiscal_year": fy,
        "bloc_A_verdict": {
            "prix": price,
            "EPV": epv_, "EPV_confidence_range": conf, "EPV_wacc_sensitivity": wsens,
            "RE_valuation": re_,
            "consensus": cons,
            "reverse_dcf": rdcf,
            "SGR": sgr_,
            "flag_growth_unfinanceable": growth_unfinanceable,
        },
        "bloc_B_flags": flags,
        "bloc_C_dupont": dupont.advanced_dupont(cd, fy),
        "bloc_D_trends": {
            "roic_10y": scores.roic_trend(cd)["series"],
            # capital investi / actif par exercice : < IC_NM_MIN_SHARE_OF_ASSETS → ROIC n/m cette année-là
            "invested_capital_share_10y": {
                y: valuation.ic_share(metrics.derive(cd, y).get("invested_capital"), cd.value("total_assets", y))
                for y in scores.roic_trend(cd)["series"]},
            "rnoa_10y": dupont.rnoa_trend(cd),
            "gross_margin_10y": buffett.gross_margin_trend(cd)["series"],
            "fcf_adj_10y": {y: metrics.derive(cd, y).get("fcf_adj")
                            for y in cd.years("ocf")[-10:]},
            "roe_10y": buffett.roe_consistency(cd).get("series", {}),
            "owner_earnings_10y": buffett.owner_earnings_series(cd),
            "revenue_10y": cd.history("revenue", 10),
        },
        "bloc_E_health": {
            "net_debt_ebitda": m.get("net_debt_ebitda"),
            "interest_coverage": m.get("interest_coverage"),
            "lt_debt_payback": buffett.lt_debt_payback(cd),
            "current_ratio": m.get("current_ratio"), "quick_ratio": m.get("quick_ratio"),
            "de_ratio": m.get("de_ratio"),
            "ccc": {"dso": m.get("dso"), "dio": m.get("dio"), "dpo": m.get("dpo"),
                    "ccc": m.get("ccc")},
            "ocf_ni": m.get("ocf_ni"), "accruals_pct_rev": m.get("accruals_pct_rev"),
            "retained_earnings_test": buffett.retained_earnings_dollar_test(cd, price_history),
        },
        "bloc_F_peers": peer_block or {
            "note": "rempli par peer_batch.build_peer_blocks() sur l'univers complet"},
        "bloc_G_smart_money": external.insider_activity(
            edgar_meta.get("_submissions_raw", {})) if "_submissions_raw" in edgar_meta else
            {"note": "submissions non fournies"},
        "bloc_H_sensitivity": valuation.sensitivity_matrix(cd, w["ke"], sgr_.get("sgr"), rf=w["rf"]),
        "bloc_I_macro": {
            "leading_indicator": external.macro_context(gics_sector, macro_signals,
                pct_revenue_non_us=None),
            "geographic_exposure": segments.geographic_exposure(cd, yf_info),
        },
        "drill_down": {
            "wacc_detail": w,
            "adjustments": adj,
            "reformulation": m.get("reformulation"),
            "net_debt_detail": metrics.net_debt_detail(cd, fy),
            "tax_detail": metrics.effective_tax(cd, fy),
            "three_statement_10y": {name: cd.history(name, 10) for name in cd.series},
            "source_map_per_tag": build_source_map(cd),
            "edgar_filings_index": edgar_filings_index_url(cd.cik),
        },
    }
    report["score_composite"] = composite_score(report)
    return _nd(report)


def build_valuation_map(reports: list[dict]) -> dict:
    """Niveau 1 — JSON agrégé pour la Valuation Map (scatter + table triable)."""
    rows = []
    for r in reports:
        a = r["bloc_A_verdict"]
        epv_ps = a.get("EPV", {}).get("epv_per_share")
        price = a.get("prix")
        gap = None
        if isinstance(epv_ps, (int, float)) and isinstance(price, (int, float)) and price:
            gap = round((epv_ps - price) / price, 4)
        meta = r.get("meta", {})
        trends = r.get("bloc_D_trends", {}) or {}
        roic = trends.get("roic_10y", {}) or {}
        ics = trends.get("invested_capital_share_10y", {}) or {}
        wd = r.get("drill_down", {}).get("wacc_detail", {}) or {}
        wacc_v = wd.get("wacc")
        num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        # Exercices où le ROIC est un artefact (capital investi quasi nul) : exclus du compte
        nm_years = sorted(str(y) for y, sh in ics.items()
                          if num(sh) is not None and sh < config.IC_NM_MIN_SHARE_OF_ASSETS)
        valid = {str(y): v for y, v in roic.items() if num(v) is not None and str(y) not in nm_years}
        n_above = (sum(1 for v in valid.values() if v > wacc_v) if num(wacc_v) is not None else None)
        sg = a.get("SGR", {}) or {}
        rd = a.get("reverse_dcf", {}) or {}
        rdi = rd.get("inputs", {}) if isinstance(rd.get("inputs"), dict) else {}
        epi = (a.get("EPV", {}) or {}).get("inputs", {}) or {}
        rows.append({
            "ticker": r["ticker"], "prix": price, "epv_per_share": epv_ps,
            "re_per_share": a.get("RE_valuation", {}).get("re_value_per_share"),
            "ecart_pct": gap if gap is not None else "N/D",
            # Discriminant principal (l'EPV suppose zéro croissance) : ce que le prix exige vs
            # ce que l'entreprise autofinance, et la persistance du ROIC au-dessus du WACC.
            "implied_growth": a.get("reverse_dcf", {}).get("implied_g", "N/D"),
            "sgr": a.get("SGR", {}).get("sgr", "N/D"),
            "roic_years_above_wacc": (f"{n_above}/{len(valid)}" if n_above is not None and valid else "N/D"),
            # Section 01 (brushing, sparkline, curseur WACC) — tout est recalculable côté client
            "roic_by_year": {str(y): (num(v) if num(v) is not None else "N/D") for y, v in roic.items()},
            "roic_nm_years": nm_years,
            "wacc": num(wacc_v) if num(wacc_v) is not None else "N/D",
            "rf": num(wd.get("rf")) if num(wd.get("rf")) is not None else "N/D",
            "rf_currency": wd.get("rf_currency", "N/D"), "rf_proxy": bool(wd.get("rf_proxy")),
            "sgr_status": sg.get("status", "N/D"), "sgr_nm_reason": sg.get("nm_reason"),
            "sgr_rr": num(sg.get("rr")) if num(sg.get("rr")) is not None else "N/D",
            "rdcf": {"ev": num(rdi.get("ev")), "fcf_adj": num(rdi.get("fcf_adj")),
                     "market_cap": num(wd.get("E")), "net_debt": num(epi.get("net_debt")),
                     "fiscal_year": r.get("fiscal_year")},
            "flags": [{"id": f.get("id"), "severity": f.get("severity")} for f in r.get("bloc_B_flags", [])],
            "score": r["score_composite"]["value_0_100"],
            "n_flags": len(r["bloc_B_flags"]),
            "consensus_median": a.get("consensus", {}).get("target_median", "N/D"),
            "currency": meta.get("reporting_currency", "N/D"),
            "source": meta.get("source", "N/D"),
            "fiscal_year_end": meta.get("fiscal_year_end", "N/D"),
            "stale_financials": meta.get("stale_financials", False),
        })
    return {"as_of": datetime.date.today().isoformat(), "rows": rows,
            "note": "prix et valeurs par action dans la devise des comptes (champ currency)",
            # seuils publiés avec les données : la page les affiche, elle ne les recode pas
            "out_of_scope": config.OUT_OF_SCOPE,
            "thresholds": {"sgr_nm_rr_min": config.SGR_NM_RR_MIN, "sgr_nm_rr_max": config.SGR_NM_RR_MAX,
                           "ic_nm_min_share_of_assets": config.IC_NM_MIN_SHARE_OF_ASSETS,
                           "terminal_g_ke_gap": config.TERMINAL_G_KE_GAP,
                           "cross_check_tol": config.CROSS_CHECK_TOL,
                           "tax_rate_valid_range": list(config.TAX_RATE_VALID_RANGE)}}


def save(obj: dict, path: str):
    # encoding explicite : sous Windows le défaut est cp1252 (casse €, ≤, ×…)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
