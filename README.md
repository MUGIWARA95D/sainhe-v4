<p align="center">
  <img src="assets/sainhe-banner.svg" alt="SAINHE — Fundamental Intelligence" width="100%">
</p>

<p align="center">
  <a href="https://sainhe.com"><img src="https://img.shields.io/badge/live-sainhe.com-9E7A2E?style=flat-square&labelColor=2C2C2C" alt="Live site"></a>
  <a href="https://github.com/MUGIWARA95D/sainhe-v4/actions/workflows/sainhe.yml"><img src="https://github.com/MUGIWARA95D/sainhe-v4/actions/workflows/sainhe.yml/badge.svg" alt="Data pipeline"></a>
  <a href="https://github.com/MUGIWARA95D/sainhe-v4/actions/workflows/verify-live.yml"><img src="https://github.com/MUGIWARA95D/sainhe-v4/actions/workflows/verify-live.yml/badge.svg" alt="Weekly check of the published values"></a>
  <img src="https://img.shields.io/badge/Python-3.11-9E7A2E?style=flat-square&labelColor=2C2C2C" alt="Python 3.11">
</p>

<p align="center">
  <b>A free investment-analysis site that measures and never forecasts.</b><br>
  Every number on the page opens its formula, its inputs and the filing it came from.
</p>

<p align="center"><a href="https://sainhe.com"><b>→ sainhe.com</b></a> · <a href="https://sainhe.com/llms.txt">llms.txt</a> · <a href="https://sainhe.com/data/index.json">JSON data</a></p>

---

## What it is

SAINHE is a one-person project: a static website rebuilt several times a day by GitHub Actions from free public data.

| Page | What you get |
|---|---|
| **Dashboard** | Macro and liquidity signals (VIX, curves, credit spreads, central-bank balance sheets, real rates), 15 global indices, 16 sectors × 4 regions, rotation breadth, and a 32-name watchlist. |
| **Stock Analysis** | For 28 companies: the growth the share price already assumes (reverse DCF) against the returns the accounts show (ROIC vs WACC, sustainable growth), earnings power value, residual earnings. An allocation lab, a 5,000-path Monte Carlo and a replay of four crises. |
| **Learn** | Plain-language explanations of every box, reachable from each `(i)` on the other pages. |

**What makes it different is that the numbers can be checked.** Click any figure in Stock Analysis and the drawer shows the formula with the numbers plugged in, every input, its XBRL tag, the fiscal year and a link to the SEC or ESEF filing. When a figure is not in the filings, the page says `N/D`; nothing is interpolated or imputed.

## Rules the code enforces

- **No forecasts.** The only forward-looking figures are what the market price implies and the external analyst consensus.
- **No language model in the pipeline.** Everything is deterministic and reproducible.
- **Free public data only.** SEC EDGAR, ESEF, FRED, the ECB, the Bundesbank, Damodaran, Yahoo Finance, the Moscow Exchange, RSS.
- **No magic numbers.** Every threshold lives in [`config.py`](./config.py) or [`sainhe_backend/config.py`](./sainhe_backend/config.py), with its source in a comment.
- **One calculation, two outputs.** Each dashboard box has a single `render(con)` that feeds both the HTML and the JSON layer.

## Architecture

```
                 ┌── fetch_prices / fetch_fx / fetch_macro_liquidity / fetch_news ──┐
 Yahoo, ECB,     │   (several times a day)                                          │
 FRED, Bundes-   ▼                                                                  │
 bank, MoF, …  SQLite (sainhe.db, synced to Google Drive) ── calc.py ── boxes/*.py ─┤
                                                                                    ├─► render_html.py ─► static HTML
 SEC EDGAR ──┐                                                                      │      + /data/*.json
 ESEF ───────┼─► sainhe_backend/pipeline.py ─► data/stock/*_report.json ────────────┤      + ai_layer.py: llms.txt,
 Yahoo (2nd)─┘   portfolio_runner.py ─────────► lab_data.json                       │        Markdown mirrors, sitemap
                 quality_check.py ────────────► quality_report.json ────────────────┘            │
                                                                                                 ▼
                                                                          Cloudflare Pages (sainhe.com)
                                                                                                 │
                                         scripts/verify_live.py (weekly) ◄── published values ───┘
                                         compares them with FRED, ECB, Bundesbank, Damodaran, SEC, Yahoo
```

No server runs at request time: the site is static files on a CDN, and every chart is computed in the browser from the published JSON.

## Engineering highlights

**Reading financial filings across three regimes.** US 10-K (US-GAAP) and 20-F (IFRS) come from SEC EDGAR, European annual reports from ESEF (filings.xbrl.org). The parser maps both taxonomies to one schema. Along the way it:
- picks the dominant reporting currency (TSMC reports in TWD and provides a USD convenience translation);
- builds totals from their components when only the parts are tagged (depreciation, short-term debt);
- reads the filing's own XBRL instance when the SEC API lags behind a new 20-F;
- reads company-specific extension tags (NVIDIA capex before 2022, Deere long-term debt).

**A valuation engine with explicit rules.**
- **Valuation measures:** reverse DCF solved in closed form, earnings power value, residual earnings.
- **WACC per currency:** the risk-free rate is the 10-year sovereign yield of the currency of the accounts. The equity risk premium is Damodaran's implied ERP. Beta is weekly over 5 years against MSCI World.
- **Price:** valuation uses the main listing (ASML in Amsterdam, TSMC in Taipei), converted to the currency of the accounts and to one ordinary share.
- **Edge cases resolved by rule, never by guess:**
  - abnormal tax years use the median of normal years, then the cash tax rate from the same filing;
  - an operating income that stopped being tagged gets an explicit, flagged proxy;
  - a spin-off after the last filing suspends the valuation;
  - negative or oversized reinvestment makes growth "not meaningful" rather than a misleading number.

**Portfolio statistics that admit their limits.**
- **Monte Carlo:** a stationary block bootstrap (Politis & Romano) of 5,000 paths, run in a Web Worker. Its extreme-value tail is a generalised Pareto fitted by probability-weighted moments on S&P 500 days since 1993.
- **Optimisers:** hierarchical risk parity and constrained optimisers, each shown with an out-of-sample check.
- **Crisis replay** of 2000–02, 2008–09, 2020 and 2022: it uses only companies already listed at the time, never proxies, with Kritzman–Li turbulence.
- **FX history:** ECB and Yahoo rates, with Federal Reserve H.10 series before 2004.
- **On the page:** look-ahead and selection bias are stated where they apply.

**Data you can trust because it is checked.**
- `quality_check.py` cross-checks filed figures against a second source and tracks regressions to `N/D`.
- `scripts/verify_live.py` runs every Monday. It compares the values actually published on sainhe.com with FRED, the ECB, the Bundesbank, Damodaran, the SEC and Yahoo, and opens an issue on any drift or on any source that stopped answering. This check was added after a free data source died silently.
- A daily keepalive re-enables scheduled workflows and alerts when data is older than 48 hours.
- Partial intraday price rows are filtered out before they reach the indicators.
- Preview and production are rendered in separate passes, so unpublished data cannot leak to production.

**Readable by people, crawlers and AI agents.** The charts are drawn by JavaScript, so [`ai_layer.py`](./ai_layer.py) also writes, at each build and from the same JSON:
- a static text version of every figure inside each page, which crawlers and screen readers can read;
- Markdown copies of the pages (`/dashboard.md`, `/stock-analysis.md`, `/learn.md`, `/about.md`);
- [`/llms.txt`](https://sainhe.com/llms.txt), `/llms-full.txt` and a sitemap;
- schema.org `Dataset` metadata.

On the dashboard, a crawler that does not run JavaScript went from 0 readable numbers to more than 300.

**A static front end without a framework.**
- Jinja2 templates with plain JavaScript and Chart.js.
- A small engine changes pages by swipe without reloading: it swaps `<main>` and re-runs that page's scripts.
- Every `(i)` links to the matching Learn topic, already open.
- A currency switch covers USD, EUR, CHF and HKD.

## Tests

| Suite | What it checks | Runs |
|---|---|---|
| `sainhe_backend/tests/test_pipeline.py` | Valuation engine on recorded filings, without network | every change, every data run |
| `sainhe_backend/tests/test_full.py` | Portfolio engine: returns, risk, HRP, Monte Carlo, stress tests | every change, every data run |
| `tests_front/test_calc.js` | The browser's arithmetic (Node) against the Python engine's own outputs | every change |
| `scripts/verify_live.py` | The live site against its sources | every Monday |

## Data sources

| Source | Used for |
|---|---|
| SEC EDGAR (XBRL company facts and filing instances) | US 10-K and foreign 20-F accounts |
| ESEF via filings.xbrl.org | European annual reports |
| FRED (St. Louis Fed) | US rates, credit spreads, Fed balance sheet, money supply, OECD long rates, H.10 FX history |
| ECB Data Portal · Deutsche Bundesbank | Euro-area data and reference FX · German Bund yields |
| Japan Ministry of Finance · ChinaBond | JGB and Chinese government yields |
| Aswath Damodaran (NYU Stern) | Implied equity risk premium, monthly |
| multpl.com | Shiller CAPE |
| Yahoo Finance | Prices, FX fallback, analyst consensus, and statements for 10 companies with no machine-readable filing (shown as secondary) |
| Moscow Exchange (ISS API) | Gazprom prices, which have had no Western quote since 2022 |
| RSS (Bloomberg, CNBC, MarketWatch, SCMP, FT) | Headlines, with TV and podcast blurbs filtered out |

## Automation

| Workflow (on `main`) | Cadence (UTC) | Does |
|---|---|---|
| `sainhe.yml` | news and macro 02, 08, 12, 16, 20 h · FX 10, 21 h · full pipeline 22 h on weekdays · every push to `dev` | Fetch, compute, render, deploy |
| `stock-analysis.yml` | 22:45 on weekdays · after each filing season | Tests, filings, valuation, lab, quality check, redeploy; opens an issue on anomalies |
| `verify-live.yml` | Monday 07:30 | Published values against their sources |
| `keepalive.yml` | daily 06:17 | Keeps schedules alive, alerts on stale data |

Pushing to `preview` deploys only to preview.sainhe.pages.dev for review; pushing to `dev` goes live.

## Repository layout

```
config.py              tickers, thresholds, sources (dashboard)
schema.py              contract of the JSON layer: units and thresholds
render_html.py         Jinja2 render + JSON layer
ai_layer.py            text versions, Markdown mirrors, llms.txt, sitemap
boxes/                 one module per dashboard box: render(con) -> {meta, data}
scripts/               fetchers, indicators, DB init, Google Drive sync, verify_live.py
sainhe_backend/        Stock Analysis and portfolio engine
  edgar/ esef/         filing clients and XBRL parsing (US-GAAP and IFRS)
  compute/             metrics, WACC and valuation, scores, market inputs
  portfolio/           returns, risk, HRP, Monte Carlo, stress
  secondary/           Yahoo statements (secondary source)
  pipeline.py          companies -> data/stock/*_report.json
  portfolio_runner.py  allocation lab, crises -> data/stock/lab_data.json
  quality_check.py     cross-source checks -> data/stock/quality_report.json
templates/             HTML, CSS and JavaScript (Stock Analysis in partials/stock/)
tests_front/           browser arithmetic tested under Node
data/stock/            versioned outputs: git history is the history of the analyses
```

## Run it locally

```bash
pip install -r requirements.txt -r sainhe_backend/requirements.txt
cp .env.example .env              # FRED key, and a contact email for the SEC User-Agent
python render_html.py             # build into output/
python serve.py                   # http://localhost:7723

cd sainhe_backend
python tests/test_pipeline.py && python tests/test_full.py
python pipeline.py --coverage && python portfolio_runner.py && python quality_check.py
```

No secret is committed: keys are read from the environment or a git-ignored `.env`.

## Limits, stated plainly

- 28 companies, not thousands. Depth over breadth.
- Ten companies have no usable free machine-readable filing: three Swiss issuers, plus one each from Japan, Saudi Arabia, China and Kazakhstan, and three European issuers absent from or incomplete in the ESEF index. Their accounts come from Yahoo Finance, shown in grey and never cross-checked as primary data.
- Prices and FX rely on Yahoo Finance.
- Informational only, not investment advice. The watchlist is the author's; he may or may not hold positions in it.

---

<sub>Built by Hugo Waldmeyer · <a href="https://sainhe.com/contact#ct-legal">Legal &amp; privacy</a> · <a href="https://sainhe.com/about.md">How it works, in one page</a></sub>
