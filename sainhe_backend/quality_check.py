"""Contrôle qualité automatique des données Stock Analysis (lancé après pipeline.py en CI).

Pourquoi : les tags XBRL migrent chaque année (constaté le 2026-11-20 sur 5 sociétés en une
soirée : dette KO/ECL, D&A MSFT/ISRG, trésorerie ZTS…). Sans surveillance, une valeur passe
en N/D — ou pire, une composante passe silencieusement à zéro — sans que personne ne le voie.

Contrôles, par ticker :
1. RECOUPEMENT DEUX SOURCES : valeurs XBRL du dernier exercice (EDGAR / ESEF) vs états
   financiers yfinance du même exercice (chiffre d'affaires, résultat net, actif total,
   trésorerie). Écart > TOL → anomalie (tag erroné, mauvaise année, mauvaise devise…).
2. RÉGRESSION N/D : champs clés passés de « renseigné » à « N/D » depuis le dernier contrôle
   (quality_report.json précédent) → probable migration de tag.
3. FRAÎCHEUR : comptes > config.STALE_FINANCIALS_MONTHS mois.
4. ÉCHECS du pipeline (pipeline_status.json).

Sortie : ../data/stock/quality_report.json ; code retour 0 (la CI décide d'ouvrir une issue
à partir de summary.n_issues).
"""
from __future__ import annotations
import datetime, json, logging, os, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("sainhe.quality")

STOCK = Path(__file__).resolve().parent.parent / "data" / "stock"
TOL = config.CROSS_CHECK_TOL   # 3 % : arrondis, reclassements mineurs (seuil dans config.py)
KEY_FIELDS = {
    "EPV/action": ("bloc_A_verdict", "EPV", "epv_per_share"),
    "RE/action": ("bloc_A_verdict", "RE_valuation", "re_value_per_share"),
    "croissance implicite": ("bloc_A_verdict", "reverse_dcf", "implied_g"),
    "SGR": ("bloc_A_verdict", "SGR", "sgr"),
    "WACC": ("drill_down", "wacc_detail", "wacc"),
    "dette nette/EBITDA": ("bloc_E_health", "net_debt_ebitda"),
}
# tag interne SAINHE → lignes yfinance candidates (income_stmt / balance_sheet)
CROSS = {
    "revenue": ("income_stmt", ["Total Revenue", "Operating Revenue"]),
    "net_income": ("income_stmt", ["Net Income Common Stockholders", "Net Income"]),
    "total_assets": ("balance_sheet", ["Total Assets"]),
    "cash": ("balance_sheet", ["Cash And Cash Equivalents",
                               "Cash Cash Equivalents And Short Term Investments"]),
}


def _get(d, path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d if isinstance(d, (int, float)) and not isinstance(d, bool) else None


def _yf_statements(ticker: str, sess):
    import yfinance as yf
    t = yf.Ticker(ticker, session=sess)
    return {"income_stmt": t.income_stmt, "balance_sheet": t.balance_sheet}


def cross_check(report: dict, ticker: str, sess) -> tuple[list[dict], list[str], list[dict]]:
    """(anomalies, champs effectivement comparés, détail de CHAQUE comparaison) — un « 0 anomalie »
    sans comparaison ne prouve rien : chaque vérification est publiée avec ses deux valeurs,
    l'écart et la tolérance (auditable depuis la page Stock Analysis)."""
    sm = report.get("drill_down", {}).get("source_map_per_tag", {})
    issues, checked, details = [], [], []
    try:
        st = _yf_statements(ticker, sess)
    except Exception as e:
        return [{"type": "cross_source_unavailable", "detail": f"yfinance : {type(e).__name__}"}], [], []
    for tag, (stmt, rows) in CROSS.items():
        src = sm.get(tag)
        df = st.get(stmt)
        if not src or df is None or df.empty or not isinstance(src.get("value"), (int, float)):
            continue
        # Même exercice : dates de clôture à ±40 j (DE : 2025-11-02 EDGAR vs 2025-10-31 yfinance)
        try:
            end = datetime.date.fromisoformat(str(src.get("end_date", ""))[:10])
        except ValueError:
            continue
        cols = [c for c in df.columns
                if abs((datetime.date.fromisoformat(str(c)[:10]) - end).days) <= 40]
        row = next((r for r in rows if r in df.index), None)
        if not cols or row is None:
            continue
        other = df.loc[row, cols[0]]
        try:
            other = float(other)
        except (TypeError, ValueError):
            continue
        if other != other or other == 0:                       # NaN ou zéro
            continue
        checked.append(tag)
        gap = abs(src["value"] - other) / abs(other)
        details.append({"field": tag, "fiscal_end": src.get("end_date"), "xbrl": src["value"],
                        "xbrl_tag": src.get("xbrl_tag"), "filing_url": src.get("edgar_filing_url"),
                        "second_source": "yfinance " + stmt, "second_source_row": row,
                        "second_source_period": str(cols[0])[:10], "second_value": other,
                        "gap_pct": round(gap * 100, 2), "tolerance_pct": round(TOL * 100, 2),
                        "status": "gap" if gap > TOL else "ok"})
        if gap > TOL:
            issues.append({"type": "cross_source_gap", "field": tag, "fiscal_end": src.get("end_date"),
                           "xbrl": src["value"], "xbrl_tag": src.get("xbrl_tag"),
                           "yfinance": other, "gap_pct": round(gap * 100, 1)})
    return issues, checked, details


def main() -> dict:
    try:
        from curl_cffi import requests as cffi
        sess = cffi.Session(impersonate="chrome")
    except ImportError:
        sess = None
    prev_path = STOCK / "quality_report.json"
    prev = json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.exists() else {}
    prev_filled = {t: set(v.get("filled_fields", [])) for t, v in prev.get("tickers", {}).items()}
    status = json.loads((STOCK / "pipeline_status.json").read_text(encoding="utf-8")) \
        if (STOCK / "pipeline_status.json").exists() else {}

    out = {"tickers": {}, "pipeline_failures": status.get("failed", {})}
    for p in sorted(STOCK.glob("*_report.json")):
        if p.name.startswith("portfolio_") or p.name == "quality_report.json":
            continue
        r = json.loads(p.read_text(encoding="utf-8"))
        t = r.get("ticker")
        if not t:
            continue
        filled = sorted(k for k, path in KEY_FIELDS.items() if _get(r, path) is not None)
        missing = sorted(set(KEY_FIELDS) - set(filled))
        secondary = str(r.get("meta", {}).get("source", "")).startswith("Yahoo")
        # source secondaire : la seconde source serait la même (Yahoo) → aucun recoupement possible
        issues, checked, details = ([], [], []) if secondary else cross_check(r, t, sess)
        regress = sorted(prev_filled.get(t, set()) - set(filled))
        if regress:
            issues.append({"type": "nd_regression", "fields": regress,
                           "detail": "renseignés au contrôle précédent, N/D maintenant (migration de tag ?)"})
        meta = r.get("meta", {})
        if meta.get("stale_financials"):
            issues.append({"type": "stale_financials", "fiscal_year_end": meta.get("fiscal_year_end"),
                           "months": meta.get("months_since_fiscal_year_end")})
        out["tickers"][t] = {"source": meta.get("source"), "filled_fields": filled,
                             "missing_fields": missing, "cross_checked": checked, "checks": details,
                             "secondary_source": secondary,
                             "issues": issues}
        log.info("%-7s %d/%d champs, recoupés %s, %d anomalie(s)", t, len(filled), len(KEY_FIELDS),
                 checked or "aucun", len(issues))
    blocking = sum(1 for v in out["tickers"].values() for i in v["issues"]
                   if i["type"] in ("cross_source_gap", "nd_regression"))
    out["summary"] = {
        "n_tickers": len(out["tickers"]),
        "n_cross_checks": sum(len(v["cross_checked"]) for v in out["tickers"].values()),
        "n_issues": blocking + len(out["pipeline_failures"]),
        "n_cross_source_gaps": sum(1 for v in out["tickers"].values() for i in v["issues"]
                                   if i["type"] == "cross_source_gap"),
        "n_nd_regressions": sum(1 for v in out["tickers"].values() for i in v["issues"]
                                if i["type"] == "nd_regression"),
        "n_stale": sum(1 for v in out["tickers"].values() for i in v["issues"]
                       if i["type"] == "stale_financials"),
        "n_pipeline_failures": len(out["pipeline_failures"]),
        "n_secondary_source": sum(1 for v in out["tickers"].values() if v.get("secondary_source")),
        "note": "n_issues compte les anomalies à corriger (écarts de sources, régressions N/D, "
                "échecs) ; la fraîcheur est informative (dépend de la date de publication).",
    }
    prev_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return out["summary"]


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))
