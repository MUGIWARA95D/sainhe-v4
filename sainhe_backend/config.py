"""SAINHE backend — configuration centrale.
Chaque seuil référence la section du plan (SAINHE_stock_analysis_plan.txt).
AUCUNE valeur magique ailleurs dans le code.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 CE QUE TU DOIS CONFIGURER AVANT D'UTILISER LES VRAIES DONNÉES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 1. EDGAR_CONTACT_EMAIL : ton email de contact (obligatoire — la SEC bloque sans ça).
    NE PAS l'écrire ici (dépôt public). Le mettre dans le fichier .env à la racine
    du dépôt (gitignoré) :  EDGAR_CONTACT_EMAIL=ton@email
    ou, pour la CI, dans le secret GitHub du même nom.

 2. Installer les dépendances :
    pip install pandas numpy scipy requests yfinance

 3. Lancer le backend (depuis le dossier sainhe_backend/) :
    python pipeline.py AAPL MSFT KO JPM XOM

 4. Pour générer les données de démo SANS internet :
    python run_demo.py

 Les JSON de sortie vont dans ../output/data/stock/
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Charge le .env de la racine du dépôt (même mécanisme que scripts/fetch_macro_liquidity.py)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# ---------------------------------------------------------------- EDGAR (.txt Couche 1)
# ⚠️  OBLIGATOIRE : la SEC exige un User-Agent avec email valide.
# L'adresse vient de l'environnement (.env local ou secret GitHub), jamais du code :
# le dépôt est public. Vide → edgar_client refuse tout appel réseau avec un message clair
# (les tests sur fixtures n'en ont pas besoin).
EDGAR_CONTACT_EMAIL = os.environ.get("EDGAR_CONTACT_EMAIL", "").strip()
EDGAR_USER_AGENT = f"SAINHE research {EDGAR_CONTACT_EMAIL}" if EDGAR_CONTACT_EMAIL else ""
EDGAR_RATE_LIMIT_PER_SEC = 10
EDGAR_BASE_SUBMISSIONS   = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
EDGAR_BASE_COMPANYFACTS  = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
EDGAR_TICKER_MAP_URL     = "https://www.sec.gov/files/company_tickers.json"

# Chemins — relatifs au dossier sainhe_backend/
CACHE_DB_PATH = os.path.join(os.path.dirname(__file__), "cache", "edgar_cache.sqlite")
# Source de vérité VERSIONNÉE (git = historique des analyses, plan §6 « versioning ») :
# data/stock/ à la racine du dépôt. render_html.py la copie dans output/data/stock/
# UNIQUEMENT pour la preview (STOCK_ANALYSIS_LIVE=true). Surcharge : SAINHE_STOCK_OUT.
OUTPUT_DIR    = os.environ.get("SAINHE_STOCK_OUT") or os.path.join(
    os.path.dirname(__file__), "..", "data", "stock")

# TTL du cache EDGAR (jours). Les fondamentaux ne bougent qu'au dépôt d'un 10-K (annuel)
# ou 10-Q (trimestriel) → inutile de re-fetcher la SEC plus de ~4×/an. 90 j couvre le
# cycle trimestriel tout en évitant le matraquage de data.sec.gov.
# En CI le cache est conservé d'une semaine à l'autre (actions/cache) : SAINHE_EDGAR_CACHE_DAYS
# (ex. 6) force un rafraîchissement hebdomadaire pour capter chaque nouveau 10-K/10-Q.
_EDGAR_TTL = os.environ.get("SAINHE_EDGAR_CACHE_DAYS")
EDGAR_CACHE_DAYS_COMPANYFACTS = float(_EDGAR_TTL) if _EDGAR_TTL else 90
EDGAR_CACHE_DAYS_SUBMISSIONS  = float(_EDGAR_TTL) if _EDGAR_TTL else 90
EDGAR_CACHE_DAYS_TICKER_MAP   = 30

# ESEF (XBRL européen, filings.xbrl.org) : la LISTE des rapports est relue chaque semaine
# (nouveau rapport annuel publié ?) ; chaque rapport, immuable, est caché sans expiration.
ESEF_CACHE_DAYS_FILINGS = 6
# Ticker SAINHE → LEI (GLEIF). Vérifié le 2026-11-20 sur filings.xbrl.org :
#   RMS.PA Hermès 2020-2025 ✓ · LDO.MI Leonardo 2021-2024 ✓ (tagging partiel) · COA.L Coats 2021-2025 ✓
# Non couverts : SAF.PA (aucun rapport indexé), RHM.DE (entité sans rapport : l'Allemagne
# publie via le Bundesanzeiger), ROP/NESN/GEBN (la Suisse n'impose pas l'ESEF).
ESEF_ENTITIES = {
    "RMS.PA": "969500Y4IJGHJE2MTJ13",
    "LDO.MI": "529900X4EEX1U9LN3U39",
    "COA.L":  "2138003SPYWGAITNFY17",
}

# Ratio ADR : 1 titre coté = N actions ordinaires (les comptes XBRL sont par action ordinaire).
# TSM : 1 ADR = 5 actions TSMC. ASML (NY registry shares) et RACE : 1 pour 1.
ADR_RATIO = {"TSM": 5.0}

# ════════════════════════════════════════════════════════════════
#  PORTFOLIO SAINHE — 31 tickers exacts du dashboard (box_06_portfolio)
# ════════════════════════════════════════════════════════════════
# EDGAR (SEC) ne couvre QUE les déposants US (10-K). Les ADR étrangers
# déposent en 20-F (parsing différent), les valeurs purement étrangères /
# ETF / crypto n'ont AUCUN companyfacts EDGAR → le pipeline les saute
# proprement (log ECHEC) et continue.
#
#   ✓ EDGAR_OK      : US filers, données complètes
#   ~ ADR (20-F)    : déposent à la SEC mais format 20-F (couverture partielle)
#   ✗ NO_EDGAR      : étranger pur / ETF / crypto → pas de fondamentaux SEC

PORTFOLIO_TICKERS = [
    # ✓ US filers (10-K) — Stock Analysis complet
    "NVDA", "AAPL", "MSFT", "ISRG", "ZTS", "XOM", "LMT", "TSLA",
    "GEV", "DE", "CTVA", "ECL", "KO", "CAT",
    # US filer récent : IPO 2026-11-19, seulement un S-1 (pas encore de 10-K) →
    # hors EDGAR_TICKERS tant que le premier 10-K n'est pas déposé
    "ANT",
    # ~ ADR déposant 20-F à la SEC (couverture partielle)
    "ASML", "TSM", "RACE",
    # ✗ Pas de données EDGAR (étranger pur / ETF / crypto) — sautés par le pipeline
    "6954.T", "ROP.SW", "2222.SR", "SAF.PA", "RHM.DE", "LDO.MI",
    "RMS.PA", "GEBN.SW", "NESN.SW", "COA.L", "600111.SS",
    "GLD", "KAP.L", "BTC-USD",
]

# Sous-ensemble exploitable par EDGAR (US 10-K) — sert de défaut sûr au pipeline
EDGAR_TICKERS = [
    "NVDA", "AAPL", "MSFT", "ISRG", "ZTS", "XOM", "LMT", "TSLA",
    "GEV", "DE", "CTVA", "ECL", "KO", "CAT",
]

# Déposants 20-F (IFRS) — couverture partielle via ifrs-full XBRL
ADR_TICKERS = ["ASML", "TSM", "RACE"]

# Couverture complète du job CI hebdomadaire : US 10-K + 20-F + ESEF
COVERAGE_TICKERS = EDGAR_TICKERS + ADR_TICKERS + list(ESEF_ENTITIES)

# Garde-fous du plan §7 (publié dans chaque JSON et sur la page)
DISCLAIMER = ("Informational only, not investment advice. SAINHE publishes measurements and flags "
              "computed from public filings; it does not recommend buying or selling any security.")
# Comptes annuels plus vieux que ça → stale_financials = true (10-K/ESEF uniquement, pas de TTM)
STALE_FINANCIALS_MONTHS = 15

# Tickers de validation Phase 1 (.txt §8 / prompt)
TEST_TICKERS = ["AAPL", "MSFT", "KO", "JPM", "XOM"]

# ---------------------------------------------------------------- Seuils flags (.txt Couche 2/3)
GOODWILL_RED_FLAG_PCT_ASSETS = 0.30
GOODWILL_COMPOSITE_PCT_ASSETS = 0.50
ROIC_DECLINE_YEARS = 3
SBC_FCF_FLAG = 0.15
LEASE_HEAVY_PCT_LIABILITIES = 0.20
ADJUSTED_NI_GAP_FLAG = 0.10
REVERSE_DCF_IMPLIED_G_FLAG = 0.15
ANALYST_MIN_OPINIONS = 5
DISPERSION_HIGH_RATIO = 1.5
DISPERSION_LOW_RATIO = 0.8

# Altman Z
ALTMAN_DISTRESS = 1.81
ALTMAN_SAFE = 2.99
# Beneish M
BENEISH_MANIPULATION = -1.78
# Hypercroissance : le modèle (calibré sur des entreprises matures) sur-réagit à SGI
# (croissance des ventes). Au-delà, le flag "manipulation" est NON CONCLUANT (ex. NVDA).
BENEISH_HYPERGROWTH_SGI = 1.5

# Buffett
LT_DEBT_PAYBACK_MAX_YEARS = 4
ROE_AVG_MIN = 0.20
ROE_FLOOR_MIN = 0.15
RETAINED_EARNINGS_TEST_WINDOW = 5
RETAINED_EARNINGS_TEST_MIN = 1.0

# ---------------------------------------------------------------- Valorisation
WACC_SENSITIVITY_BPS = 0.01
FCF_CONFIDENCE_PCTL = (25, 75)
FCF_MIN_YEARS_FOR_PCTL = 4
RE_HORIZON_YEARS = 5
TERMINAL_G_KE_GAP = 0.02     # croissance perpétuelle ≤ min(Rf, ke − 2 pts) (voir valuation.terminal_g_cap)
SENSITIVITY_NOPAT_PCTL = (10, 25, 50, 75, 90)
SENSITIVITY_G_FACTORS = (0.5, 0.75, 1.0, 1.25, 1.5)

# Macro : Rf live = US 10Y du module Macro SAINHE (compute/market_inputs.py) ;
# DEFAULT_RISK_FREE = repli si le site est injoignable.
MACRO_JSON_URL = "https://sainhe.pages.dev/data/macro.json"
DEFAULT_RISK_FREE = 0.042
DEFAULT_ERP = 0.050
DEFAULT_BETA = 1.0
BLUME_WEIGHT = 0.67          # beta ajusté = 0,67·β_brut + 0,33

# Opérations sur titres postérieures au dernier 10-K (splits / spin-offs, yfinance) :
# ratio ≈ entier → actions ajustées ; sinon (spin-off) → valorisation N/D + flag.
SPLIT_INTEGER_TOLERANCE = 0.01

# CIK de repli quand la table SEC pointe vers une nouvelle entité sans historique 10-K
# (ex. XOM → "ExxonMobil Holdings Corp" CIK 2115436, historique sous Exxon Mobil Corp 34088).
CIK_FALLBACK = {"XOM": 34088}

# ---------------------------------------------------------------- Score composite
SCORE_WEIGHTS = {
    "valuation":          0.30,
    "balance_sheet_health": 0.25,
    "earnings_quality":   0.20,
    "smart_money":        0.15,
    "momentum":           0.10,
}

# Peers
PEER_MARKET_CAP_BUCKETS = [(0, 2e9), (2e9, 10e9), (10e9, 200e9), (200e9, float("inf"))]
