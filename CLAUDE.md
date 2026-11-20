# SAINHE v3 — consignes pour Claude

Dashboard d'aide à la décision d'investissement (sainhe.com / sainhe.pages.dev).
Outil d'**analyse**, jamais de conseil : on mesure et on signale, on ne recommande pas.

## Règles non négociables

1. **Zéro LLM dans le pipeline.** Aucun appel d'API d'IA dans le code produit. Tout est déterministe.
2. **Zéro prédiction.** Pas de DCF prévisionnel, pas d'objectif de cours maison, pas de « buy/sell ».
   Seuls éléments tournés vers le futur autorisés : DCF inverse (croissance implicite), SGR,
   plage de confiance historique, consensus analystes (externe).
3. **Données gratuites uniquement** : SEC EDGAR direct, yfinance, FRED, ECB SDW, stooq, multpl, RSS.
   Pas d'API payante (pas de Financial Datasets MCP).
4. **Traçabilité** : chaque chiffre porte sa source (tag XBRL, date du filing, URL). Donnée absente →
   `"N/D"`, jamais d'interpolation ni d'imputation silencieuse.
5. **Aucune valeur magique** : tous les seuils (1.81, 2.99, −1.78, 15 %, 20 %, 50 %, 100 bps…) vivent dans `config.py`.
6. **EDGAR** : en-tête `User-Agent` obligatoire, 10 requêtes/s maximum. L'email de contact vient de
   `EDGAR_CONTACT_EMAIL` (`.env` local ou secret GitHub) — **jamais écrit dans le code** : le dépôt est public.
7. Disclaimer « Informational only, not investment advice » sur toute page d'analyse.

## Architecture

```
scripts/fetch_*.py ──► SQLite (sainhe.db, synchronisée sur Google Drive via scripts/gdrive_sync.py)
scripts/calc.py        indicateurs (DMA, MFI, OBV, momentum)
boxes/*.py             un module par bloc : render(con) -> {meta, data}
render_html.py         Jinja2 -> HTML statique + couche JSON (output/data/*.json)
schema.py              contrat de la couche JSON (unités + seuils)
templates/             HTML/CSS/JS (Chart.js) ; stock-analysis.html derrière le flag STOCK_ANALYSIS_LIVE
sainhe_backend/        Stock Analysis + Portfolio Sim (EDGAR, compute, portfolio, output, tests)
.github/workflows/sainhe.yml     pipeline -> Cloudflare Pages (preview dev : flag true ; prod : flag false)
.github/workflows/keepalive.yml  garde-fou quotidien (sur main) : réactive les workflows, alerte si données > 48 h
```

Branches : `main` = branche par défaut (les `schedule` GitHub ne tournent que depuis main, avec le
`sainhe.yml` de main, qui fait un checkout du code de `dev`) ; `dev` = branche de travail, un push déploie.
Dépôt GitHub : `MUGIWARA95D/sainhe-v4` (ex `SAINHE-v3`, l'ancienne URL redirige).
La vraie base `sainhe.db` est sur Google Drive (téléchargée par la CI) ; la copie locale peut être vide.

Un bloc = une seule fonction `render(con)` qui alimente à la fois le HTML et le JSON : ne jamais dupliquer un calcul.

## Commandes (Windows)

`python` n'est pas dans le PATH de cette machine (l'alias Microsoft Store intercepte la commande) :
utiliser le lanceur `py`, qui pointe sur Python 3.14. L'entrée 3.11 de `py` est cassée (exécutable supprimé).
La CI tourne en 3.11 : éviter la syntaxe propre à 3.12+.

```powershell
py -m pip install -r requirements.txt
py render_html.py      # build dans output/
py serve.py            # http://localhost:7723
```

Pour tout script qui affiche des caractères Unicode (✓, ≥, €) : `$env:PYTHONUTF8='1'`, sinon la console cp1252 plante.

## Chantier en cours : Stock Analysis + Portfolio Sim

- **Spécification (source de vérité)** : `G:\Mon Drive\PROJET SAINHE\sainhe reprise\SAINHE_stock_analysis_plan.txt`
  — en cas de conflit avec ce fichier ou un prompt, le plan gagne.
- **Backend intégré dans `sainhe_backend/`** (`edgar/`, `compute/`, `portfolio/`, `output/`, `tests/`).
  Tests : `py tests/test_pipeline.py` (62 assertions, sur fixtures), depuis `sainhe_backend/`.
  (`test_full.py`, 61 assertions Portfolio Sim, n'existe que dans le zip `sainhe reprise\sainhe_backend_v2.zip`.)
  Sous Windows : `$env:PYTHONUTF8='1'` obligatoire (sorties ✓/≥).
- **Page publique** : Stock Analysis est rendue en preview (dev) mais masquée en production (`STOCK_ANALYSIS_LIVE=false`).
- **Reste à faire** (README du backend) : brancher Rf/ERP sur le module Macro, parsing XML des Forms 4,
  validation sur tickers réels (Net Debt Apple = 10-K au million près), univers de pairs complet.
- Tickers de validation : AAPL, MSFT, KO, JPM, XOM.
- `ANT` (Anthropic, IPO 2026-11-19) : historique de prix très court → DMA200, momentum, beta 1Y/3Y/5Y
  restent `N/D` pendant des mois ; pas de 10-K avant 2027 → hors `EDGAR_TICKERS`.

## Tickers

Vérifier leur santé avant toute modification de `config.py` : un ticker sans cotation depuis 7 jours est mort.
Yahoo limite les requêtes : passer par `curl_cffi` (`Session(impersonate="chrome")`), comme `fetch_prices.py`.
`db_init.py` fait `INSERT OR IGNORE` : un ticker retiré de `config.py` reste dans `ticker_info`.

## Skills utiles pour ce projet

| Module | Skill |
|---|---|
| Monte Carlo, VaR/CVaR, stress tests | `forward-risk` |
| Volatilité réalisée, drawdown, VaR historique | `historical-risk` |
| EPV, RE, DCF inverse, WACC | `quantitative-valuation` |
| Décomposition du risque local vs FX (USD/EUR/HKD) | `currencies-and-fx` |
| Sharpe, Sortino, Calmar, alpha de Jensen, IR | `performance-metrics` |
| Attribution par titre / secteur / géographie | `performance-attribution` |
| Graphiques du dashboard | `dataviz` |
| Requêtes sur `sainhe.db` | `duckdb-skills` |

Ces skills donnent les définitions de référence ; en cas d'écart avec le plan SAINHE, **le plan gagne**.

## Conventions

- Commits au format Conventional Commits, portée entre parenthèses : `fix(portfolio,rotation): …`, `feat(boxes): …`.
- Ne jamais committer `.env` ni `sainhe.db`.
- Le dépôt est sur Google Drive : éviter les opérations git pendant une synchronisation Drive en cours.
