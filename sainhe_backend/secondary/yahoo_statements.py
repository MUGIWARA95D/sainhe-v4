"""Source SECONDAIRE — états financiers annuels Yahoo Finance (4 à 5 exercices).

Pour les sociétés sans XBRL accessible gratuitement :
  - Suisse (Roche, Nestlé, Geberit) : pas d'ESEF hors UE, pas de 20-F à la SEC ;
  - Japon (Fanuc) : EDINET exige une clé d'API ;
  - Arabie saoudite, Chine, Kazakhstan : pas de dépôt XBRL public exploitable ;
  - ESEF non indexé sur filings.xbrl.org (Safran, Rheinmetall) ;
  - ESEF périmé ou incomplet (pipeline.secondary_reason : Leonardo, Coats).
Chaque valeur garde la ligne Yahoo comme « tag » (yahoo:Total Revenue) et la page Yahoo comme
URL : la traçabilité s'arrête à l'agrégateur, pas au dépôt — d'où le badge « secondary source »
sur la page, et l'absence de recoupement qualité (la seconde source serait la même).
Aucune valeur n'est complétée : ligne absente ou NaN → la série n'a pas cette année (N/D).
"""
from __future__ import annotations
import logging
import math

from edgar.xbrl_parser import CompanyData, DataPoint

log = logging.getLogger("sainhe.secondary.yahoo")

SOURCE = "Yahoo Finance statements (secondary)"
FORM = "Yahoo"

# nom interne → (relevé, lignes Yahoo candidates par ordre de préférence, valeur absolue ?)
# Leases : exclus de la dette (Long Term Debt / Current Debt sans « Capital Lease ») pour rester
# cohérent avec la règle US GAAP du moteur (leases opérationnels hors dette).
YF_MAP: dict[str, tuple[str, list[str], bool]] = {
    "revenue":            ("income", ["Total Revenue", "Operating Revenue"], False),
    "cogs":               ("income", ["Cost Of Revenue", "Reconciled Cost Of Revenue"], False),
    "gross_profit":       ("income", ["Gross Profit"], False),
    "opex":               ("income", ["Operating Expense"], False),
    "sga":                ("income", ["Selling General And Administration"], False),
    "rnd":                ("income", ["Research And Development"], False),
    "ebit":               ("income", ["Operating Income", "Total Operating Income As Reported"], False),
    "interest_expense":   ("income", ["Interest Expense", "Interest Expense Non Operating"], True),
    "tax_expense":        ("income", ["Tax Provision"], False),
    "pretax_income":      ("income", ["Pretax Income"], False),
    "net_income":         ("income", ["Net Income Common Stockholders", "Net Income"], False),
    "eps_diluted":        ("income", ["Diluted EPS"], False),
    "shares_diluted":     ("income", ["Diluted Average Shares"], False),
    "cash":               ("balance", ["Cash And Cash Equivalents"], False),
    "st_investments":     ("balance", ["Other Short Term Investments"], False),
    "ar":                 ("balance", ["Accounts Receivable", "Receivables"], False),
    "inventory":          ("balance", ["Inventory"], False),
    "current_assets":     ("balance", ["Current Assets"], False),
    "total_assets":       ("balance", ["Total Assets"], False),
    "ppe_net":            ("balance", ["Net PPE"], False),
    "goodwill":           ("balance", ["Goodwill"], False),
    "intangibles":        ("balance", ["Other Intangible Assets"], False),
    "ap":                 ("balance", ["Accounts Payable", "Payables"], False),
    "current_liabilities": ("balance", ["Current Liabilities"], False),
    "lt_debt":            ("balance", ["Long Term Debt"], False),
    "st_debt":            ("balance", ["Current Debt"], False),
    "total_liabilities":  ("balance", ["Total Liabilities Net Minority Interest"], False),
    "equity":             ("balance", ["Stockholders Equity", "Common Stock Equity"], False),
    "retained_earnings":  ("balance", ["Retained Earnings"], False),
    "shares_outstanding": ("balance", ["Ordinary Shares Number", "Share Issued"], False),
    # méthode directe (Kazatomprom) : même total, autre intitulé Yahoo
    "ocf":                ("cashflow", ["Operating Cash Flow", "Cash Flowsfromusedin Operating Activities Direct"], False),
    "capex":              ("cashflow", ["Capital Expenditure", "Purchase Of PPE"], True),
    "sbc":                ("cashflow", ["Stock Based Compensation"], True),
    "buybacks":           ("cashflow", ["Repurchase Of Capital Stock", "Common Stock Payments"], True),
    "dividends_paid":     ("cashflow", ["Cash Dividends Paid"], True),
    "dep_amort":          ("cashflow", ["Depreciation And Amortization", "Reconciled Depreciation"], False),
}
_PER_SHARE = {"eps_diluted"}
_SHARES = {"shares_diluted", "shares_outstanding"}


def page_url(ticker: str) -> str:
    return f"https://finance.yahoo.com/quote/{ticker}/financials"


def company_from_frames(ticker: str, frames: dict, currency: str) -> CompanyData:
    """frames = {"income"|"balance"|"cashflow": DataFrame lignes × dates de clôture}."""
    cd = CompanyData(ticker=ticker, cik=0)
    for name, (stmt, rows, absolute) in YF_MAP.items():
        df = frames.get(stmt)
        if df is None or getattr(df, "empty", True):
            continue
        series: dict[int, DataPoint] = {}
        for row in rows:                       # première ligne disponible par exercice
            if row not in df.index:
                continue
            for col in df.columns:
                try:
                    v = float(df.loc[row, col])
                except (TypeError, ValueError):
                    continue
                if math.isnan(v):
                    continue
                end = str(col)[:10]
                year = int(end[:4])
                if year in series:
                    continue
                unit = "shares" if name in _SHARES else (f"{currency}/shares" if name in _PER_SHARE else currency)
                series[year] = DataPoint(value=abs(v) if absolute else v, fy=year, end=end,
                                         accn=f"YAHOO|{ticker}", form=FORM, tag=f"yahoo:{row}", unit=unit)
        if series:
            cd.series[name] = series
    return cd


def fetch(ticker: str, session=None) -> tuple[CompanyData, dict]:
    """(CompanyData, info yfinance). Lève ValueError si aucun chiffre d'affaires exploitable."""
    import yfinance as yf
    tk = yf.Ticker(ticker, session=session)
    info = tk.info or {}
    frames = {"income": tk.income_stmt, "balance": tk.balance_sheet, "cashflow": tk.cashflow}
    cur = info.get("financialCurrency") or info.get("currency") or "USD"
    cd = company_from_frames(ticker, frames, cur)
    if not cd.series.get("revenue"):
        raise ValueError(f"aucun chiffre d'affaires Yahoo exploitable pour {ticker}")
    log.info("%s : %d séries Yahoo, exercices %s, devise %s", ticker, len(cd.series),
             cd.years("revenue"), cur)
    return cd, info
