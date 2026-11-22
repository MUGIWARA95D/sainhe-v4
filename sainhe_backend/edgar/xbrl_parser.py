"""Couche 2 — Parsing structuré XBRL (.txt §3 Couche 2). Zéro LLM.

Extrait les tags US-GAAP (10-K) et IFRS (20-F) depuis companyfacts EDGAR, en séries
ANNUELLES, chaque point portant : valeur, fiscal year, end date, accession (filing source).
Tag absent → absent du dict (propagé "N/D" en aval, jamais d'imputation).
"""
from __future__ import annotations
from dataclasses import dataclass, field

# Mapping nom interne -> liste de tags US-GAAP candidats (ordre de préférence).
# Les entreprises varient dans leurs tags ; on prend le premier disponible.
TAG_MAP: dict[str, list[str]] = {
    # ---------------- INCOME STATEMENT
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet"],
    "cogs": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "opex": ["OperatingExpenses"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    "ebit": ["OperatingIncomeLoss"],
    "dep_amort": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
                  "DepreciationAmortizationAndAccretionNet"],
    # Composants (MSFT, ISRG, ECL 2013+, GEV, TSLA ne publient pas le total) → _compose_da()
    "_depreciation": ["Depreciation"],
    "_amort_intangibles": ["AmortizationOfIntangibleAssets"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt",
                         "InterestExpenseNonoperating", "InterestAndDebtExpense"],
    "tax_expense": ["IncomeTaxExpenseBenefit"],
    "income_taxes_paid": ["IncomeTaxesPaidNet", "IncomeTaxesPaid"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                      "ExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                      "MinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "net_income": ["NetIncomeLoss", "ProfitLoss",
                   "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "shares_outstanding": ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"],
    # ---------------- BALANCE SHEET
    # Fallback : certains déposants (ZTS, GEV) ne publient plus que le total incluant le
    # cash restreint (écart marginal, tag source tracé dans DataPoint.tag).
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "st_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                       "OtherShortTermInvestments"],   # KO depuis 2022
    "ar": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],   # LMT, GEV : 2e tag
    "inventory": ["InventoryNet"],
    "current_assets": ["AssetsCurrent"],
    "total_assets": ["Assets"],
    "ppe_net": ["PropertyPlantAndEquipmentNet"],
    # ASC 842 (.txt NOTE ASC 842 — critique retail/airlines/restaurants)
    "rou_asset": ["OperatingLeaseRightOfUseAsset"],
    "operating_lease_liab": ["OperatingLeaseLiability"],
    "finance_lease_liab": ["FinanceLeaseLiability"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill",
                    "FiniteLivedIntangibleAssetsNet"],
    "ap": ["AccountsPayableCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    # st_debt : COMPOSÉ dans _compose_debt() (une seule valeur par tag ne suffit pas :
    # Apple = CommercialPaper + LongTermDebtCurrent ; KO = CP + OtherShortTerm + courant LT).
    "_debt_current": ["DebtCurrent"],
    "_st_borrowings": ["ShortTermBorrowings"],
    "_commercial_paper": ["CommercialPaper"],
    "_other_st_borrowings": ["OtherShortTermBorrowings"],
    "_ltd_current": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    # LongTermDebt (en dernier) = total y compris la part courante chez la plupart des
    # déposants → corrigé dans _compose_debt(). Migration de tags : KO/ECL/CTVA 2024+.
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebt"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    # Capitaux propres totaux (avec minoritaires) : sert à dériver le passif total s'il manque
    "_equity_total": ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "minority_interest": ["MinorityInterest"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    # ---------------- CASH FLOW
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "sbc": ["ShareBasedCompensation"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "debt_issued": ["ProceedsFromIssuanceOfLongTermDebt"],
    "debt_repaid": ["RepaymentsOfLongTermDebt"],
    # ---------------- ITEMS TRANSITOIRES (.txt AJUSTEMENT DES EARNINGS)
    "goodwill_impairment": ["GoodwillImpairmentLoss", "GoodwillAndIntangibleAssetImpairment"],
    "restructuring": ["RestructuringCharges", "RestructuringCostsAndAssetImpairmentCharges"],
    "asset_disposal_gain": ["GainLossOnDispositionOfAssets",
                            "GainLossOnSaleOfPropertyPlantEquipment"],
    "acquisition_costs": ["BusinessCombinationAcquisitionRelatedCosts"],
    # ---------------- FLAGS XBRL natifs
    "going_concern": ["SubstantialDoubtAboutGoingConcernExistence"],
    # ---------------- SEGMENT GEO (.txt Bloc I point 70 — best-effort, souvent dimensionnel)
    "us_revenue": ["RevenueFromContractWithCustomerExcludingAssessedTaxUnitedStates",
                   "RevenuesUnitedStates"],
    "foreign_revenue": ["RevenuesForeign", "ForeignCountryMember"],
}

# Mapping IFRS (ifrs-full namespace) pour les déposants 20-F (ASML, TSM, RACE, etc.).
# Même noms internes que TAG_MAP → le résultat est fusionné dans le même CompanyData.
IFRS_TAG_MAP: dict[str, list[str]] = {
    # ---------------- INCOME STATEMENT
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "cogs": ["CostOfSales"],
    "gross_profit": ["GrossProfit"],
    "ebit": ["ProfitLossFromOperatingActivities", "OperatingProfit"],
    "dep_amort": ["DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLoss"
                  "RecognisedInProfitOrLoss",
                  "DepreciationAndAmortisationExpense",
                  "AdjustmentsForDepreciationAndAmortisationExpense"],
    # composants sans total publié (TSMC) → additionnés par _compose_da, tags tracés
    "_depreciation": ["DepreciationExpense", "DepreciationPropertyPlantAndEquipment"],
    "_amort_intangibles": ["AmortisationExpense", "AmortisationIntangibleAssetsOtherThanGoodwill"],
    "income_taxes_paid": ["IncomeTaxesPaidRefundClassifiedAsOperatingActivities",
                          "IncomeTaxesPaidClassifiedAsOperatingActivities"],
    "interest_expense": ["FinanceCosts", "InterestExpense"],
    "tax_expense": ["IncomeTaxExpenseContinuingOperations", "IncomeTaxExpense"],
    "pretax_income": ["ProfitLossBeforeTax"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "eps_diluted": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"],
    "shares_diluted": ["WeightedAverageDilutedNumberOfOrdinarySharesOutstanding",
                       "WeightedAverageNumberOfSharesOutstandingBasic"],
    "shares_outstanding": ["NumberOfSharesOutstanding", "IssuedCapitalOrdinaryShares"],
    # ---------------- BALANCE SHEET
    "cash": ["CashAndCashEquivalents", "CashAndCashEquivalentsClassifiedAsPartOfDisposalGroup"],
    "ar": ["TradeAndOtherCurrentReceivables", "TradeAndOtherReceivables"],
    "inventory": ["Inventories"],
    "current_assets": ["CurrentAssets"],
    "total_assets": ["Assets"],
    "ppe_net": ["PropertyPlantAndEquipment"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsOtherThanGoodwill"],
    "ap": ["TradeAndOtherCurrentPayables", "TradeAndOtherPayablesToTradeSuppliers"],
    "current_liabilities": ["CurrentLiabilities"],
    # Dette financière IFRS. Les dettes de loyers IFRS 16 (Current/NoncurrentLeaseLiabilities)
    # restent HORS dette nette : IFRS 16 ne distingue pas location simple / financement,
    # et le plan classe les loyers opérationnels en obligations opérationnelles (ASC 842).
    "_debt_current": ["CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"],
    "_st_borrowings": ["ShorttermBorrowings"],
    "_ltd_current": ["CurrentPortionOfLongtermBorrowings"],
    "lt_debt": ["NoncurrentPortionOfNoncurrentBorrowings", "NoncurrentPortionOfLongtermBorrowings",
                "LongtermBorrowings", "NoncurrentBorrowings"],
    "total_liabilities": ["Liabilities"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    "_equity_total": ["Equity"],
    "minority_interest": ["NoncontrollingInterests"],
    "retained_earnings": ["RetainedEarnings"],
    # ---------------- CASH FLOW
    "ocf": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
              "AcquisitionOfPropertyPlantAndEquipment",
              # ESEF (Hermès…) : capex corporels + incorporels sur une seule ligne
              "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwill"
              "InvestmentPropertyAndOtherNoncurrentAssets"],
    "sbc": ["ExpenseFromShareBasedPaymentTransactionsWithEmployees", "ShareBasedCompensation",
            "AdjustmentsForSharebasedPayments"],
    "buybacks": ["RepurchaseOfTreasuryShares", "PaymentsForRepurchaseOfOrdinaryShares"],
    "dividends_paid": ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid",
                       "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities"],
    # ---------------- ITEMS TRANSITOIRES
    "goodwill_impairment": ["ImpairmentLossRecognisedInProfitOrLossGoodwill"],
    "restructuring": ["RestructuringCosts"],
}


@dataclass
class DataPoint:
    value: float
    fy: int                # fiscal year
    end: str               # date de fin de période
    accn: str              # accession number = filing source (traçabilité .txt Couche 4)
    form: str = "10-K"
    tag: str = ""          # tag XBRL source (ou "a+b" / "derived:..." si composé)
    unit: str = ""         # unité XBRL (USD, EUR, TWD, EUR/shares, shares…)


@dataclass
class CompanyData:
    ticker: str
    cik: int
    series: dict[str, dict[int, DataPoint]] = field(default_factory=dict)

    def latest(self, name: str) -> DataPoint | None:
        s = self.series.get(name)
        return s[max(s)] if s else None

    def value(self, name: str, fy: int | None = None):
        """Valeur ou None ('N/D' en aval). fy=None → dernière année."""
        s = self.series.get(name)
        if not s:
            return None
        if fy is None:
            fy = max(s)
        return s[fy].value if fy in s else None

    def years(self, name: str) -> list[int]:
        return sorted(self.series.get(name, {}))

    def history(self, name: str, n: int = 10) -> dict[int, float]:
        s = self.series.get(name, {})
        return {y: s[y].value for y in sorted(s)[-n:]}

    def reporting_currency(self) -> str | None:
        """Devise des comptes (unité du chiffre d'affaires, à défaut du résultat net).
        Ex. ASML/RACE → EUR, TSM → TWD. Nécessaire pour comparer les valeurs
        par action au prix de cotation (qui peut être dans une autre devise)."""
        for name in ("revenue", "net_income", "total_assets"):
            dp = self.latest(name)
            if dp and dp.unit:
                return dp.unit.split("/")[0]
        return None


def _dominant_currency(namespaces: list[dict], forms: frozenset[str]) -> str:
    """Devise des comptes de la société. Les 20-F/ESEF peuvent publier à la fois la devise
    fonctionnelle (TSM : TWD) et une traduction de convenance en USD (année récente) :
    la devise fonctionnelle (non-USD) prime alors, sinon on mélangerait les devises."""
    counts: dict[str, int] = {}
    for ns in namespaces:
        for tag in ("Revenues", "Revenue", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "RevenueFromContractsWithCustomers", "Assets", "NetIncomeLoss", "ProfitLoss"):
            for u, items in ns.get(tag, {}).get("units", {}).items():
                if len(u) == 3 and u.isalpha() and u.isupper():
                    counts[u] = counts.get(u, 0) + sum(1 for x in items if x.get("form") in forms)
    counts = {u: n for u, n in counts.items() if n}
    if not counts:
        return "USD"
    non_usd = {u: n for u, n in counts.items() if u != "USD"}
    if non_usd and not forms <= {"10-K", "10-Q"}:
        return max(non_usd, key=non_usd.get)
    return max(counts, key=counts.get)


def _extract_series(node: dict, forms: frozenset[str], tag: str = "",
                    currency: str = "USD") -> dict[int, "DataPoint"]:
    """Extrait une série annuelle depuis un node companyfacts {units:{...:[...]}}"""
    units = node.get("units", {})
    unit_key = next((u for u in (currency, f"{currency}/shares", "shares", "pure") if u in units),
                    next(iter(units), None))
    if not unit_key:
        return {}
    per_tag: dict[int, DataPoint] = {}
    for item in units[unit_key]:
        if item.get("form") not in forms:
            continue
        val, end = item.get("val"), item.get("end")
        if val is None or not end:
            continue
        start = item.get("start")
        if start:  # flux : ne garder que les périodes ~annuelles
            try:
                months = (int(end[:4]) * 12 + int(end[5:7])) - \
                         (int(start[:4]) * 12 + int(start[5:7]))
            except (ValueError, IndexError):
                continue
            if not (11 <= months <= 13):
                continue
        try:
            period_year = int(end[:4])
        except ValueError:
            continue
        accn = item.get("accn", "")
        dp = DataPoint(value=float(val), fy=period_year, end=end,
                       accn=accn, form=item.get("form", ""), tag=tag, unit=unit_key)
        cur = per_tag.get(period_year)
        if cur is None or accn >= cur.accn:
            per_tag[period_year] = dp
    return per_tag


def parse_companyfacts(
    ticker: str,
    cik: int,
    facts_json: dict,
    forms: tuple[str, ...] = ("10-K",),
) -> "CompanyData":
    """Construit les séries annuelles depuis le JSON companyfacts EDGAR.

    Règles :
      - Indexée par l'ANNÉE DE LA PÉRIODE (end[:4]), jamais par item["fy"].
      - Flux (start→end) : durées ~annuelles (11-13 mois) seulement.
      - Restatement : à période égale, accn max gagne.
      - Tag migration : tags candidats MERGENT, pas break au premier.
      - forms=("20-F",) : cherche aussi dans ifrs-full (IFRS_TAG_MAP).
    """
    cd = CompanyData(ticker=ticker, cik=cik)
    facts = facts_json.get("facts", {})
    gaap = facts.get("us-gaap", {})
    dei = facts.get("dei", {})
    ifrs = facts.get("ifrs-full", {})

    allowed = frozenset(forms)
    currency = _dominant_currency([gaap, ifrs], allowed)

    for internal_name, candidates in TAG_MAP.items():
        merged: dict[int, DataPoint] = {}
        for tag in candidates:
            node = gaap.get(tag) or dei.get(tag)
            if not node:
                continue
            for y, dp in _extract_series(node, allowed, tag, currency).items():
                merged.setdefault(y, dp)
        # Déposants IFRS (20-F à la SEC, ESEF en Europe) : compléter avec ifrs-full
        if ifrs and allowed & {"20-F", "ESEF"}:
            for tag in IFRS_TAG_MAP.get(internal_name, []):
                node = ifrs.get(tag)
                if not node:
                    continue
                for y, dp in _extract_series(node, allowed, tag, currency).items():
                    merged.setdefault(y, dp)
        if merged:
            cd.series[internal_name] = merged
    _compose_debt(cd)
    _compose_da(cd)
    _derive_ebit(cd)
    _derive_shares_and_liabilities(cd)
    return cd


def _derive_shares_and_liabilities(cd: "CompanyData") -> None:
    """Pour les années où ils ne sont pas publiés (fréquent en ESEF) :
    - actions diluées = résultat net part du groupe / BPA dilué (deux valeurs publiées) ;
      ex. Hermès 2025 : 4,524 G€ / 43,07 € = 105,0 M actions ;
    - passif total = actif total − capitaux propres totaux (y compris minoritaires).
    Calculs à partir de valeurs publiées (tag 'derived:' tracé), pas d'interpolation."""
    s = cd.series
    ni, eps = s.get("net_income", {}), s.get("eps_diluted", {})
    sh = s.setdefault("shares_diluted", {})
    for y, e in eps.items():
        if y not in sh and y in ni and e.value:
            n = ni[y]
            sh[y] = DataPoint(value=n.value / e.value, fy=y, end=n.end, accn=n.accn, form=n.form,
                              tag="derived:net_income/eps_diluted", unit="shares")
    if not sh:
        s.pop("shares_diluted")
    ta, eq_tot = s.get("total_assets", {}), s.pop("_equity_total", {})
    tl = s.setdefault("total_liabilities", {})
    for y, a in ta.items():
        if y not in tl and y in eq_tot:
            tl[y] = DataPoint(value=a.value - eq_tot[y].value, fy=y, end=a.end, accn=a.accn,
                              form=a.form, tag="derived:assets-equity_total", unit=a.unit)
    if not tl:
        s.pop("total_liabilities")


def _compose_da(cd: "CompanyData") -> None:
    """D&A d'une année sans tag total publié = Depreciation + AmortizationOfIntangibleAssets
    (deux tags publiés, additionnés ; tag tracé). Le total publié prime toujours."""
    dep = cd.series.pop("_depreciation", {})
    amo = cd.series.pop("_amort_intangibles", {})
    da = cd.series.setdefault("dep_amort", {})
    for y, d in dep.items():
        if y in da:
            continue
        a = amo.get(y)
        da[y] = DataPoint(value=d.value + (a.value if a else 0.0), fy=y, end=d.end, accn=d.accn,
                          form=d.form, tag=d.tag + (f"+{a.tag}" if a else ""))
    if not da:
        cd.series.pop("dep_amort")


def _compose_debt(cd: "CompanyData") -> None:
    """Dette courante et LT par année, à partir de leurs composants (traçable via .tag).

    st_debt = DebtCurrent si publié (total) ; sinon
              (ShortTermBorrowings, à défaut CommercialPaper + OtherShortTermBorrowings)
              + part courante de la dette LT.
    lt_debt : si le tag retenu est LongTermDebt (total incluant la part courante) et que
              la part courante est publiée la même année → lt = LongTermDebt − part courante
              (évite le double comptage).
    Avant ce correctif : un seul tag par année → la dette d'Apple ignorait ses 2,0 G$ de
    commercial paper (dette nette 43,265 G$ au lieu de 45,262 G$).
    """
    s = cd.series
    comp = {k: s.pop(k, {}) for k in ("_debt_current", "_st_borrowings", "_commercial_paper",
                                       "_other_st_borrowings", "_ltd_current")}
    years = set().union(*[set(v) for v in comp.values()])
    st: dict[int, DataPoint] = {}
    for y in sorted(years):
        if y in comp["_debt_current"]:
            st[y] = comp["_debt_current"][y]
            continue
        parts = []
        if y in comp["_st_borrowings"]:
            parts.append(comp["_st_borrowings"][y])
        else:
            parts += [comp[k][y] for k in ("_commercial_paper", "_other_st_borrowings") if y in comp[k]]
        if y in comp["_ltd_current"]:
            parts.append(comp["_ltd_current"][y])
        if parts:
            ref = parts[0]
            st[y] = DataPoint(value=sum(p.value for p in parts), fy=y, end=ref.end, accn=ref.accn,
                              form=ref.form, tag="+".join(p.tag for p in parts))
    if st:
        s["st_debt"] = st
    for y, dp in list(s.get("lt_debt", {}).items()):
        cur = comp["_ltd_current"].get(y)
        if dp.tag == "LongTermDebt" and cur is not None and dp.value > cur.value:
            s["lt_debt"][y] = DataPoint(value=dp.value - cur.value, fy=y, end=dp.end, accn=dp.accn,
                                        form=dp.form, tag=f"LongTermDebt-{cur.tag}")


def _derive_ebit(cd: "CompanyData") -> None:
    """EBIT dérivé = résultat avant impôt + charges d'intérêts, UNIQUEMENT pour les
    déposants qui ne publient jamais OperatingIncomeLoss (ex. ZTS, CTVA). Calcul à partir
    de deux tags publiés (pas une interpolation) ; tag 'derived:pretax+interest' tracé.
    Inclut les autres produits/charges non opérationnels → approximation, signalée."""
    if cd.series.get("ebit"):
        _ebit_gap_proxy(cd)
        return
    pre, intr = cd.series.get("pretax_income", {}), cd.series.get("interest_expense", {})
    derived = {y: DataPoint(value=pre[y].value + intr[y].value, fy=y, end=pre[y].end,
                            accn=pre[y].accn, form=pre[y].form, tag="derived:pretax+interest")
               for y in pre if y in intr}
    if derived:
        cd.series["ebit"] = derived


def _ebit_gap_proxy(cd: "CompanyData") -> None:
    """Exercices les plus récents sans EBIT publié alors que la société le publiait avant
    (Deere : OperatingIncomeLoss absent du 10-K 2025). Proxy explicite par société
    (config.EBIT_GAP_PROXY), jamais pour combler un trou au milieu de l'historique ; tag tracé."""
    import config
    proxy = getattr(config, "EBIT_GAP_PROXY", {}).get(cd.ticker)
    ebit = cd.series.get("ebit", {})
    if not proxy or not ebit:
        return
    last = max(ebit)
    for y, dp in cd.series.get(proxy, {}).items():
        if y > last and y not in ebit:
            ebit[y] = DataPoint(value=dp.value, fy=y, end=dp.end, accn=dp.accn, form=dp.form,
                                tag=f"proxy:{dp.tag} (OperatingIncomeLoss not tagged this year)", unit=dp.unit)
