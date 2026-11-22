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
  partials/stock/      page Stock Analysis phase 1 (_sa_style, _sa_sections, _sa_js) — preview seulement
sainhe_backend/        Stock Analysis + Portfolio Sim (edgar/, esef/, compute/, portfolio/, output/, tests/)
  pipeline.py          --coverage : US 10-K + ADR 20-F (EDGAR) + Europe ESEF (filings.xbrl.org) → data/stock/
  portfolio_runner.py  Portfolio Sim USD/EUR/CHF/HKD → data/stock/portfolio_report_{DEV}.json
  quality_check.py     recoupement XBRL vs yfinance, régressions N/D, fraîcheur → data/stock/quality_report.json
data/stock/            JSON versionnés (git = historique des analyses) ; publiés UNIQUEMENT en preview
.github/workflows/sainhe.yml          pipeline -> Cloudflare Pages (preview dev : flag true ; prod : flag false)
.github/workflows/stock-analysis.yml  hebdo (mardi 05:30 UTC, sur main) : tests → backend → qualité → commit dev → redéploiement → issue si anomalies
.github/workflows/keepalive.yml       garde-fou quotidien (sur main) : réactive les workflows, alerte si données > 48 h
```

`render_html.py` copie `data/stock` dans `output/data/stock` si `STOCK_ANALYSIS_LIVE=true` et **supprime**
ce dossier sinon : les jobs CI rendent la preview puis la prod dans le même `output/` (sans ce nettoyage,
les JSON de la preview partaient sur sainhe.com — fuite du 14 juin).

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
- **Backend dans `sainhe_backend/`**. Tests hermétiques (sans réseau, `SAINHE_RF_LIVE=0`) depuis `sainhe_backend/` :
  `py tests/test_pipeline.py` (104) et `py tests/test_full.py` (61, Portfolio Sim). `$env:PYTHONUTF8='1'` sous Windows.
  Lancer : `py pipeline.py --coverage`, `py portfolio_runner.py`, `py quality_check.py` (dans cet ordre).
  Dépendances : `sainhe_backend/requirements.txt` (scipy inclus). Email SEC : `EDGAR_CONTACT_EMAIL` (`.env` / secret).
- **Couverture (21 nov.) : 29 sociétés.** Primaire : 14 US (10-K) + ASML/TSM/RACE (20-F) + Hermès (ESEF).
  Secondaire Yahoo (`secondary/yahoo_statements.py`, `config.YAHOO_STATEMENTS_TICKERS`) : ROP/NESN/GEBN.SW
  (Suisse, pas d'ESEF), 6954.T (EDINET : clé API), 2222.SR, 600111.SS, KAP.IL, SAF.PA et RHM.DE (ESEF non indexé
  sur filings.xbrl.org) ; LDO.MI et COA.L basculent automatiquement (ESEF incomplet : `pipeline.secondary_reason`).
  Secondaire = valeurs de l'agrégateur, gris sur la carte, pas de recoupement qualité. Hors périmètre : GLD, BTC, ANT.
  companyfacts SEC en retard sur un dépôt (TSM 20-F 2025) → instance XBRL du dépôt lue (`edgar/instance.py`).
  Rf sans champ macro.json : FRED OCDE (CHF) ; devise ancrée au dollar (SAR) → US 10Y signalé.
  Impôt effectif hors [0 %, 60 %] → médiane des exercices normaux, sinon N/D (`metrics.effective_tax`).
  Éléments XBRL propres à une société (jamais dans companyfacts) : `config.CUSTOM_XBRL_TAGS`, lus dans les
  instances des 4 derniers rapports (Deere : dette LT). DCF inverse : FCF du dernier exercice ≤ 0 → médiane
  5 ans si positive (flag `fcf_basis_median`). Les tests n'écrivent plus dans un fichier suivi (tests/out/).
- **Devises** : prix ramené à la devise des COMPTES et à l'action ordinaire (`pipeline._pricing_context` :
  ASML/RACE EUR, TSM TWD + ADR ×5, COA.L pence→USD) ; devise dominante détectée par société (TSM : TWD
  prime sur la traduction de convenance USD). Parités fixes (SAR 3,75) plutôt que cotations Yahoo bruitées.
- **WACC par devise (21 nov.)** : Rf = 10 ans souverain de la devise des COMPTES (`config.RF_FIELD_BY_CURRENCY`,
  champs de macro.json) ; TWD sans source gratuite → US 10Y + flag `rf_currency_proxy`.
- **Dette nette** : dette + leases financiers − cash − placements CT (`metrics.net_debt_detail`) ; placements
  CT non publiés cette année → non déduits et signalés (asymétrie volontaire, sens prudent).
- **Valeurs n/m** : SGR/ROIC non significatifs (réinvestissement < 0 ou > 1, capital investi < 10 % de l'actif)
  → statut `n/m` + raison + flag ; seuils dans `sainhe_backend/config.py`, publiés dans `valuation_map.thresholds`.
- **Règles N/D du moteur** : composante de dette publiée d'autres années mais pas cette année → N/D (pas 0) ;
  Kd N/D → WACC all-equity ; impôt N/D → EPV N/D ; croissance perpétuelle ≤ min(Rf, ke − 2 pts) ;
  opération sur titres non entière après le dernier exercice (spin-off) → valorisation N/D + flag.
- **Fraîcheur** : `meta.stale_financials` si comptes > 15 mois (pas de TTM 10-Q à ce stade).
- **Portfolio Sim** : portefeuille actuel rejoué sans reconstitution (titres non encore cotés exclus, poids
  renormalisés, couverture publiée) ; stress tests 2008→2022 sur l'historique depuis 2007. Poids : équipondéré,
  ou `data/portfolio_weights.json` {"TICKER": poids} s'il existe.
- **Page publique (phase 1, 20 nov.)** : `templates/stock-analysis.html` inclut `partials/stock/_sa_*.html`
  UNIQUEMENT si `STOCK_ANALYSIS_LIVE=true` et valuation map présente (sinon « Coming soon »). 5 modules :
  carte Qualité × Prix (croissance implicite × années ROIC > WACC), simulateur « ce qu'il faut croire »
  (DCF inverse sur les curseurs de l'utilisateur, jamais une estimation SAINHE), tiroir des sources (formule,
  entrées, tag XBRL, lien du dépôt), Portfolio Lab (poids, ⇄ concurrent, objectif de devise synchronisé avec
  l'en-tête via l'événement `sainhe:currency`, lien partageable `#lab=`), rejeu des 5 crises. Tout est
  calculé dans le navigateur depuis `data/stock/*_report.json` et `lab_data.json` (écrit par
  `portfolio_runner.export_lab_data` : rendements journaliers en 1e-5, change « USD pour 1 unité »).
  Parité vérifiée avec le backend (vol 5 ans, max drawdown). Palette des graphiques validée (dataviz).
  Le site est en anglais : les libellés `COMPETITORS` (config.py racine) le sont aussi.
  L'ancienne interface du 14 juin (46a4550, b355862) n'a PAS été reprise.
- **Reste à faire** : parsing XML des Forms 4, univers de pairs SIC, TTM via 10-Q, matrice de sensibilité
  `bloc_H` dégénérée (axe g écrasé par le plafond de croissance terminale), statut FINMA avant mise en ligne.
- `ANT` (Anthropic, IPO 2026-11-19) : historique de prix très court → exclu de Portfolio Sim (< 400 jours) ;
  pas de 10-K avant 2027 → hors couverture Stock Analysis.

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
| Décomposition du risque local vs FX (USD/EUR/CHF/HKD) | `currencies-and-fx` |
| Sharpe, Sortino, Calmar, alpha de Jensen, IR | `performance-metrics` |
| Attribution par titre / secteur / géographie | `performance-attribution` |
| Graphiques du dashboard | `dataviz` |
| Requêtes sur `sainhe.db` | `duckdb-skills` |

Ces skills donnent les définitions de référence ; en cas d'écart avec le plan SAINHE, **le plan gagne**.

## Conventions

- Commits au format Conventional Commits, portée entre parenthèses : `fix(portfolio,rotation): …`, `feat(boxes): …`.
- Ne jamais committer `.env` ni `sainhe.db`.
- Le dépôt est sur Google Drive : éviter les opérations git pendant une synchronisation Drive en cours.
