"""Faits d'un dépôt EDGAR lus dans son INSTANCE XBRL quand l'API companyfacts est en retard.

La SEC n'agrège pas toujours un dépôt récent dans companyfacts : TSMC a déposé son 20-F 2025
(XBRL) le 2026-04-16, absent de companyfacts au 21 nov. 2026 → comptes affichés « 23 mois ».
Le fichier *_htm.xml du dépôt est l'instance XBRL extraite par la SEC (mêmes faits, même
taxonomie) : on la lit et on ajoute ses faits au JSON companyfacts, au même format, avant le
parsing habituel. Seuls les faits numériques SANS dimension (contexte sans segment) sont gardés.
"""
from __future__ import annotations
import logging
try:   # durcit le parsing XML (entités externes, « billion laughs ») — dépendance légère
    from defusedxml.ElementTree import fromstring as _fromstring
except ImportError:  # pragma: no cover — repli stdlib (expat ne résout pas les entités externes)
    from xml.etree.ElementTree import fromstring as _fromstring

log = logging.getLogger("sainhe.edgar.instance")

XBRLI = "{http://www.xbrl.org/2003/instance}"
_TAXONOMIES = (("http://fasb.org/us-gaap/", "us-gaap"), ("https://xbrl.ifrs.org/taxonomy/", "ifrs-full"),
               ("http://xbrl.ifrs.org/taxonomy/", "ifrs-full"), ("http://xbrl.sec.gov/dei/", "dei"))


def _taxonomy(ns: str) -> str | None:
    return next((name for prefix, name in _TAXONOMIES if ns.startswith(prefix)), None)


def _measure(unit_el) -> str | None:
    """iso4217:TWD → TWD ; xbrli:shares → shares ; division → TWD/shares."""
    def local(m):
        return (m.text or "").split(":")[-1].strip()
    div = unit_el.find(XBRLI + "divide")
    if div is not None:
        num = div.find(XBRLI + "unitNumerator/" + XBRLI + "measure")
        den = div.find(XBRLI + "unitDenominator/" + XBRLI + "measure")
        return f"{local(num)}/{local(den)}" if num is not None and den is not None else None
    m = unit_el.find(XBRLI + "measure")
    return local(m) if m is not None else None


def parse_instance(xml_text: str | bytes, accn: str, form: str) -> dict:
    """Instance XBRL → {taxonomie: {concept: {"units": {unité: [items companyfacts]}}}}."""
    root = _fromstring(xml_text)
    contexts = {}
    for c in root.iter(XBRLI + "context"):
        ent = c.find(XBRLI + "entity")
        if (ent is not None and ent.find(XBRLI + "segment") is not None) or c.find(XBRLI + "scenario") is not None:
            continue                                   # fait dimensionnel : écarté
        p = c.find(XBRLI + "period")
        if p is None:
            continue
        inst, start, end = p.find(XBRLI + "instant"), p.find(XBRLI + "startDate"), p.find(XBRLI + "endDate")
        if inst is not None:
            contexts[c.get("id")] = (None, (inst.text or "").strip()[:10])
        elif start is not None and end is not None:
            contexts[c.get("id")] = ((start.text or "").strip()[:10], (end.text or "").strip()[:10])
    units = {u.get("id"): _measure(u) for u in root.iter(XBRLI + "unit")}
    out: dict = {}
    seen = set()
    for el in root:
        tag = el.tag
        if not tag.startswith("{") or el.get("contextRef") not in contexts or el.get("unitRef") is None:
            continue
        ns, concept = tag[1:].split("}", 1)
        tax = _taxonomy(ns)
        unit = units.get(el.get("unitRef"))
        if not tax or not unit or el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
            continue
        try:
            val = float((el.text or "").strip())
        except ValueError:
            continue
        start, end = contexts[el.get("contextRef")]
        key = (tax, concept, unit, start, end)
        if key in seen:
            continue
        seen.add(key)
        item = {"val": val, "end": end, "accn": accn, "form": form}
        if start:
            item["start"] = start
        out.setdefault(tax, {}).setdefault(concept, {"units": {}})["units"].setdefault(unit, []).append(item)
    return out


def merge_facts(facts_json: dict, extra: dict) -> dict:
    """Ajoute les faits d'une instance au JSON companyfacts (le parseur garde l'accession la plus récente)."""
    facts = facts_json.setdefault("facts", {})
    for tax, concepts in extra.items():
        dst = facts.setdefault(tax, {})
        for concept, node in concepts.items():
            d = dst.setdefault(concept, {"units": {}})
            for unit, items in node["units"].items():
                d["units"].setdefault(unit, []).extend(items)
    return facts_json


def newer_annual_filing(subs: dict, forms: tuple[str, ...], latest_year: int | None) -> dict | None:
    """Dernier dépôt annuel XBRL (forms) dont l'exercice dépasse latest_year, ou None."""
    r = (subs or {}).get("filings", {}).get("recent", {})
    for form, accn, rep, isx in zip(r.get("form", []), r.get("accessionNumber", []),
                                    r.get("reportDate", []), r.get("isXBRL", [])):
        if form in forms and isx and rep:
            try:
                year = int(rep[:4])
            except ValueError:
                continue
            return {"accn": accn, "report_date": rep, "form": form} if year > (latest_year or 0) else None
    return None


def instance_url(index: dict, cik: int, accn: str) -> str | None:
    """URL de l'instance XBRL extraite (*_htm.xml) dans l'index JSON du dépôt."""
    names = [i.get("name", "") for i in index.get("directory", {}).get("item", [])]
    pick = next((n for n in names if n.endswith("_htm.xml")), None) or next(
        (n for n in names if n.endswith(".xml") and not n.endswith(("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml"))
         and n != "FilingSummary.xml"), None)
    if not pick:
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{pick}"
