"""
verify_live.py — weekly check of what sainhe.com actually publishes against the sources.

Born from the audit of 22 Nov 2026: stooq had died silently and the site showed the ECB AAA
curve under the « Bund » label for weeks. Nothing then compared the published numbers with
their sources. This script does, and fails (exit 1) when a value drifts or a source answers
nothing — the workflow verify-live.yml then opens an issue.

A value passes when it matches ANY of the source's last observations (sources publish with
one or two days of lag: comparing a Friday close with Thursday's FRED value is not an error).

Run:  python scripts/verify_live.py [--site https://sainhe.com] [--out report.md]
Needs: curl_cffi, openpyxl. SEC checks only if EDGAR_CONTACT_EMAIL is set (env or .env).
"""
import argparse
import csv
import io
import math
import os
import sys
import time
from pathlib import Path

from curl_cffi import requests as cr

S = cr.Session(impersonate="chrome")
RESULTS = []          # (status, area, item, site, ref, source, note)


def get_json(url):
    r = S.get(url, timeout=40)
    r.raise_for_status()
    return r.json()


def fred_obs(series, n=3):
    txt = S.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", timeout=40).text
    rows = [(d, float(v)) for d, v in list(csv.reader(io.StringIO(txt)))[1:] if v not in (".", "")]
    return rows[-n:]


def ecb_obs(key, n=3):
    txt = S.get(f"https://data-api.ecb.europa.eu/service/data/{key}?lastNObservations={n}&format=csvdata", timeout=40).text
    return [(r["TIME_PERIOD"], float(r["OBS_VALUE"])) for r in csv.DictReader(io.StringIO(txt))]


def bundesbank_obs(mat, n=3):
    url = ("https://api.statistiken.bundesbank.de/rest/data/BBSIS/D.I.ZAR.ZI.EUR.S1311.B.A604."
           f"{mat}.R.A.A._Z._Z.A?lastNObservations={n}&format=csv&lang=en")
    out = []
    for line in S.get(url, timeout=40).text.lstrip("﻿").splitlines():
        p = line.split(",")
        if len(p) >= 2 and len(p[0]) == 10 and p[0][4] == "-" and p[1] not in ("", "."):
            out.append((p[0], float(p[1])))
    return out


def yahoo_closes(t, rng="10d"):
    r = S.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range={rng}&interval=1d", timeout=40).json()
    res = r["chart"]["result"][0]
    q = res["indicators"]
    closes = q.get("adjclose", [{}])[0].get("adjclose") if rng != "10d" else None
    closes = closes or q["quote"][0]["close"]
    return [(time.strftime("%Y-%m-%d", time.gmtime(a)), b) for a, b in zip(res["timestamp"], closes) if b is not None]


def check(area, item, site, fetch, source, tol, absolute=False, scale=1.0, note=""):
    """site: published value; fetch(): [(date, value)…] from the source; pass if any matches."""
    try:
        obs = fetch()
    except Exception as e:
        RESULTS.append(("FAIL", area, item, site, None, source, f"source error: {str(e)[:80]}"))
        return
    if not obs:
        RESULTS.append(("FAIL", area, item, site, None, source, "source returned nothing"))
        return
    if site is None or site == []:
        RESULTS.append(("FAIL", area, item, None, obs[-1][1] * scale, source, "missing on the site"))
        return
    # site may be one value or the site's own recent history (sparkline): any pair that matches passes
    sv = site if isinstance(site, list) else [site]
    gap_of = lambda x, ref: abs(x - ref) if absolute else (abs(x - ref) / abs(ref) if ref else math.inf)
    x, best = min(((x, o) for x in sv for o in obs), key=lambda p: gap_of(p[0], p[1][1] * scale))
    ref = best[1] * scale
    shown = x if isinstance(site, list) else site
    RESULTS.append(("OK" if gap_of(x, ref) <= tol else "FAIL", area, item, shown, round(ref, 6), f"{source} ({best[0]})", note))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="https://sainhe.com")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    site = a.site.rstrip("/")

    macro = get_json(site + "/data/macro.json")["data"]
    idx = {x["ticker"]: x for reg in get_json(site + "/data/indices.json")["data"]["par_region"].values() for x in reg}
    import json as _json
    spark = lambda t: _json.loads(idx[t]["sparkline"]) if t in idx and idx[t].get("sparkline") else []
    val = lambda k: (macro.get(k) or {}).get("value")
    vmap = get_json(site + "/data/stock/valuation_map.json")
    rows = {r["ticker"]: r for r in vmap["rows"]}

    # ── Dashboard: rates, spreads, central banks (absolute tolerance in points / bp) ──
    # FRED publishes a day later than Yahoo: the site's 7-day history is matched against FRED's last days
    check("dash", "US 10Y (%)", spark("^TNX") or val("us_10y"), lambda: fred_obs("DGS10", 4), "FRED DGS10", 0.06, True)
    check("dash", "US 3M (%)", val("us_3m"), lambda: fred_obs("DTB3", 4), "FRED DTB3", 0.10, True)
    check("dash", "VIX", spark("^VIX") or val("vix"), lambda: fred_obs("VIXCLS", 4), "FRED VIXCLS", 0.03)
    for k, s in (("hy_oas_us", "BAMLH0A0HYM2"), ("hy_oas_eu", "BAMLHE00EHYIOAS"), ("hy_oas_em", "BAMLEMHBHYCRPIOAS")):
        check("dash", k + " (bp)", val(k), lambda s=s: fred_obs(s, 4), "FRED " + s, 3, True, scale=100)
    check("dash", "TIPS 10Y (%)", val("us_tips_10y"), lambda: fred_obs("DFII10", 4), "FRED DFII10", 0.03, True)
    check("dash", "Breakeven 10Y (%)", val("us_breakeven_10y"), lambda: fred_obs("T10YIE", 4), "FRED T10YIE", 0.03, True)
    check("dash", "Fed balance sheet (T$)", val("fed_balance_sheet"), lambda: fred_obs("WALCL", 3), "FRED WALCL", 0.005, scale=1e-6)
    check("dash", "Bund 10Y (%)", val("bund_10y"), lambda: bundesbank_obs("R10XX", 4), "Bundesbank", 0.011, True)
    check("dash", "JGB 10Y (%)", val("jgb_10y"), lambda: fred_obs("IRLTLT01JPM156N", 2), "FRED OECD monthly", 0.35, True,
          note="monthly average vs daily: wide tolerance")
    check("dash", "USD/JPY", val("jpy_usd"),
          lambda: [(d, j / u) for (d, j), (_, u) in zip(ecb_obs("EXR/D.JPY.EUR.SP00.A", 4), ecb_obs("EXR/D.USD.EUR.SP00.A", 4))],
          "ECB cross", 0.015)

    # ── Stock Analysis: risk-free, ERP, FX, internal consistency ──
    rf = lambda cur: next((r["rf"] * 100 for r in rows.values() if r.get("rf_currency") == cur and not r.get("rf_proxy")), None)
    # Stock Analysis takes its risk-free rates from the dashboard's macro data at ITS run (early
    # morning); the dashboard may have refreshed since. Same source, other day: the rate must equal
    # one of the source's recent values, not today's dashboard figure (a wrong source still fails).
    check("stock", "rf USD from the US 10Y series", rf("USD"),
          lambda: [(str(i), v) for i, v in enumerate(spark("^TNX"))] + [("today", val("us_10y") or 0)],
          "site's ^TNX history (7 days)", 0.0015, True)
    check("stock", "rf EUR from the Bund series", rf("EUR"), lambda: bundesbank_obs("R10XX", 6), "Bundesbank", 0.006, True)
    if rf("CHF") is not None:
        check("stock", "rf CHF (%)", rf("CHF"), lambda: fred_obs("IRLTLT01CHM156N", 2), "FRED OECD", 0.05, True)

    def damodaran():
        import openpyxl
        b = S.get(vmap["erp"]["url"], timeout=60).content
        ws = openpyxl.load_workbook(io.BytesIO(b), data_only=True, read_only=True).worksheets[0]
        last = None
        for r in ws.iter_rows(values_only=True):
            if r and r[0] is not None and any(isinstance(c, (int, float)) and 0 < c < 0.1 for c in r[1:]):
                last = r
        return [(str(last[0])[:10], c) for c in last[1:] if isinstance(c, (int, float)) and 0.01 < c < 0.1]
    check("stock", "ERP Damodaran", (vmap.get("erp") or {}).get("erp"), damodaran, "Damodaran ERPbymonth.xlsx", 0.0005, True)
    usd = ecb_obs("EXR/D.USD.EUR.SP00.A", 4)
    check("stock", "EUR→USD", vmap["fx"]["EUR"]["usd_per_unit"], lambda: usd, "ECB reference", 0.01)
    check("stock", "CHF→USD", vmap["fx"]["CHF"]["usd_per_unit"],
          lambda: [(d, u / c) for (d, c), (_, u) in zip(ecb_obs("EXR/D.CHF.EUR.SP00.A", 4), usd)], "ECB cross", 0.012)

    # ── Stock Analysis: share prices (main listings) ──
    for t, q in (("AAPL", "AAPL"), ("MSFT", "MSFT"), ("KO", "KO"), ("ASML", "ASML.AS"), ("RMS.PA", "RMS.PA"), ("NESN.SW", "NESN.SW")):
        if t in rows:
            check("stock", f"{t} price", rows[t]["prix"], lambda q=q: yahoo_closes(q), f"Yahoo {q}", 0.01)

    # ── Stock Analysis: filings (SEC) ──
    email = os.environ.get("EDGAR_CONTACT_EMAIL")
    env = Path(__file__).resolve().parent.parent / ".env"
    if not email and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("EDGAR_CONTACT_EMAIL="):
                email = line.split("=", 1)[1].strip().strip('"')
    if email:
        hdr = {"User-Agent": f"SAINHE verify_live {email}"}
        for t in ("AAPL", "MSFT", "KO"):
            rep = get_json(f"{site}/data/stock/{t}_report.json")
            rev = rep["bloc_D_trends"]["revenue_10y"]
            fy = max(rev, key=int)

            def sec(cik=rep["cik"], fy=fy):
                f = S.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers=hdr, timeout=60).json()
                out = []
                for tag in ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"):
                    for x in f["facts"].get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", []):
                        if x.get("form") == "10-K" and x.get("fp") == "FY" and x.get("fy") is not None and x["end"][:4] == str(fy) \
                                and x.get("start") and (int(x["end"][:4]) - int(x["start"][:4])) * 12 + int(x["end"][5:7]) - int(x["start"][5:7]) >= 11:
                            out.append((x["end"], float(x["val"])))
                return out
            check("stock", f"{t} revenue FY{fy}", rev[fy], sec, "SEC companyfacts", 0.001)
            time.sleep(0.3)
    else:
        RESULTS.append(("SKIP", "stock", "SEC revenue", None, None, "SEC", "EDGAR_CONTACT_EMAIL not set"))

    # ── Lab: 5-year volatility recomputed from Yahoo adjusted closes ──
    lab = get_json(site + "/data/stock/lab_data.json")
    d0, d1 = lab["main"]["dates"][0], lab["main"]["dates"][-1]
    for t in ("AAPL", "KO"):
        rs = [v / 1e5 for v in lab["main"]["r"][t][1:] if v is not None]
        m = sum(rs) / len(rs)
        vol = (sum((x - m) ** 2 for x in rs) / len(rs)) ** .5 * 252 ** .5

        def ref(t=t):
            h = [x for x in yahoo_closes(t, "6y") if d0 <= x[0] <= d1]
            r = [h[i][1] / h[i - 1][1] - 1 for i in range(1, len(h))]
            mm = sum(r) / len(r)
            return [(d1, (sum((x - mm) ** 2 for x in r) / len(r)) ** .5 * 252 ** .5)]
        check("lab", f"{t} 5y volatility", vol, ref, "Yahoo, recomputed", 0.03)

    # ── report ──
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    lines = [f"# SAINHE — live data check ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})", "",
             f"Site: {site} · {len(RESULTS)} checks · **{len(fails)} failing**", "",
             "| | Area | Item | Site | Source value | Source | Note |", "|---|---|---|---|---|---|---|"]
    for st, area, item, s_, ref, src, note in RESULTS:
        lines.append(f"| {'✅' if st == 'OK' else ('⏭️' if st == 'SKIP' else '❌')} | {area} | {item} | {s_} | {ref} | {src} | {note} |")
    md = "\n".join(lines)
    print(md)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
