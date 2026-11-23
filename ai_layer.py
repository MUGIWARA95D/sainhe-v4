"""
ai_layer.py — makes sainhe.com readable by AI agents, search engines and screen readers.

Problem (audit of 23 Nov 2026): every figure on the site is drawn by JavaScript. A crawler that
does not run JavaScript (GPTBot, ClaudeBot, PerplexityBot, most RAG fetchers) read 522 words and
ZERO numbers on the dashboard, and 5 numbers on Stock Analysis.

What this module writes, at each build, from the SAME JSON the pages draw (no second calculation):
  1. a static text version of the figures inside each page (<details> at the end of <main>):
     readable without JavaScript, by crawlers and by screen readers (the charts are canvases);
  2. one Markdown mirror per page (/dashboard.md, /stock-analysis.md, /learn.md, /about.md),
     announced in each page head with <link rel="alternate" type="text/markdown">;
  3. /llms.txt (index, llmstxt.org convention) and /llms-full.txt (everything in one file);
  4. /sitemap.xml and /robots.txt (which points to both).
Called by render_html.render() after the pages are written.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from config import SITE_NAME, SITE_URL, WATCHLIST

DISCLAIMER = ("Informational only, not investment advice. SAINHE publishes measurements computed from public "
              "data; it does not recommend buying or selling any security.")
GITHUB = "https://github.com/MUGIWARA95D/sainhe-v4"

PAGES = [  # html file, clean URL, markdown mirror, title
    ("index.html", "/", "/dashboard.md", "Dashboard — macro, indices, sectors, rotation, watchlist"),
    ("stock-analysis.html", "/stock-analysis", "/stock-analysis.md", "Stock Analysis — what the price assumes vs what the accounts show"),
    ("learn.html", "/learn", "/learn.md", "Learn — plain-language guides to every number on the site"),
    ("contact.html", "/contact", "/about.md", "About — method, sources, engineering, legal"),
]


# ───────────────────────────── formatting ─────────────────────────────
def _num(v, d=2):
    return "N/D" if not isinstance(v, (int, float)) else f"{v:,.{d}f}"


def _pct(v, d=1, signed=True):
    if not isinstance(v, (int, float)):
        return "N/D"
    s = f"{abs(v) * 100:.{d}f}%"
    return ("−" if v < 0 else ("+" if signed and v > 0 else "")) + s


def _esc(s):
    return _html.escape(str(s), quote=True)


class Table:
    """One row model rendered both as Markdown and as HTML (the two can never disagree)."""

    def __init__(self, caption: str, headers: list[str], rows: list[list], note: str = ""):
        self.caption, self.headers, self.rows, self.note = caption, headers, rows, note

    def md(self) -> str:
        cell = lambda c: str(c).replace("|", "/").replace("\n", " ")
        out = [f"### {self.caption}", ""]
        if self.note:
            out += [self.note, ""]
        out.append("| " + " | ".join(self.headers) + " |")
        out.append("|" + "---|" * len(self.headers))
        out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in self.rows]
        return "\n".join(out) + "\n"

    def html(self) -> str:
        th = "".join(f'<th scope="col">{_esc(h)}</th>' for h in self.headers)
        trs = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>" for r in self.rows)
        note = f"<p>{_esc(self.note)}</p>" if self.note else ""
        return (f'<h3>{_esc(self.caption)}</h3>{note}<div class="ai-snap-tw"><table>'
                f"<thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>")


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ───────────────────────────── dashboard ─────────────────────────────
MACRO_LABELS = {
    "vix": "VIX (S&P 500 implied volatility)", "erp": "Earnings yield gap (S&P 500 earnings yield − US 10Y)",
    "yield_curve_10y_3m": "US yield curve 10Y − 3M", "us_10y": "US 10-year Treasury yield", "us_3m": "US 3-month bill yield",
    "dxy": "US dollar index (DXY)", "sp500_pe": "S&P 500 trailing P/E", "cape_us": "Shiller CAPE (US)",
    "fed_balance_sheet": "Fed balance sheet", "ecb_balance_sheet": "ECB balance sheet", "reverse_repo": "Fed reverse repo",
    "treasury_general_acct": "US Treasury General Account", "us_m2_yoy": "US M2 growth y/y", "eu_m3_yoy": "Euro area M3 growth y/y",
    "japan_m2_yoy": "Japan M2 growth y/y", "hy_oas_us": "US high-yield spread (ICE BofA OAS)",
    "hy_oas_eu": "Euro high-yield spread (ICE BofA OAS)", "hy_oas_em": "EM high-yield spread (ICE BofA OAS)",
    "bund_10y": "German Bund 10Y (Bundesbank)", "bund_2y": "German Bund 2Y (Bundesbank)", "jgb_10y": "Japan JGB 10Y",
    "jgb_2y": "Japan JGB 2Y", "cgb_10y": "China government bond 10Y", "uk_10y": "UK Gilt 10Y",
    "us_tips_10y": "US 10Y real yield (TIPS)", "us_breakeven_10y": "US 10Y inflation breakeven", "pe_eu": "Europe P/E (VGK)",
    "pe_jp": "Japan P/E (EWJ)", "pe_em": "Emerging markets P/E (EEM)", "pe_cn": "China P/E (MCHI)",
    "copper_gold_ratio": "Copper / gold ratio", "jpy_usd": "USD/JPY", "cnh_usd": "USD/CNH", "gold_stocks": "Gold / S&P 500 ratio",
    "global_cb_trend": "Central-bank balance sheets (Fed + ECB)", "fed_balance_sheet_wow": "Fed balance sheet, week on week",
    "ecb_balance_sheet_wow": "ECB balance sheet, week on week",
}
UNIT_FMT = {
    "pct": lambda v: f"{v:.2f}%", "pct_points": lambda v: f"{v:+.2f} pts", "bps": lambda v: f"{v:.0f} bp",
    "ratio": lambda v: f"{v:.4g}", "index": lambda v: f"{v:,.2f}", "trillion_usd": lambda v: f"${v:.2f} T",
    "trillion_eur": lambda v: f"€{v:.2f} T", "billion_usd": lambda v: f"${v:,.1f} bn", "fx_per_usd": lambda v: f"{v:,.4g} per USD",
}


def dashboard_tables(data: Path) -> tuple[str, list[Table]]:
    macro = _load(data / "macro.json") or {}
    tables = []
    rows = []
    for k, m in (macro.get("data") or {}).items():
        v, u = m.get("value"), m.get("unit")
        if v is None:
            continue
        if k == "erp":
            shown = f"{v * 100:+.2f}%"
        else:
            shown = UNIT_FMT.get(u, lambda x: str(x))(v) if isinstance(v, (int, float)) else str(v)
        th = m.get("thresholds")
        rows.append([MACRO_LABELS.get(k, k), shown, m.get("regime") or "",
                     ", ".join(f"{a} {b}" for a, b in th.items()) if th else ""])
    tables.append(Table("Macro and liquidity signals", ["Indicator", "Value", "Regime", "Regime boundaries used"], rows,
                        f"Sources: {macro.get('source', '')}. As of {macro.get('as_of', '')}."))

    ind = (_load(data / "indices.json") or {}).get("data", {}).get("par_region", {})
    rows = [[x.get("nom"), x.get("ticker"), _num(x.get("close")), x.get("native_devise") or "",
             _pct(x.get("chg_pct"), 2), _pct(x.get("ret_1m")), _pct(x.get("ret_1y")),
             {1: "above", 0: "below"}.get(x.get("above_dma200"), "N/D")]
            for reg in ind.values() for x in reg]
    tables.append(Table("Global indices", ["Index", "Ticker", "Level / close", "Quote currency", "Day", "1 month", "1 year", "vs 200-day average"], rows,
                        "Index levels are in points (native); returns in local terms. Source: Yahoo Finance."))

    sec = (_load(data / "sectors.json") or {}).get("data", {})
    rows = []
    for s in sec.get("secteurs", []):
        w = (s.get("regions") or {}).get("MONDE") or {}
        if w:
            rows.append([s.get("nom"), w.get("ticker"), _pct(w.get("chg_pct"), 2), _pct(w.get("ret_1m")),
                         _pct(w.get("ret_3m")), _pct(w.get("ret_1y")), {1: "above", 0: "below"}.get(w.get("above_dma200"), "N/D")])
    tables.append(Table("Sectors, world (sector ETFs)", ["Sector", "ETF", "Day", "1 month", "3 months", "1 year", "vs 200-day average"], rows,
                        "Regional splits (USA, Europe, Asia) are in /data/sectors.json."))

    sen = (_load(data / "sentiment.json") or {}).get("data", {})
    b, rg = sen.get("breadth") or {}, sen.get("regime") or {}
    if b:
        tables.append(Table("Rotation and breadth", ["Measure", "Value"],
                            [["Sector ETFs above their 200-day average", f"{b.get('count_above')}/{b.get('count_total')} ({b.get('pct')}%)"],
                             ["Breadth label", b.get("label", "")],
                             ["Regime", rg.get("label", "") if isinstance(rg, dict) else str(rg)]]))

    port = (_load(data / "portfolio.json") or {}).get("data", {})
    rows = [[x.get("ticker"), x.get("nom"), x.get("secteur"), x.get("native_devise"), _num(x.get("close")),
             _pct(x.get("chg_pct"), 2), _pct(x.get("ret_1m")), _pct(x.get("ret_3m")), _pct(x.get("ret_1y")),
             {1: "above", 0: "below"}.get(x.get("above_dma200"), "N/D"), (x.get("signal") or "").title()]
            for x in port.get("tickers", [])]
    tables.append(Table("Watchlist", ["Ticker", "Company", "Sector", "Currency", "Close (native)", "Day", "1 month", "3 months", "1 year",
                                      "vs 200-day average", "Momentum label"], rows,
                        "The momentum label is a mechanical reading of price trend and volume (200-day average, money flow, "
                        "on-balance volume), not a recommendation. The author's personal watchlist; he may or may not hold positions."))

    news = (_load(data / "news.json") or {}).get("data", {}).get("par_region", {})
    items = sorted((a for reg in news.values() for a in reg), key=lambda a: a.get("ts_pub") or "", reverse=True)[:12]
    rows = [[(a.get("ts_pub") or "")[:16].replace("T", " "), a.get("titre"), a.get("lien")] for a in items]
    tables.append(Table("Latest headlines", ["Published (UTC)", "Headline", "Link"], rows, "RSS: Bloomberg, CNBC, MarketWatch, SCMP, FT."))
    return macro.get("as_of", ""), tables


# ───────────────────────────── stock analysis ─────────────────────────────
def stock_tables(data: Path) -> tuple[str, list[Table], dict]:
    v = _load(data / "stock" / "valuation_map.json")
    if not v:
        return "", [], {}
    names = {t: w.get("nom", t) for t, w in WATCHLIST.items()}
    rows = []
    for r in sorted(v.get("rows", []), key=lambda r: r["ticker"]):
        sg = r.get("sgr")
        sgr_txt = "n/m" if r.get("sgr_status") == "n/m" else _pct(sg, 1, False)
        gap = (r["implied_growth"] - sg) if isinstance(r.get("implied_growth"), (int, float)) and isinstance(sg, (int, float)) \
            and r.get("sgr_status") != "n/m" else None
        rows.append([r["ticker"], names.get(r["ticker"], ""), r.get("source", "").split(" — ")[0], r.get("fiscal_year_end") or "",
                     f"{_num(r.get('prix'))} {r.get('currency', '')}", _pct(r.get("implied_growth"), 1, False), sgr_txt,
                     ("N/D" if gap is None else ("−" if gap < 0 else "+") + f"{abs(gap) * 100:.1f} pts"), r.get("roic_years_above_wacc") or "N/D",
                     _pct(r.get("wacc"), 1, False), f"{r.get('rf_currency', '')} {_pct(r.get('rf'), 2, False)}",
                     f"{_num(r.get('epv_per_share'))} {r.get('currency', '')}" if isinstance(r.get("epv_per_share"), (int, float)) else "N/D",
                     _pct(r.get("ecart_pct"), 0), ", ".join(f["id"] for f in r.get("flags", [])) or "—"])
    erp = v.get("erp") or {}
    t1 = Table("Quality × Price — every company", [
        "Ticker", "Company", "Accounts", "Fiscal year end", "Price (accounts currency)", "Implied growth (reverse DCF)",
        "Sustainable growth (ROIC × reinvestment)", "Gap", "Years ROIC > WACC", "WACC", "Risk-free (10Y)", "EPV per share",
        "EPV vs price", "Flags"], rows,
        f"Implied growth = perpetual free-cash-flow growth that the market price assumes, solved from EV = FCF×(1+g)/(WACC−g). "
        f"EPV = earnings power value (no growth). Equity risk premium in the WACC: {_pct(erp.get('erp'), 2, False)} "
        f"(Damodaran implied ERP, {erp.get('date', '')}). n/m = not meaningful (e.g. negative net reinvestment); "
        f"N/D = not available in the filings, never imputed.")
    oos = v.get("out_of_scope") or {}
    t2 = Table("Watchlist names outside the valuation scope", ["Ticker", "Reason"], [[k, x] for k, x in oos.items()])
    lab = _load(data / "stock" / "lab_data.json") or {}
    excl = ", ".join(e["t"] for e in lab.get("excluded", []))
    t3 = Table("Allocation Lab, Range of outcomes, Crisis replay", ["Module", "What it computes"], [
        ["Allocation Lab", f"Five years of daily returns of {len(lab.get('holdings', []))} holdings, in USD, EUR, CHF or HKD; presets: equal weight, "
                           f"inverse volatility, risk clusters (HRP), best Sharpe, lowest volatility. Excluded (history too short): {excl or 'none'}."],
        ["Range of outcomes", "5,000-path stationary block bootstrap (Politis & Romano 1994, mean block 21 days, fixed seed) of the allocation's own "
                              "returns, raw and tail-adjusted (generalised Pareto tail fitted on S&P 500 days since 1993)."],
        ["Crisis replay", "Today's companies through 2000–02, 2008–09, 2020 and 2022 (S&P 500 peak to trough), only those already listed, "
                          "weights rescaled, coverage shown; Kritzman–Li turbulence."],
    ])
    return v.get("as_of", ""), [t1, t2, t3], v


# ───────────────────────────── learn (from the rendered page) ─────────────────────────────
def learn_markdown(out: Path) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((out / "learn.html").read_text(encoding="utf-8"), "html.parser")
    main = soup.find(id="main") or soup
    for s in main(["script", "style", "svg", "canvas", "button"]):
        s.decompose()
    lines = []
    for el in main.find_all(["h2", "h3", "div"]):
        cls = el.get("class") or []
        if el.name == "h2":
            lines += ["", f"## {el.get_text(' ', strip=True)}", ""]
        elif el.name == "h3":
            lines += ["", f"### {el.get_text(' ', strip=True)}", ""]
        elif "gr-title" in cls:
            lines += ["", f"### {el.get_text(' ', strip=True)}"]
        elif "gr-body" in cls or "lrn-panel-row-val" in cls:
            lbl = el.find_previous_sibling("div", class_="lrn-panel-row-lbl")
            head = f"**{lbl.get_text(' ', strip=True)}** — " if lbl is not None and "lrn-panel-row-val" in cls else ""
            lines.append(head + el.get_text(" ", strip=True))
            lines.append("")
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


# ───────────────────────────── about / engineering ─────────────────────────────
ABOUT = f"""## What SAINHE is

A free, non-commercial investment-analysis site built and maintained by one student, Hugo Waldmeyer. It measures and flags; it never
forecasts prices or recommends trades. Every figure carries its source.

## How a number reaches the page

1. **Fetch** (GitHub Actions, several times a day): prices and FX from Yahoo Finance and the ECB, Gazprom from the Moscow Exchange,
   rates from FRED, the ECB, the Bundesbank, Japan's Ministry of Finance and ChinaBond, valuation from multpl.com, the implied equity
   risk premium from Aswath Damodaran, headlines from RSS.
2. **Filings**: US 10-K and 20-F from SEC EDGAR (XBRL company facts, and the filing's own XBRL instance when the API lags),
   European annual reports from ESEF (filings.xbrl.org). Yahoo statements only where no machine-readable filing exists, shown as
   secondary. A missing figure is N/D, never interpolated or imputed.
3. **Compute** (Python, SQLite): returns, moving averages, money flow; reverse DCF, earnings power value, residual earnings, ROIC vs
   WACC per currency; portfolio statistics, bootstrap Monte Carlo with an extreme-value tail, crisis replay, turbulence.
4. **Render**: static HTML (Jinja2) plus a JSON data layer (/data/*.json), deployed to Cloudflare Pages.
5. **Check**: unit tests on the valuation engine, the portfolio engine and the front-end arithmetic run on every change; a quality
   pass cross-checks XBRL against a second source; every Monday a job compares the published values with FRED, the ECB, the
   Bundesbank, Damodaran, the SEC and Yahoo and opens an issue on any drift or silent source.

## Rules the code enforces

- No forecasts: the only forward-looking figures are what the price implies (reverse DCF) and external analyst consensus.
- Free public data only; every threshold lives in configuration, none is hard-coded.
- Each number links to its formula, inputs and filing (XBRL tag, accession number).
- No language model anywhere in the data pipeline: everything is deterministic and reproducible.

## Source code

Public repository: {GITHUB}

## Legal

{DISCLAIMER} No cookies, no tracking, no analytics. Hosted by Cloudflare. Full text: {SITE_URL}/contact#ct-legal
"""


# ───────────────────────────── writers ─────────────────────────────
def _snapshot_html(title: str, as_of: str, tables: list[Table], md_url: str) -> str:
    body = "".join(t.html() for t in tables)
    return (f'<section class="ai-snap" aria-label="Text version of the figures">'
            f'<details><summary>Text version of this page\'s figures — for screen readers, search engines and AI agents</summary>'
            f'<p>{_esc(title)}. As of {_esc(as_of)}. Same data as the charts above, read from the published JSON. '
            f'Markdown: <a href="{md_url}">{md_url}</a> · data: <a href="/data/index.json">/data/index.json</a> · '
            f'overview: <a href="/llms.txt">/llms.txt</a>. {_esc(DISCLAIMER)}</p>{body}</details></section>')


def _inject(html_path: Path, md_url: str, snapshot: str | None):
    s = html_path.read_text(encoding="utf-8")
    s = re.sub(r'<section class="ai-snap".*?</section>', "", s, flags=re.S)
    link = f'<link rel="alternate" type="text/markdown" href="{md_url}" title="Markdown version">'
    if link not in s:
        s = s.replace("</head>", f"  {link}\n</head>", 1)
    if snapshot:
        i = s.rfind("</main>")
        if i > 0:
            s = s[:i] + snapshot + s[i:]
    html_path.write_text(s, encoding="utf-8")


def build(out: Path, ts_render: str, stock_live: bool) -> dict:
    out = Path(out)
    data = out / "data"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    docs = {}

    d_asof, d_tables = dashboard_tables(data)
    docs["/dashboard.md"] = (f"# {SITE_NAME} — Dashboard\n\n> Macro and liquidity signals, 15 global indices, 16 sectors × 4 regions, "
                             f"rotation breadth and the watchlist. As of {d_asof} (built {ts_render}). Page: {SITE_URL}/\n\n"
                             f"{DISCLAIMER}\n\n" + "\n".join(t.md() for t in d_tables) +
                             "\nRaw data with units and thresholds: /data/index.json (manifest), /data/macro.json, /data/indices.json, "
                             "/data/sectors.json, /data/sentiment.json, /data/portfolio.json, /data/news.json.\n")
    _inject(out / "index.html", "/dashboard.md", _snapshot_html("SAINHE dashboard", d_asof, d_tables, "/dashboard.md"))

    s_asof, s_tables, vmap = stock_tables(data) if stock_live else ("", [], {})
    if s_tables:
        reports = sorted(p.name for p in (data / "stock").glob("*_report.json"))
        docs["/stock-analysis.md"] = (
            f"# {SITE_NAME} — Stock Analysis\n\n> For each company: the growth its share price already assumes (reverse DCF) against the "
            f"returns its own accounts show (ROIC vs WACC, sustainable growth), with every input traced to the filing. "
            f"As of {s_asof} (built {ts_render}). Page: {SITE_URL}/stock-analysis\n\n{DISCLAIMER}\n\n"
            + "\n".join(t.md() for t in s_tables)
            + "\n### Full audit trail per company\n\nEach report holds every input, its XBRL tag, fiscal year and filing: "
            + ", ".join(f"[{r.replace('_report.json', '')}](/data/stock/{r})" for r in reports)
            + ".\nMap data: /data/stock/valuation_map.json · Lab and crises: /data/stock/lab_data.json · quality checks: /data/stock/quality_report.json\n")
        _inject(out / "stock-analysis.html", "/stock-analysis.md",
                _snapshot_html("SAINHE stock analysis", s_asof, s_tables, "/stock-analysis.md"))

    docs["/learn.md"] = (f"# {SITE_NAME} — Learn\n\n> Plain-language guides: why finance matters, the golden rules, how to read "
                         f"every box of the dashboard and every section of Stock Analysis. Page: {SITE_URL}/learn\n\n" + learn_markdown(out))
    _inject(out / "learn.html", "/learn.md", None)

    docs["/about.md"] = f"# {SITE_NAME} — About, method and engineering\n\n> Page: {SITE_URL}/contact\n\n" + ABOUT
    _inject(out / "contact.html", "/about.md", None)

    for url, text in docs.items():
        (out / url.lstrip("/")).write_text(text, encoding="utf-8")

    # llms.txt (index) + llms-full.txt (everything)
    llms = [f"# {SITE_NAME}", "",
            "> Free investment-analysis site: macro signals, global markets and a 28-company stock analysis that shows what each "
            "share price assumes against what the company's filings show. Every number is traced to its source; nothing is forecast.",
            "", DISCLAIMER, "", "## Pages (Markdown versions, regenerated at every build)", ""]
    llms += [f"- [{t}]({md}): HTML at {SITE_URL}{u}" for _, u, md, t in PAGES if md in docs]
    llms += ["", "## Data (JSON, with units and sources)", "",
             "- [Manifest](/data/index.json): every dataset, its source and timestamp",
             "- [Macro](/data/macro.json): each metric with value, unit, thresholds and regime",
             "- [Indices](/data/indices.json), [Sectors](/data/sectors.json), [Rotation](/data/sentiment.json), "
             "[Watchlist](/data/portfolio.json), [News](/data/news.json)"]
    if s_tables:
        llms += ["- [Valuation map](/data/stock/valuation_map.json): one row per company (implied growth, ROIC, WACC, EPV, flags)",
                 "- Per-company audit trail: /data/stock/{TICKER}_report.json (e.g. /data/stock/AAPL_report.json)",
                 "- [Lab data](/data/stock/lab_data.json): daily returns in 1e-5 units, FX, crisis windows"]
    llms += ["", "## Notes for agents", "",
             "- Quote the as-of date: market figures change daily, filings quarterly.",
             "- N/D means the figure is not in the filings; it is never estimated. n/m means not meaningful by construction.",
             "- The dashboard's « earnings yield gap » is not the equity risk premium used in Stock Analysis (Damodaran implied ERP).",
             "- Momentum labels on the watchlist are mechanical price and volume readings, not recommendations: "
             "Buy = 3-month return above +5 %, price above its 200-day average and on-balance volume not distributing; "
             "Avoid = 3-month return below −5 %, or price below the 200-day average with on-balance volume distributing; "
             "Watch = every other case.",
             "- Regime boundaries for each macro indicator are in /data/macro.json (field thresholds).",
             "", "## Optional", "", f"- [Everything in one file](/llms-full.txt)", f"- [Source code]({GITHUB})"]
    (out / "llms.txt").write_text("\n".join(llms) + "\n", encoding="utf-8")
    (out / "llms-full.txt").write_text("\n\n---\n\n".join(docs[m] for _, _, m, _ in PAGES if m in docs), encoding="utf-8")

    # sitemap + robots
    urls = [u for _, u, _, _ in PAGES] + list(docs) + ["/llms.txt", "/llms-full.txt", "/data/index.json"]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"  <url><loc>{SITE_URL}{u}</loc><lastmod>{now}</lastmod></url>" for u in urls]
    sm.append("</urlset>")
    (out / "sitemap.xml").write_text("\n".join(sm) + "\n", encoding="utf-8")
    (out / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n\n"
        "# AI agents: start at /llms.txt (overview and Markdown versions of every page), raw data at /data/index.json\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
    return {"docs": sorted(docs), "dashboard_tables": len(d_tables), "stock_tables": len(s_tables)}
