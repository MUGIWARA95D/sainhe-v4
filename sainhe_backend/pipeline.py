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
import sys, os, logging, dataclasses, datetime

sys.path.insert(0, os.path.dirname(__file__))

import config
from edgar.edgar_client import EdgarClient
from edgar import instance
from secondary import yahoo_statements
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


def _with_newer_instance(client: EdgarClient, ticker: str, cik: int, facts: dict, subs: dict, forms):
    """companyfacts en retard sur le dernier dépôt annuel XBRL (TSMC : 20-F du 2026-04-16 absent)
    → faits lus dans l'instance XBRL du dépôt et ajoutés (edgar/instance.py)."""
    cd = parse_companyfacts(ticker, cik, facts, forms=forms)
    newer = instance.newer_annual_filing(subs, forms, (cd.years("revenue") or [None])[-1])
    if not newer:
        return cd, facts
    try:
        nodash = newer["accn"].replace("-", "")
        idx = client._get_json(f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/index.json",
                               f"index:{newer['accn']}", max_age_days=30)
        url = instance.instance_url(idx, cik, newer["accn"])
        if not url:
            return cd, facts
        extra = instance.parse_instance(client.get_text(url, f"instance:{newer['accn']}"), newer["accn"], newer["form"])
        facts = instance.merge_facts(facts, extra)
        cd2 = parse_companyfacts(ticker, cik, facts, forms=forms)
        log.warning("%s : companyfacts sans le %s du %s → instance XBRL lue (%s), exercices %s",
                    ticker, newer["form"], newer["report_date"], url.rsplit("/", 1)[-1], cd2.years("revenue")[-3:])
        return cd2, facts
    except Exception as e:  # instance illisible : on garde companyfacts, signalé
        log.warning("%s : instance XBRL du dépôt %s illisible (%s)", ticker, newer["accn"], e)
        return cd, facts


def secondary_reason(cd) -> str | None:
    """Raison de passer une société ESEF sur la source secondaire, ou None."""
    fy = (cd.years("revenue") or [None])[-1]
    missing = [f for f in config.SECONDARY_CRITICAL_FIELDS if cd.value(f, fy) is None]
    if missing:
        return f"ESEF {fy} incomplete ({', '.join(missing)} not tagged)"
    last = cd.latest("revenue")
    try:
        end = datetime.date.fromisoformat(last.end)
        today = datetime.date.today()
        if (today.year - end.year) * 12 + today.month - end.month > config.STALE_FINANCIALS_MONTHS:
            return f"ESEF stale (latest indexed year {fy})"
    except (AttributeError, ValueError):
        pass
    return None


def _parse_with_fallback(client: EdgarClient, ticker: str, cik: int, forms):
    """CIK de la table SEC sans historique (nouvelle holding) → CIK historique en complément."""
    facts = client.companyfacts(cik)
    subs = client.submissions(cik)
    cd, facts = _with_newer_instance(client, ticker, cik, facts, subs, forms)
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


def _fx_history(src: str, dst: str):
    """Série journalière 'unités dst pour 1 src' (yfinance), ou None."""
    if src == dst:
        return None
    try:
        import yfinance as yf
        for pair, inv in ((f"{src}{dst}=X", False), (f"{dst}{src}=X", True)):
            h = yf.Ticker(pair, session=_yf_session()).history(period="11y")["Close"].dropna()
            if len(h):
                return (1 / h) if inv else h
    except Exception as e:
        log.warning("FX %s→%s indisponible (%s)", src, dst, e)
    return None


def _pricing_context(ticker: str, cd, info: dict | None) -> dict:
    """Facteur prix de cotation → prix par action ORDINAIRE dans la devise des COMPTES.

    Avant : le prix (USD pour ASML/TSM/RACE, pence pour COA.L) était comparé tel quel à des
    valeurs par action en EUR/TWD → marges de sécurité fausses, et TSM ignorait 1 ADR = 5 actions.
    factor_now s'applique au prix et aux objectifs analystes ; factors_by_year au prix
    historique annuel (test du dollar retenu), au taux de change de chaque fin d'année.
    """
    listing = (info or {}).get("currency") or "USD"
    reporting = cd.reporting_currency() or listing
    ratio = config.ADR_RATIO.get(ticker, 1.0)
    unit_scale = 0.01 if listing in ("GBp", "GBX") else 1.0       # pence → livres
    base = "GBP" if listing in ("GBp", "GBX") else listing
    ctx = {"listing_currency": listing, "reporting_currency": reporting, "adr_ratio": ratio,
           "factor_now": unit_scale / ratio, "factors_by_year": {}, "fx_now": 1.0}
    if base != reporting:
        fx = _fx_history(base, reporting)
        if fx is None:
            ctx["factor_now"] = None          # pas de taux → valorisation N/D (pas de devinette)
            return ctx
        ctx["fx_now"] = float(fx.iloc[-1])
        ctx["factor_now"] = unit_scale * ctx["fx_now"] / ratio
        yearly = fx.groupby(fx.index.year).last()
        ctx["factors_by_year"] = {int(y): unit_scale * float(v) / ratio for y, v in yearly.items()}
    return ctx


def _load_company(client: EdgarClient, esef, ticker: str):
    """(CompanyData, submissions EDGAR ou {}, source) selon le régime de publication."""
    if ticker in config.YAHOO_STATEMENTS_TICKERS:
        cd, _ = yahoo_statements.fetch(ticker, _yf_session())
        return cd, {}, f"{yahoo_statements.SOURCE} — {config.YAHOO_STATEMENTS_TICKERS[ticker]}"
    if ticker in config.ESEF_ENTITIES:
        lei = config.ESEF_ENTITIES[ticker]
        cd = parse_companyfacts(ticker, lei, esef.companyfacts(lei, ticker), forms=("ESEF",))
        if not cd.series.get("revenue"):
            raise ValueError(f"aucun chiffre d'affaires ESEF exploitable pour {ticker} (LEI {lei})")
        reason = secondary_reason(cd)
        if reason:
            try:
                ycd, _ = yahoo_statements.fetch(ticker, _yf_session())
                if (ycd.years("revenue") or [0])[-1] >= (cd.years("revenue") or [0])[-1]:
                    log.warning("%s : %s → source secondaire Yahoo", ticker, reason)
                    return ycd, {}, f"{yahoo_statements.SOURCE} — {reason}"
            except Exception as e:
                log.warning("%s : source secondaire indisponible (%s) — ESEF conservé", ticker, e)
        return cd, {}, "ESEF (filings.xbrl.org)"
    cik = client.ticker_to_cik(ticker)
    forms = ("20-F",) if ticker in config.ADR_TICKERS else ("10-K",)
    cd, subs = _parse_with_fallback(client, ticker, cik, forms)
    return cd, subs, f"SEC EDGAR {forms[0]}"


def run_ticker(client: EdgarClient, ticker: str, esef=None) -> dict:
    cd, subs, source = _load_company(client, esef, ticker)
    meta = metadata_flags(subs)
    meta["_submissions_raw"] = subs

    price, info, price_history, splits = _yf_data(ticker)
    pc = _pricing_context(ticker, cd, info)
    if price is not None and pc["factor_now"] is not None and pc["factor_now"] != 1.0:
        f = pc["factor_now"]
        info = dict(info or {})
        for k in ("currentPrice", "regularMarketPrice", "targetMeanPrice", "targetMedianPrice",
                  "targetHighPrice", "targetLowPrice"):
            if isinstance(info.get(k), (int, float)):
                info[k] = info[k] * f
        if price_history:
            price_history = {y: p * pc["factors_by_year"].get(y, f) for y, p in price_history.items()}
        log.info("%s : prix %s → comptes %s (×%.6f, ADR %g)", ticker, pc["listing_currency"],
                 pc["reporting_currency"], f, pc["adr_ratio"])
    listing_price = price
    price = None if (price is None or pc["factor_now"] is None) else price * pc["factor_now"]
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
        currency=pc["reporting_currency"],
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

    # ---- Métadonnées : source, devises, fraîcheur des comptes, disclaimer
    last = cd.latest("revenue") or cd.latest("net_income")
    months = None
    if last:
        try:
            fy_end = datetime.date.fromisoformat(last.end)
            today = datetime.date.today()
            months = (today.year - fy_end.year) * 12 + today.month - fy_end.month
        except ValueError:
            pass
    report["meta"] = {
        "source": source,
        "listing_currency": pc["listing_currency"], "reporting_currency": pc["reporting_currency"],
        "price_listing": listing_price, "price_in_reporting_currency": price,
        "fx_listing_to_reporting": pc["fx_now"], "adr_ratio": pc["adr_ratio"],
        "fiscal_year_end": last.end if last else None,
        "months_since_fiscal_year_end": months,
        # Plan : 10-K/rapport annuel uniquement → jusqu'à ~15 mois de décalage ; au-delà, signalé.
        "stale_financials": bool(months is not None and months > config.STALE_FINANCIALS_MONTHS),
        "disclaimer": config.DISCLAIMER,
    }
    if source.startswith(yahoo_statements.SOURCE):
        report["bloc_G_smart_money"] = {"value": "N/D", "note": "pas de dépôts d'initiés accessibles pour cette source"}
        report["drill_down"]["edgar_filings_index"] = yahoo_statements.page_url(ticker)
    if source.startswith("ESEF"):
        report["bloc_G_smart_money"] = {"value": "N/D",
                                        "note": "Forms 3/4/5 et 13F : obligations SEC, sans équivalent ESEF"}
        report["drill_down"]["edgar_filings_index"] = (
            f"https://filings.xbrl.org/api/entities/{config.ESEF_ENTITIES[ticker]}/filings")
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{ticker}_report.json")
    report_builder.save(report, path)
    log.info("OK %s -> %s", ticker, path)
    return report


def main(tickers: list[str]) -> dict:
    """Retourne {ok: [...], failed: {ticker: erreur}} — écrit aussi pipeline_status.json."""
    client = EdgarClient()
    esef = None
    if any(t in config.ESEF_ENTITIES for t in tickers):
        from esef.esef_client import EsefClient
        esef = EsefClient()
    reports, failed = [], {}
    for t in tickers:
        try:
            reports.append(run_ticker(client, t, esef))
        except Exception as e:
            log.error("ECHEC %s : %s", t, e)
            failed[t] = f"{type(e).__name__}: {e}"
    if reports:
        vmap = report_builder.build_valuation_map(reports)
        vmap["disclaimer"] = config.DISCLAIMER
        report_builder.save(vmap, os.path.join(OUT_DIR, "valuation_map.json"))
        log.info("valuation_map.json : %d tickers", len(vmap["rows"]))
    status = {"as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "requested": tickers, "ok": [r["ticker"] for r in reports], "failed": failed}
    report_builder.save(status, os.path.join(OUT_DIR, "pipeline_status.json"))
    return status


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--portfolio"]:
        # 14 tickers US du portfolio SAINHE exploitables par EDGAR (10-K)
        args = config.EDGAR_TICKERS
    elif args == ["--coverage"]:
        # Tout ce qui a des comptes XBRL : US 10-K + ADR 20-F + Europe ESEF (job CI hebdo)
        args = config.COVERAGE_TICKERS
    elif args == ["--portfolio-all"]:
        # Les 31 tickers — non couverts sautés (log ECHEC)
        args = config.PORTFOLIO_TICKERS
    elif not args:
        args = config.EDGAR_TICKERS
    main(args)
