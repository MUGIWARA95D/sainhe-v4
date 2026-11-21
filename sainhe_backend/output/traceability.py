"""Traçabilité fine — accession EDGAR par chiffre (.txt §3 Couche 4 + point 75 drill-down).

Pour chaque tag XBRL parsé, on expose dans le bloc drill_down :
  tag_interne → {value, fy, end, accession, edgar_url}

URL EDGAR par accession :
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K
  +  index_url précis du filing : sec.gov/Archives/edgar/data/{cik}/{accn_no_dashes}/
"""
from __future__ import annotations
from edgar.xbrl_parser import CompanyData


def _accn_to_url(cik: int, accn: str) -> str:
    """URL de la source : filing index EDGAR, ou visualiseur iXBRL filings.xbrl.org (ESEF)."""
    if not accn:
        return ""
    if accn.startswith("YAHOO|"):
        from secondary.yahoo_statements import page_url
        return page_url(accn.split("|", 1)[1])
    if accn.startswith("ESEF|"):
        from esef.esef_client import filing_url
        return filing_url(accn)
    no_dash = accn.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{no_dash}/"


def build_source_map(cd: CompanyData) -> dict[str, dict]:
    """Pour chaque tag, retourne le dernier DataPoint avec accession + URL EDGAR.
    Drill-down ligne par ligne : l'utilisateur clique un chiffre → URL du filing."""
    out = {}
    for tag, series in cd.series.items():
        if not series:
            continue
        latest_fy = max(series)
        dp = series[latest_fy]
        out[tag] = {
            "value": dp.value,
            "fiscal_year": dp.fy,
            "end_date": dp.end,
            "accession": dp.accn,
            "filing_form": dp.form,
            "xbrl_tag": dp.tag,          # tag source (ou composition / dérivation tracée)
            "unit": dp.unit,
            "edgar_filing_url": _accn_to_url(cd.cik, dp.accn),
        }
    return out


def edgar_filings_index_url(cik: int) -> str:
    """URL de la liste complète des filings 10-K du ticker."""
    return (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik:0>10}&type=10-K&dateb=&owner=include&count=40")
