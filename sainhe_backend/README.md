# sainhe_backend — Stock Analysis and portfolio engine

The engine behind [sainhe.com/stock-analysis](https://sainhe.com/stock-analysis). Overview, architecture and rules: [../README.md](../README.md).

## Run

```bash
pip install -r requirements.txt
# the SEC requires a contact email in the User-Agent: set EDGAR_CONTACT_EMAIL in ../.env (never in code)
python pipeline.py --coverage     # every covered company -> ../data/stock/{TICKER}_report.json + valuation_map.json
python portfolio_runner.py        # allocation lab, Monte Carlo inputs, crises -> ../data/stock/lab_data.json
python quality_check.py           # cross-source checks, N/D regressions, freshness -> quality_report.json
python pipeline.py AAPL MSFT      # a few companies only
```

## Tests (no network)

```bash
SAINHE_RF_LIVE=0 python tests/test_pipeline.py   # valuation engine on recorded filings
SAINHE_RF_LIVE=0 python tests/test_full.py       # portfolio engine
```

Both run in CI on every change and before every data run.

## Map

| Folder | Role |
|---|---|
| `edgar/` | SEC client (cached, rate-limited, identified User-Agent), XBRL company facts parser, filing instances, TTM from 10-Q |
| `esef/` | filings.xbrl.org client (facts parsed by `edgar/xbrl_parser.py`, IFRS map) |
| `secondary/` | Yahoo statements for companies without a machine-readable filing; one checked field fill |
| `compute/` | metrics (NOPAT, ROIC, net debt, tax), valuation (WACC, reverse DCF, EPV, residual earnings, SGR), scores, DuPont, market inputs (risk-free, ERP) |
| `portfolio/` | returns, risk, HRP, Monte Carlo, stress scenarios |
| `output/` | report builder (flags included), valuation map, traceability of every input |
| `coherence.py` | accounting identities checked on every report |
| `config.py` | every threshold, currency rule and company-specific exception, each with its reason |
