"""Portfolio Sim — lanceur (plan §4). Avant ce fichier, portfolio/builder.py n'était appelé
par AUCUN programme : le module existait mais ne produisait rien.

Usage (depuis sainhe_backend/) :
    python portfolio_runner.py                 # 4 devises : USD EUR CHF HKD
    python portfolio_runner.py --currencies USD CHF --sims 2000

Entrées
- Univers : WATCHLIST de ../config.py (lue par AST : aucun effet de bord à l'import).
- Poids : ../data/portfolio_weights.json {"TICKER": poids} s'il existe, sinon ÉQUIPONDÉRÉ
  (la watchlist SAINHE n'a pas de poids) — l'hypothèse est écrite dans le JSON.
- Prix : yfinance (session navigateur), 5 ans, clôtures ajustées ; titres avec < 400 jours
  exclus et listés (ex. ANT, introduit le 2026-11-19).
- Devise de référence : prix convertis jour par jour ; rendements LOCAUX et de CHANGE fournis
  séparément au builder → décomposition du risque local vs FX (plan, « problème 2 »).
- Taux sans risque : US 3M (macro SAINHE) pour USD ; Bund 2Y comme proxy EUR ; pour CHF/HKD,
  pas de taux publié par le dashboard → US 3M, signalé dans rf_note (pas de taux inventé).
Sortie : ../data/stock/portfolio_report_{DEVISE}.json
"""
from __future__ import annotations
import argparse, ast, json, logging, os, sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import config                                   # noqa: E402  (config du backend)
from portfolio import builder as port_builder   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("sainhe.portfolio_runner")

ROOT = Path(__file__).resolve().parent.parent
MIN_DAYS = 400
HISTORY_START = "2007-01-01"   # stress tests : GFC 2008 → hike cycle 2022
MAIN_YEARS = 5                 # métriques courantes (rendements, risque, HRP, Monte Carlo)
BENCHMARKS = ["SPY", "ACWI"]
CURRENCIES = ["USD", "EUR", "CHF", "HKD"]


def _session():
    try:
        from curl_cffi import requests as cffi
        return cffi.Session(impersonate="chrome")
    except ImportError:
        return None


def _root_config_value(name: str):
    tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == name:
            return ast.literal_eval(n.value)
    raise RuntimeError(f"{name} introuvable dans config.py")


def load_watchlist() -> dict:
    return _root_config_value("WATCHLIST")


def load_competitors() -> dict:
    """ticker → (concurrent, nom, marché) ; table éditoriale de config.py (bouton ⇄ du Lab)."""
    try:
        return {t: v for t, v in _root_config_value("COMPETITORS").items() if v}
    except RuntimeError:
        return {}


def _enc(series: pd.Series) -> list:
    """Rendements → entiers en 1e-5 (≈ 6 octets/valeur au lieu de 20), None si absent."""
    return [None if v != v else int(round(v * 1e5)) for v in series.tolist()]


def export_lab_data(closes: dict, infos: dict, wl: dict, comps: dict, holdings: list[str],
                    weights: dict, sess) -> Path:
    """data/stock/lab_data.json — tout ce dont le Portfolio Lab et le Rejeu de crises ont
    besoin pour recalculer DANS LE NAVIGATEUR (site statique, aucun serveur) :
      - rendements journaliers en DEVISE LOCALE (titres, concurrents, SPY/ACWI), 5 ans ;
      - niveaux de change « USD pour 1 unité » de chaque devise → conversion dans n'importe
        quelle devise d'affichage : r_ref = (1+r_loc)·(fx_nat,t/fx_nat,t-1)·(fx_ref,t-1/fx_ref,t) − 1 ;
      - fenêtres de crise (stress.SCENARIOS) avec les mêmes données ;
      - préréglages : poids par défaut et HRP (portfolio_report_USD.json).
    """
    from portfolio.stress import SCENARIOS
    names = list(dict.fromkeys(holdings + [c[0] for c in comps.values()] + BENCHMARKS))
    names = [t for t in names if t in closes and len(closes[t])]
    nat = {}
    for t in names:
        c = (infos.get(t) or {}).get("currency") or (wl.get(t) or {}).get("devise", "USD")
        nat[t] = "GBP" if c in ("GBp", "GBX") else c
    days = closes["SPY"].index
    px = pd.DataFrame({t: closes[t] for t in names}).reindex(days).ffill(limit=3)
    rets = px.pct_change(fill_method=None)
    currencies = sorted(set(nat.values()) | set(CURRENCIES))
    fx = {}
    for c in currencies:
        if c == "USD":
            continue
        s = fx_series(c, "USD", sess)
        if s is not None:
            fx[c] = s.reindex(days).ffill().bfill()

    def block(idx) -> dict:
        return {"dates": [d.strftime("%Y-%m-%d") for d in idx],
                "r": {t: _enc(rets.loc[idx, t]) for t in names},
                "fx_usd": {c: [round(float(v), 6) for v in s.loc[idx].tolist()] for c, s in fx.items()}}

    cutoff = days[-1] - pd.DateOffset(years=MAIN_YEARS, days=15)
    main_idx = days[days >= cutoff]
    crises = {}
    for name, (start, end) in SCENARIOS.items():
        idx = days[(days >= pd.Timestamp(start) - pd.Timedelta(days=7)) & (days <= pd.Timestamp(end))]
        if len(idx) > 2:
            crises[name] = {"start": start, "end": end, **block(idx)}
    hrp = {}
    rp = ROOT / "data" / "stock" / "portfolio_report_USD.json"
    if rp.exists():
        rep = json.loads(rp.read_text(encoding="utf-8"))
        for row in rep.get("bloc_3_correlation_hrp", {}).get("weights_comparison_hrp", []):
            hrp[row["ticker"]] = row.get("hrp_suggested")
    out = {
        "as_of": str(days[-1].date()),
        "unit": "rendements en 1e-5 (entiers), devise locale ; fx_usd = USD pour 1 unité",
        "holdings": [{"t": t, "name": wl[t].get("nom", t), "sector": wl[t].get("secteur", "N/D"),
                      "cur": nat.get(t, "USD")} for t in holdings if t in names],
        "competitors": {t: {"t": c[0], "name": c[1], "market": c[2], "cur": nat.get(c[0])}
                        for t, c in comps.items() if c[0] in names},
        # watchlist absente du Labo (historique trop court ou cotation morte) : affichée
        "excluded": [{"t": t, "name": wl[t].get("nom", t)} for t in wl if t not in holdings],
        "benchmarks": BENCHMARKS,
        "currencies_display": CURRENCIES,
        "presets": {"default": weights, "hrp": hrp},
        "main": block(main_idx),
        "crises": crises,
        "disclaimer": config.DISCLAIMER,
    }
    path = ROOT / "data" / "stock" / "lab_data.json"
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("OK lab_data.json (%d séries, %d crises, %.0f Ko)", len(names), len(crises),
             path.stat().st_size / 1024)
    return path


def load_weights(tickers: list[str]) -> tuple[dict, str]:
    p = ROOT / "data" / "portfolio_weights.json"
    if p.exists():
        w = {t: float(v) for t, v in json.loads(p.read_text(encoding="utf-8")).items() if t in tickers}
        if w:
            return w, f"poids utilisateur ({p.name})"
    return {t: 1.0 for t in tickers}, "équipondéré (la watchlist SAINHE n'a pas de poids)"


def macro_rates() -> dict:
    try:
        import requests
        d = requests.get(config.MACRO_JSON_URL, timeout=15).json().get("data", {})
        val = lambda k: (d.get(k) or {}).get("value") if isinstance(d.get(k), dict) else d.get(k)
        return {"USD": val("us_3m"), "EUR_proxy": val("bund_2y")}
    except Exception as e:
        log.warning("macro.json indisponible (%s)", e)
        return {}


def fetch(tickers: list[str], sess):
    import yfinance as yf
    closes, infos = {}, {}
    for t in tickers:
        try:
            tk = yf.Ticker(t, session=sess)
            h = tk.history(period="max", auto_adjust=True)["Close"].dropna()
            h.index = h.index.tz_localize(None).normalize()
            closes[t] = h.loc[HISTORY_START:]
            try:
                infos[t] = tk.get_info()
            except Exception:
                infos[t] = {}
        except Exception as e:
            log.warning("%s : prix indisponibles (%s)", t, e)
    return closes, infos


# Parités officielles (même règle que scripts/fetch_fx.py) : la cotation Yahoo du SAR est
# bruitée et ses paires croisées (SAREUR, SARCHF…) n'ont qu'UN jour d'historique.
USD_PEGS = {"SAR": 3.75}      # unités de devise pour 1 USD
MIN_FX_DAYS = 250


def _fx_direct(cur: str, ref: str, sess) -> pd.Series | None:
    import yfinance as yf
    for pair, inv in ((f"{cur}{ref}=X", False), (f"{ref}{cur}=X", True)):
        try:
            h = yf.Ticker(pair, session=sess).history(period="max")["Close"].dropna().loc[HISTORY_START:]
            if len(h) >= MIN_FX_DAYS:
                h.index = h.index.tz_localize(None).normalize()
                return (1 / h) if inv else h
        except Exception:
            continue
    return None


def _fx_usd(cur: str, sess) -> pd.Series | float | None:
    """USD pour 1 `cur` : parité officielle si elle existe, sinon cotation journalière."""
    if cur == "USD":
        return 1.0
    if cur in USD_PEGS:
        return 1.0 / USD_PEGS[cur]
    return _fx_direct(cur, "USD", sess)


def fx_series(cur: str, ref: str, sess) -> pd.Series | None:
    """Unités de `ref` pour 1 `cur` (journalier). Paire directe si son historique suffit,
    sinon triangulation par l'USD : cur→USD × USD→ref."""
    if cur == ref:
        return None
    if cur not in USD_PEGS and ref not in USD_PEGS:
        direct = _fx_direct(cur, ref, sess)
        if direct is not None:
            return direct
    a, b = _fx_usd(cur, sess), _fx_usd(ref, sess)
    if a is None or b is None:
        return None
    if isinstance(a, float) and isinstance(b, float):
        # Deux taux fixes (ex. SAR→USD) : série constante sur la fenêtre de 5 ans
        days = pd.bdate_range(HISTORY_START, pd.Timestamp.today().normalize())
        return pd.Series(a / b, index=days)
    if isinstance(a, float):
        return a / b
    if isinstance(b, float):
        return a / b
    al = pd.concat([a, b], axis=1).ffill().dropna()
    return al.iloc[:, 0] / al.iloc[:, 1]


def main(currencies: list[str], n_sims: int) -> dict:
    sess = _session()
    wl = load_watchlist()
    comps = load_competitors()
    comp_tickers = [c[0] for c in comps.values() if c[0] not in wl]
    closes, infos = fetch(list(dict.fromkeys(list(wl) + BENCHMARKS + comp_tickers)), sess)
    native = {}
    for t in wl:
        c = (infos.get(t) or {}).get("currency") or wl[t].get("devise", "USD")
        native[t] = "GBP" if c in ("GBp", "GBX") else c       # pence : facteur constant, sans effet sur les rendements
    excluded = {t: len(closes.get(t, [])) for t in wl if len(closes.get(t, [])) < MIN_DAYS}
    tickers = [t for t in wl if t not in excluded]
    weights, weights_note = load_weights(tickers)

    us_days = closes["SPY"].index
    local = pd.DataFrame({t: closes[t] for t in tickers}).reindex(us_days).ffill(limit=3)
    bench_usd = pd.DataFrame({b: closes[b] for b in BENCHMARKS}).reindex(us_days).ffill(limit=3)

    sector_map = {t: wl[t].get("secteur", "N/D") for t in tickers}
    geo_map = {t: (infos.get(t) or {}).get("country") or "N/D" for t in tickers}
    yields = {t: (infos.get(t) or {}).get("trailingAnnualDividendYield") for t in tickers}
    yields = {t: float(v) for t, v in yields.items() if isinstance(v, (int, float))}

    stock_reports = {}
    for p in (ROOT / "data" / "stock").glob("*_report.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            stock_reports[r["ticker"]] = r
        except Exception:
            continue

    rates = macro_rates()
    status = {"currencies": {}, "excluded_short_history": excluded, "weights": weights_note}
    fx_cache: dict[tuple[str, str], pd.Series | None] = {}
    for ref in currencies:
        prices = {}
        fx_ret = {}
        for t in tickers:
            cur = native[t]
            if cur == ref:
                prices[t] = local[t]
                continue
            key = (cur, ref)
            if key not in fx_cache:
                fx_cache[key] = fx_series(cur, ref, sess)
            fx = fx_cache[key]
            if fx is None:
                log.warning("%s : pas de taux %s→%s → exclu de la vue %s", t, cur, ref, ref)
                continue
            fx_al = fx.reindex(us_days).ffill()
            prices[t] = local[t] * fx_al
            fx_ret[f"{cur}{ref}"] = fx_al.pct_change()
        prices = pd.DataFrame(prices).dropna(how="all")
        if ref == "USD":
            bench = bench_usd
        else:
            fxb = fx_cache.setdefault(("USD", ref), fx_series("USD", ref, sess))
            bench = bench_usd.mul(fxb.reindex(us_days).ffill(), axis=0) if fxb is not None else bench_usd
        bench = bench.reindex(prices.index)
        rf_pct = rates.get("USD") if ref == "USD" else (rates.get("EUR_proxy") if ref == "EUR" else rates.get("USD"))
        rf_note = {"USD": "US 3M T-bill (macro SAINHE)",
                   "EUR": "Bund 2Y comme proxy du taux court EUR (macro SAINHE)"}.get(
            ref, f"aucun taux court {ref} publié par le dashboard → US 3M (approximation signalée)")
        rf = (rf_pct / 100) if isinstance(rf_pct, (int, float)) else config.DEFAULT_RISK_FREE
        w_ref = {t: w for t, w in weights.items() if t in prices.columns}
        # Fenêtre courante (5 ans) pour rendements / risque / HRP / Monte Carlo ;
        # historique complet (≥ 2007) seulement pour rejouer les stress tests.
        # +15 jours : returns.total_returns exige strictement plus de 5×252 séances pour le 5Y
        cutoff = prices.index[-1] - pd.DateOffset(years=MAIN_YEARS, days=15)
        main_prices = prices.loc[cutoff:]
        report = port_builder.build_portfolio_report(
            prices=main_prices, weights=w_ref, benchmarks=bench.loc[cutoff:], rf_annual=rf,
            per_ticker_yield=yields, sector_map=sector_map, geo_map=geo_map,
            currency_map={t: native[t] for t in w_ref}, reference_currency=ref,
            local_returns=local.loc[cutoff:, list(w_ref)].pct_change(fill_method=None),
            fx_returns=pd.DataFrame(fx_ret).loc[cutoff:],
            stock_analysis_reports=stock_reports, n_mc_sims=n_sims, portfolio_currency=ref,
            stress_prices=prices,
        )
        first_quote = {t: str(prices[t].first_valid_index().date()) for t in w_ref
                       if prices[t].first_valid_index() is not None}
        report["meta"] = {
            "reference_currency": ref, "rf_annual": rf, "rf_note": rf_note,
            "weights_note": weights_note, "n_holdings": len(w_ref),
            "excluded_short_history": excluded,
            "excluded_no_fx": sorted(set(tickers) - set(w_ref)),
            "period_main": [str(main_prices.index[0].date()), str(main_prices.index[-1].date())],
            "period_stress": [str(prices.index[0].date()), str(prices.index[-1].date())],
            "method": ("portefeuille actuel rejoué : chaque jour, seuls les titres déjà cotés "
                       "comptent, poids renormalisés (portfolio/_common.py) ; jours < 50 % "
                       "de couverture écartés ; titres absents listés par scénario"),
            "listed_after_window_start": {t: d for t, d in first_quote.items()
                                          if d > str(main_prices.index[0].date())},
            "disclaimer": config.DISCLAIMER,
        }
        out = ROOT / "data" / "stock" / f"portfolio_report_{ref}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        port_builder.save(report, str(out))
        size = out.stat().st_size
        log.info("OK %s → %s (%d holdings, %.0f Ko)", ref, out.name, len(w_ref), size / 1024)
        status["currencies"][ref] = {"holdings": len(w_ref), "kb": round(size / 1024)}
    # Données du Portfolio Lab / Rejeu de crises (calcul côté navigateur) — après les
    # rapports, pour reprendre les poids HRP de portfolio_report_USD.json
    lab = export_lab_data(closes, infos, wl, comps, tickers,
                          {t: w / sum(weights.values()) for t, w in weights.items()}, sess)
    status["lab_data_kb"] = round(lab.stat().st_size / 1024)
    return status


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--currencies", nargs="+", default=CURRENCIES)
    ap.add_argument("--sims", type=int, default=10_000)
    a = ap.parse_args()
    print(json.dumps(main(a.currencies, a.sims), ensure_ascii=False))
