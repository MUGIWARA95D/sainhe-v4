"""Pipeline SAINHE — orchestrateur (Fetch → Compute → Render).

Usage (depuis sainhe_backend/) :
    python pipeline.py AAPL MSFT KO JPM XOM
    python pipeline.py --portfolio          # tickers de config.PORTFOLIO_TICKERS

Prérequis :
    pip install pandas numpy scipy requests yfinance
    Mettre son email dans config.py → EDGAR_USER_AGENT

JSON de sortie → output/data/stock/{TICKER}_report.json  (lu par le front)
                 output/data/stock/valuation_map.json
"""
from __future__ import annotations
import sys, os, logging, dataclasses

sys.path.insert(0, os.path.dirname(__file__))

import config
from edgar.edgar_client import EdgarClient
from edgar.xbrl_parser import parse_companyfacts
from edgar.metadata_flags import metadata_flags
from output import report_builder

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("sainhe.pipeline")

OUT_DIR = config.OUTPUT_DIR
_YF_SESSION = None


def _yf_session():
    """Session navigateur (curl_cffi), comme scripts/fetch_prices.py : sans elle Yahoo
    répond souvent 'Too Many Requests' et prix/consensus tombent en N/D."""
    global _YF_SESSION
    if _YF_SESSION is None:
        try:
            from curl_cffi import requests as cffi
            _YF_SESSION = cffi.Session(impersonate="chrome")
        except ImportError:
            _YF_SESSION = False
    return _YF_SESSION or None


def _yf_data(ticker: str):
    """Prix courant + info + prix historiques annuels (pour le $1 test) + splits. Optionnel."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker, session=_yf_session())
        info = t.info or {}
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        hist = t.history(period="10y")["Close"]
        price_history = {}
        if len(hist):
            yearly = hist.groupby(hist.index.year).last()
            price_history = {int(y): float(p) for y, p in yearly.items()}
        splits = {str(d.date()): float(r) for d, r in t.splits.items() if r}
        return price, info, price_history, splits
    except Exception as e:  # réseau absent, ticker inconnu, etc.
        log.warning("yfinance indisponible pour %s (%s) — prix/consensus = N/D", ticker, e)
        return None, None, None, {}


def _corporate_action(cd, splits: dict[str, float]) -> dict | None:
    """Opérations sur titres POSTÉRIEURES au dernier exercice publié.
    Ratio ≈ entier (split 2:1, 10:1) → actions/BPA de l'exercice ramenés à la base actuelle.
    Ratio non entier (spin-off, ex. CTVA 2026-10-01 ×6,665) → comptes d'une autre entreprise
    que celle cotée aujourd'hui → valorisation N/D + flag."""
    last = cd.latest("revenue") or cd.latest("net_income")
    if not last or not splits:
        return None
    after = {d: r for d, r in splits.items() if d > last.end}
    if not after:
        return None
    ratio = 1.0
    for r in after.values():
        ratio *= r
    integer = abs(ratio - round(ratio)) < config.SPLIT_INTEGER_TOLERANCE * ratio
    return {"ratio": ratio, "dates": sorted(after), "integer": integer, "fy_end": last.end}


def _apply_split(cd, ratio: float) -> None:
    for name, op in (("shares_diluted", "mul"), ("shares_outstanding", "mul"), ("eps_diluted", "div")):
        s = cd.series.get(name)
        if not s:
            continue
        for y, dp in s.items():
            v = dp.value * ratio if op == "mul" else dp.value / ratio
            s[y] = dataclasses.replace(dp, value=v, tag=f"{dp.tag}×split{ratio:g}")


def _parse_with_fallback(client: EdgarClient, ticker: str, cik: int, forms):
    """CIK de la table SEC sans historique (nouvelle holding) → CIK historique en complément."""
    facts = client.companyfacts(cik)
    subs = client.submissions(cik)
    cd = parse_companyfacts(ticker, cik, facts, forms=forms)
    fb = config.CIK_FALLBACK.get(ticker)
    if fb and fb != cik and not (cd.series.get("revenue") and cd.series.get("ebit")):
        log.warning("%s : CIK %s sans historique %s → CIK historique %s", ticker, cik, forms, fb)
        legacy = parse_companyfacts(ticker, fb, client.companyfacts(fb), forms=forms)
        for name, s in legacy.series.items():           # la nouvelle entité prime à année égale
            cd.series[name] = {**s, **cd.series.get(name, {})}
        if not cd.series.get("revenue"):
            subs = client.submissions(fb)
            cd.cik = fb
    return cd, subs


def run_ticker(client: EdgarClient, ticker: str) -> dict:
    cik = client.ticker_to_cik(ticker)
    forms = ("20-F",) if ticker in config.ADR_TICKERS else ("10-K",)
    cd, subs = _parse_with_fallback(client, ticker, cik, forms)
    meta = metadata_flags(subs)
    meta["_submissions_raw"] = subs

    price, info, price_history, splits = _yf_data(ticker)
    ca = _corporate_action(cd, splits)
    valuation_price = price
    if ca and ca["integer"]:
        _apply_split(cd, ca["ratio"])
        log.warning("%s : split ×%g après le %s → actions de l'exercice ajustées", ticker, ca["ratio"], ca["fy_end"])
    elif ca:
        valuation_price = None
        log.warning("%s : opération sur titres ×%.4f après le %s (spin-off) → valorisation N/D",
                    ticker, ca["ratio"], ca["fy_end"])
    report = report_builder.build_report(
        cd, price=valuation_price, yf_info=info, edgar_meta=meta,
        gics_sector=(info or {}).get("sector"),
        price_history=price_history,
    )
    if ca and not ca["integer"]:
        va = report["bloc_A_verdict"]
        note = "N/D — comptes antérieurs à une opération sur titres (spin-off)"
        for k in ("EPV", "EPV_confidence_range", "EPV_wacc_sensitivity", "RE_valuation", "reverse_dcf", "SGR"):
            va[k] = {"value": "N/D", "note": note}
        va["prix"] = price
        report["bloc_H_sensitivity"] = {"value": "N/D", "note": note}
        report["bloc_B_flags"].append({
            "id": "corporate_action_after_filing", "severity": "high",
            "message": (f"Opération sur titres ×{ca['ratio']:.4f} le {', '.join(ca['dates'])}, après le "
                        f"dernier exercice publié ({ca['fy_end']}) : les comptes décrivent l'entreprise "
                        "d'avant l'opération → valorisation N/D jusqu'au prochain 10-K"),
            "details": ca})
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{ticker}_report.json")
    report_builder.save(report, path)
    log.info("OK %s -> %s", ticker, path)
    return report


def main(tickers: list[str]):
    client = EdgarClient()
    reports = []
    for t in tickers:
        try:
            reports.append(run_ticker(client, t))
        except Exception as e:
            log.error("ECHEC %s : %s", t, e)
    if reports:
        vmap = report_builder.build_valuation_map(reports)
        report_builder.save(vmap, os.path.join(OUT_DIR, "valuation_map.json"))
        log.info("valuation_map.json : %d tickers", len(vmap["rows"]))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--portfolio"]:
        # 14 tickers US du portfolio SAINHE exploitables par EDGAR (10-K)
        args = config.EDGAR_TICKERS
    elif args == ["--portfolio-all"]:
        # Les 31 tickers — ASML/TSM/RACE via 20-F, non-EDGAR sautés (log ECHEC)
        args = config.PORTFOLIO_TICKERS
    elif not args:
        args = config.EDGAR_TICKERS
    main(args)
