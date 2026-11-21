"""Tests de validation — chaque formule vérifiée contre les valeurs calculées à la main
(cf. docstring de tests/fixtures/testco.py). Règle Phase 1 du prompt :
'le Net Debt doit matcher au million près' → ici au centime, fixture déterministe.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Tests hermétiques : pas d'appel réseau pour le taux sans risque (valeurs attendues
# calculées à la main avec config.DEFAULT_RISK_FREE = 4,2 %).
os.environ["SAINHE_RF_LIVE"] = "0"

from edgar.xbrl_parser import parse_companyfacts
from compute import metrics, valuation, dupont, buffett, scores
from compute.adjustments import adjust
from output import report_builder
from tests.fixtures.testco import testco_companyfacts, badco_companyfacts

PASS, FAIL = 0, 0


def check(name, got, expected, tol=1e-2):
    global PASS, FAIL
    ok = (got is not None and expected is not None and abs(got - expected) <= tol)
    if ok:
        PASS += 1
        print(f"  ✓ {name}: {got:.4f}")
    else:
        FAIL += 1
        print(f"  ✗ {name}: got={got} expected={expected}")


def check_true(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✓ {name}")
    else:
        FAIL += 1; print(f"  ✗ {name}")


print("=" * 60)
print("TESTCO — formules de base")
print("=" * 60)
cd = parse_companyfacts("TESTCO", 999999, testco_companyfacts())
m = metrics.derive(cd, 2023)

check("Net Debt ASC842 (300+30+15-100-20 placements CT)", m["net_debt"], 225.0)
check("EBITDA (250+50)", m["ebitda"], 300.0)
check("Effective tax (46/230)", m["effective_tax_rate"], 0.20)
check("NOPAT (250×0.8)", m["nopat"], 200.0)
check("Invested Capital (700+225)", m["invested_capital"], 925.0)
check("ROIC (200/925)", m["roic"], 200.0 / 925.0, tol=1e-4)
check("FCF brut (260-70)", m["fcf_brut"], 190.0)
check("FCF ajusté SBC (190-25)", m["fcf_adj"], 165.0)
check("Owner Earnings (260-50-25)", m["owner_earnings"], 185.0)
check("Gross Margin (400/1000)", m["gross_margin"], 0.40, tol=1e-4)
check("Interest Coverage (250/20)", m["interest_coverage"], 12.5)
check("OCF/NI (260/184)", m["ocf_ni"], 260.0 / 184.0, tol=1e-4)
check("DSO (120/1000×365)", m["dso"], 43.8)
check("CCC (43.8+48.67-54.75)", m["ccc"], 43.8 + 80/600*365 - 90/600*365, tol=0.1)

print("\nReformulation Penman")
ref = m["reformulation"]
check("FA (100+20)", ref["FA"], 120.0)
check("FO (300+30+15)", ref["FO"], 345.0)
check("NOA (1380-455)", ref["NOA"], 925.0)
check("NFO (345-120)", ref["NFO"], 225.0)
check("Equity check (NOA-NFO-Equity=0)", ref["equity_check"], 0.0)

print("\nAdvanced DuPont")
d = dupont.advanced_dupont(cd, 2023)
check("RNOA (200/925)", d["RNOA"], 200.0 / 925.0, tol=1e-4)
check("FLEV (225/700)", d["FLEV"], 225.0 / 700.0, tol=1e-4)
check("NBC (20×0.8/225)", d["NBC"], 16.0 / 225.0, tol=1e-4)
check("ROE dupont ≈ ROE direct", d["coherence_gap"], 0.0, tol=1e-3)

print("\nInstance XBRL d'un dépôt (companyfacts en retard)")
from edgar import instance as _inst
_xml = """<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
 xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
 <context id="FY25"><entity><identifier scheme="x">1</identifier></entity><period><startDate>2025-01-01</startDate><endDate>2025-12-31</endDate></period></context>
 <context id="I25"><entity><identifier scheme="x">1</identifier></entity><period><instant>2025-12-31</instant></period></context>
 <context id="SEG"><entity><identifier scheme="x">1</identifier><segment><xbrldi:explicitMember dimension="a">b</xbrldi:explicitMember></segment></entity><period><instant>2025-12-31</instant></period></context>
 <unit id="TWD"><measure>iso4217:TWD</measure></unit>
 <ifrs-full:Revenue contextRef="FY25" unitRef="TWD" decimals="-6">3809000000000</ifrs-full:Revenue>
 <ifrs-full:Assets contextRef="I25" unitRef="TWD" decimals="-6">7000000000000</ifrs-full:Assets>
 <ifrs-full:Assets contextRef="SEG" unitRef="TWD" decimals="-6">1</ifrs-full:Assets>
</xbrl>"""
_ex = _inst.parse_instance(_xml, "0001628280-26-025362", "20-F")
_rev = _ex["ifrs-full"]["Revenue"]["units"]["TWD"][0]
check("instance : chiffre d'affaires lu", _rev["val"], 3.809e12, tol=1)
check_true("instance : période annuelle conservée", _rev["start"] == "2025-01-01" and _rev["end"] == "2025-12-31")
check_true("instance : fait dimensionnel écarté", len(_ex["ifrs-full"]["Assets"]["units"]["TWD"]) == 1)
_subs = {"filings": {"recent": {"form": ["6-K", "20-F"], "accessionNumber": ["a", "0001628280-26-025362"],
                                "reportDate": ["2026-09-30", "2025-12-31"], "isXBRL": [0, 1]}}}
check_true("instance : dépôt annuel plus récent détecté", (_inst.newer_annual_filing(_subs, ("20-F",), 2024) or {}).get("accn") == "0001628280-26-025362")
check_true("instance : rien si companyfacts est à jour", _inst.newer_annual_filing(_subs, ("20-F",), 2025) is None)
_cx = _xml.replace('<ifrs-full:Revenue', '<de:LongTermDebtAndFinanceLeasesNoncurrent xmlns:de="http://www.deere.com/20251102" contextRef="I25" unitRef="TWD">42000</de:LongTermDebtAndFinanceLeasesNoncurrent><ifrs-full:Revenue')
_cf = _inst.custom_facts(_cx, {"LongTermDebtAndFinanceLeasesNoncurrent"})
check("instance : élément propre à la société lu (Deere)", _cf["LongTermDebtAndFinanceLeasesNoncurrent"]["2025-12-31"][0], 42000.0)
check_true("instance : éléments standards ignorés par custom_facts", "Revenue" not in _cf)

print("\nSource secondaire Yahoo (relevés simulés, sans réseau)")
import pandas as _pd
from secondary import yahoo_statements as _ys
_cols = [_pd.Timestamp("2025-12-31"), _pd.Timestamp("2024-12-31")]
_frames = {"income": _pd.DataFrame({_cols[0]: [100.0, 30.0, 20.0, 5.0], _cols[1]: [90.0, 27.0, float("nan"), 4.0]},
                                   index=["Total Revenue", "Operating Income", "Pretax Income", "Tax Provision"]),
           "balance": _pd.DataFrame({_cols[0]: [12.0], _cols[1]: [10.0]}, index=["Cash And Cash Equivalents"]),
           "cashflow": _pd.DataFrame({_cols[0]: [25.0, -8.0], _cols[1]: [22.0, -7.0]}, index=["Operating Cash Flow", "Capital Expenditure"])}
_ycd = _ys.company_from_frames("ROP.SW", _frames, "CHF")
check("Yahoo : chiffre d'affaires 2025", _ycd.value("revenue", 2025), 100.0)
check("Yahoo : capex positif (signe Yahoo négatif)", _ycd.value("capex", 2025), 8.0)
check_true("Yahoo : NaN → année absente (pas de 0)", _ycd.value("pretax_income", 2024) is None)
check_true("Yahoo : devise des comptes et tag tracés", _ycd.reporting_currency() == "CHF"
           and _ycd.latest("revenue").tag == "yahoo:Total Revenue")

print("\nValorisation")
w = valuation.wacc(cd, price=15.0)
# Ke = 0.042 + 1.0×0.05 = 0.092 ; E=1500, D=345, V=1845
# WACC = 0.092×(1500/1845) + (20/330)×0.8×(345/1845)
exp_wacc = 0.092 * (1500/1845) + (20/330) * 0.8 * (345/1845)
check("Ke (rf+β×ERP)", w["ke"], 0.092, tol=1e-4)
check("WACC complet", w["wacc"], exp_wacc, tol=1e-4)

e = valuation.epv(cd, w["wacc"], 2023)
# EPV_EV = 250×0.8/wacc ; equity = EV-225 ; /100 shares
exp_ev = 200.0 / w["wacc"]
check("EPV enterprise", e["epv_ev"], exp_ev, tol=0.5)
check("EPV per share", e["epv_per_share"], (exp_ev - 225.0) / 100.0, tol=0.05)

r = valuation.reverse_dcf(cd, price=15.0, wacc_value=w["wacc"])
# EV = 15×100+225 = 1725 ; g = (wacc×1725-165)/(1725+165)
exp_g = (w["wacc"] * 1725 - 165.0) / (1725.0 + 165.0)
check("Reverse DCF implied g", r["implied_g"], exp_g, tol=1e-4)

s = valuation.sgr(cd, 2023)
check_true("SGR calculé (non-None)", s["sgr"] is not None)
check_true("SGR : statut publié", s.get("status") in ("ok", "n/m", "N/D"))

print("\nValeurs non significatives (n/m) — seuils config.SGR_NM_* / IC_NM_MIN_SHARE_OF_ASSETS")
check_true("n/m : réinvestissement négatif",
           valuation.sgr_status(-0.03, 500.0, 1000.0) == ("n/m", "negative_net_reinvestment"))
check_true("n/m : réinvestissement > NOPAT",
           valuation.sgr_status(1.6, 500.0, 1000.0) == ("n/m", "reinvestment_exceeds_nopat"))
check_true("n/m : capital investi < 10 % de l'actif",
           valuation.sgr_status(0.3, 42.0, 1000.0) == ("n/m", "invested_capital_near_zero"))
check_true("n/m : capital investi négatif",
           valuation.sgr_status(0.3, -10.0, 1000.0) == ("n/m", "invested_capital_near_zero"))
check_true("ok : cas normal", valuation.sgr_status(0.3, 500.0, 1000.0) == ("ok", None))
check_true("N/D : réinvestissement inconnu", valuation.sgr_status(None, 500.0, 1000.0)[0] == "N/D")

print("\nTaux d'impôt aberrant → médiane des exercices normaux (config.TAX_RATE_*)")
class _TaxStub:
    def __init__(self, d): self.d = d
    def value(self, k, y): return self.d.get(k, {}).get(y)
    def years(self, k): return sorted(self.d.get(k, {}))
_ts = _TaxStub({"tax_expense": {2021: 20.0, 2022: 21.0, 2023: 22.0, 2024: -50.0},
                "pretax_income": {2021: 100.0, 2022: 100.0, 2023: 100.0, 2024: 69.0}})
_t24 = metrics.effective_tax(_ts, 2024)
check("impôt : taux négatif → médiane 3 ans", _t24["rate"], 0.21, tol=1e-9)
check_true("impôt : source = median, brut conservé", _t24["source"] == "median" and _t24["raw"] < 0)
check_true("impôt : exercice normal → taux de l'année", metrics.effective_tax(_ts, 2023)["source"] == "year")
_ts2 = _TaxStub({"tax_expense": {2023: 22.0, 2024: 5.0}, "pretax_income": {2023: 100.0, 2024: -40.0}})
check_true("impôt : perte et 1 seul exercice normal → N/D", metrics.effective_tax(_ts2, 2024)["rate"] is None)

print("\nTaux sans risque par devise (macro.json simulé, sans réseau)")
from compute import market_inputs
_env = os.environ.get("SAINHE_RF_LIVE")
os.environ["SAINHE_RF_LIVE"] = "1"
market_inputs._cache.clear()
market_inputs._cache["macro"] = {"us_10y": {"value": 4.5}, "bund_10y": {"value": 2.6}, "uk_10y": 4.4}
eur, twd, usd = (market_inputs.risk_free_info(c) for c in ("EUR", "TWD", "USD"))
check("Rf EUR = Bund 10 ans", eur["rf"], 0.026, tol=1e-9)
check_true("Rf EUR non substitué", eur["proxy"] is False and eur["field"] == "bund_10y")
check("Rf TWD = substitut US 10 ans", twd["rf"], 0.045, tol=1e-9)
check_true("Rf TWD signalé comme substitut", twd["proxy"] is True and "PROXY" in twd["source"])
check_true("Rf USD non substitué", usd["proxy"] is False)
check_true("WACC EUR : Ke au Bund", abs(valuation.wacc(cd, price=15.0, currency="EUR")["ke"] - (0.026 + 0.05)) < 1e-9)
market_inputs._cache["fred_IRLTLT01CHM156N"] = (0.0051, "2026-10-01")
chf, sar = market_inputs.risk_free_info("CHF"), market_inputs.risk_free_info("SAR")
check("Rf CHF = FRED OCDE (Confédération 10 ans)", chf["rf"], 0.0051, tol=1e-12)
check_true("Rf CHF non substitué, source FRED", chf["proxy"] is False and "FRED" in chf["source"])
check_true("Rf SAR : substitut US justifié par l'ancrage", sar["proxy"] is True and sar.get("proxy_reason") == "usd_peg")
market_inputs._cache.clear()
if _env is None:
    os.environ.pop("SAINHE_RF_LIVE", None)
else:
    os.environ["SAINHE_RF_LIVE"] = _env

re_ = valuation.residual_earnings_static(cd, w["ke"], s["sgr"])
check_true("RE valuation per share > 0", isinstance(re_["re_value_per_share"], float)
           and re_["re_value_per_share"] > 0)

sm = valuation.sensitivity_matrix(cd, w["ke"], s["sgr"])
check_true("Sensitivity matrix 5×5", sm["matrix"] is not None and len(sm["matrix"]) == 5
           and len(sm["matrix"][0]) == 5)
check_true("Sensitivity matrix : axe g non constant (0 → plafond)", len(set(sm["g_axis"])) == 5)

print("\nBuffett")
lt = buffett.lt_debt_payback(cd)
check("LT Debt payback (300/184)", lt["years"], 300.0 / 184.0, tol=1e-3)
gm = buffett.gross_margin_trend(cd)
check_true("GM trend = expansion", gm["direction"] == "expansion")
roe_c = buffett.roe_consistency(cd)
check_true("ROE consistency disponible", roe_c["available"])
re_test = buffett.retained_earnings_dollar_test(
    cd, price_history={2018: 9.0, 2023: 15.0})
check_true("$1 Test calculé avec prix fournis", isinstance(re_test["ratio"], float))

print("\nScores TESTCO (sain — flags minimaux)")
z = scores.altman_z(cd, market_cap=1500.0, fy=2023)
check_true(f"Altman Z = {z['z']:.2f} en zone safe/grey", z["zone"] in ("safe", "grey"))
b = scores.beneish_m(cd, 2023)
check_true(f"Beneish M = {b['m']:.2f} sous le seuil", b["flag"] is False)
flags = scores.all_flags(cd, {"nt_filing": {"triggered": False},
                              "restatement_2y": {"triggered": False}}, 1500.0)
ids = {f["id"] for f in flags}
check_true("TESTCO : pas de flag composite goodwill", "goodwill_roic_composite" not in ids)
check_true("TESTCO : pas de flag SBC", "sbc_15pct_fcf" not in ids)

print("\n" + "=" * 60)
print("BADCO — tous les drapeaux rouges doivent tirer")
print("=" * 60)
bad = parse_companyfacts("BADCO", 999998, badco_companyfacts())
rt = scores.roic_trend(bad)
check_true("ROIC déclinant 3Y+ détecté", rt["declining_3y_plus"])
bflags = scores.all_flags(bad, {"nt_filing": {"triggered": True, "details": []},
                                "restatement_2y": {"triggered": False}}, 1200.0)
bids = {f["id"] for f in bflags}
for expected_flag in ["goodwill_30pct", "goodwill_roic_composite", "sbc_15pct_fcf",
                      "lease_heavy", "goodwill_impairment", "nt_filing"]:
    check_true(f"flag {expected_flag} tiré", expected_flag in bids)
badj = adjust(bad, 2023)
check_true("Impairment dans les ajustements transitoires",
           any(a["item"] == "goodwill_impairment" for a in badj["adjustments"]))
check_true("NI ajusté > NI brut (addback impairment)",
           badj["ni_adj"] > badj["ni_brut"])

print("\nRapport complet end-to-end")
rep = report_builder.build_report(
    cd, price=15.0, yf_info={"targetMeanPrice": 16.0, "targetMedianPrice": 15.5,
                             "targetHighPrice": 18.0, "targetLowPrice": 13.0,
                             "numberOfAnalystOpinions": 8, "beta": 1.1,
                             "sector": "Industrials"},
    edgar_meta={"nt_filing": {"triggered": False}, "restatement_2y": {"triggered": False}},
    price_history={2018: 9.0, 2023: 15.0})
for bloc in ["bloc_A_verdict", "bloc_B_flags", "bloc_C_dupont", "bloc_D_trends",
             "bloc_E_health", "bloc_F_peers", "bloc_G_smart_money",
             "bloc_H_sensitivity", "bloc_I_macro", "score_composite", "drill_down"]:
    check_true(f"bloc présent : {bloc}", bloc in rep)
check_true("score composite 0-100",
           0 <= rep["score_composite"]["value_0_100"] <= 100)
check_true("consensus dispersion calculée",
           isinstance(rep["bloc_A_verdict"]["consensus"]["dispersion"], float))
vmap = report_builder.build_valuation_map([rep])
check_true("valuation map 1 ligne", len(vmap["rows"]) == 1)
row = vmap["rows"][0]
for k in ("roic_by_year", "roic_nm_years", "wacc", "rf", "rf_currency", "sgr_status", "rdcf", "flags"):
    check_true(f"valuation map : champ {k}", k in row)
check_true("valuation map : entrées du DCF inverse",
           all(k in row["rdcf"] for k in ("ev", "fcf_adj", "market_cap", "net_debt", "fiscal_year")))
check_true("valuation map : compte ROIC > WACC cohérent avec la série",
           row["roic_years_above_wacc"].endswith("/" + str(len([v for v in row["roic_by_year"].values()
                                                                  if isinstance(v, float)]))))

# Échantillon écrit dans tests/out/ (ignoré par git) : un fichier suivi modifié par les tests
# bloquait le « git pull --rebase » du job hebdomadaire (run 44952654312).
os.makedirs(os.path.join(os.path.dirname(__file__), "out"), exist_ok=True)
report_builder.save(rep, os.path.join(os.path.dirname(__file__), "out", "TESTCO_report_sample.json"))
print(f"\n{'='*60}\nRÉSULTAT : {PASS} ✓ / {FAIL} ✗")
sys.exit(1 if FAIL else 0)
