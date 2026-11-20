"""Couche 1 bis — Ingestion ESEF (XBRL européen) via filings.xbrl.org. Zéro LLM, gratuit.

Depuis l'exercice 2020, les sociétés cotées de l'UE (et du Royaume-Uni) publient leur rapport
financier annuel au format ESEF (iXBRL, taxonomie IFRS). filings.xbrl.org les republie en
xBRL-JSON, avec une API JSON:API indexée par LEI :
    /api/entities/{LEI}/filings  → liste des rapports (json_url, date_added, fxo_id…)
    https://filings.xbrl.org{json_url} → faits XBRL {concept, période, unité, dimensions}

Ce module convertit ces faits en une structure identique à l'API companyfacts de la SEC
({"facts": {"ifrs-full": {concept: {"units": {unité: [ {val,end,start,form,accn} ]}}}}})
pour réutiliser TEL QUEL edgar.xbrl_parser.parse_companyfacts(forms=("ESEF",)).

Règles :
- seuls les faits NUMÉRIQUES, ifrs-full, SANS dimension (totaux consolidés) sont gardés ;
- la période vient du fait lui-même, jamais des métadonnées du rapport (constaté : le
  rapport Hermès FY2025 est étiqueté period_end 2026-12-31) ;
- xBRL-JSON exprime les fins de période en instant exclusif ("2025-01-01T00:00:00" =
  fin du 31/12/2024) → on retire un jour ;
- à période égale, le rapport le plus récent gagne (comparatifs retraités) : accn =
  "ESEF|<date_added>|<viewer_url>" trie chronologiquement et sert de lien source ;
- un rapport publié est immuable → mis en cache sans expiration (seuls les nouveaux
  rapports sont téléchargés : c'est l'automatisation annuelle).
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import sqlite3
import time

import requests

import config

log = logging.getLogger("sainhe.esef")
API = "https://filings.xbrl.org"
HEADERS = {"Accept": "application/vnd.api+json"}
_KEEP_DIMS = {"concept", "entity", "period", "unit", "language"}


def _end_date(stamp: str) -> str:
    """'2025-01-01T00:00:00' (instant exclusif) → '2024-12-31'."""
    d, _, t = stamp.partition("T")
    day = dt.date.fromisoformat(d)
    if t.startswith("00:00:00"):
        day -= dt.timedelta(days=1)
    return day.isoformat()


def _unit(u: str | None) -> str:
    """'iso4217:EUR/xbrli:shares' → 'EUR/shares' ; 'xbrli:shares' → 'shares'."""
    if not u:
        return "pure"
    return "/".join(p.split(":")[-1] for p in u.split("/"))


def convert_xbrl_json(doc: dict, accn: str) -> dict[str, dict[str, list]]:
    """Faits xBRL-JSON → {concept_ifrs: {unité: [items companyfacts]}} (numériques, sans dimension)."""
    out: dict[str, dict[str, list]] = {}
    seen = set()
    for fact in doc.get("facts", {}).values():
        dims = fact.get("dimensions", {})
        concept = dims.get("concept", "")
        if not concept.startswith("ifrs-full:") or set(dims) - _KEEP_DIMS:
            continue
        try:
            val = float(fact.get("value"))
        except (TypeError, ValueError):
            continue
        period = dims.get("period", "")
        if not period:
            continue
        name, unit = concept.split(":", 1)[1], _unit(dims.get("unit"))
        if "/" in period:
            start_s, end_s = period.split("/")
            item = {"val": val, "start": start_s[:10], "end": _end_date(end_s)}
        else:
            item = {"val": val, "end": _end_date(period)}
        key = (name, unit, item.get("start"), item["end"])
        if key in seen:          # doublon (même fait répété dans le document)
            continue
        seen.add(key)
        item.update({"form": "ESEF", "accn": accn})
        out.setdefault(name, {}).setdefault(unit, []).append(item)
    return out


class EsefClient:
    def __init__(self, cache_path: str = config.CACHE_DB_PATH):
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        self._db = sqlite3.connect(cache_path)
        self._db.execute("CREATE TABLE IF NOT EXISTS esef_cache ("
                         " key TEXT PRIMARY KEY, fetched_at REAL, payload TEXT)")

    def _cached(self, key: str, max_age_days: float | None):
        """Payload en cache s'il est assez récent (max_age_days=None : sans expiration)."""
        row = self._db.execute("SELECT fetched_at, payload FROM esef_cache WHERE key=?", (key,)).fetchone()
        if row and (max_age_days is None or time.time() - row[0] < max_age_days * 86400):
            log.info("CACHE hit %s", key)
            return json.loads(row[1])
        return None

    def _store(self, key: str, payload) -> None:
        self._db.execute("INSERT OR REPLACE INTO esef_cache VALUES (?,?,?)",
                         (key, time.time(), json.dumps(payload)))
        self._db.commit()

    @staticmethod
    def _fetch(url: str, api: bool) -> dict:
        log.info("FETCH %s", url)
        last = None
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS if api else None, timeout=120)
                r.raise_for_status()
                return r.json()
            except Exception as e:     # réseau / 5xx : retry, puis erreur explicite
                last = e
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"ESEF {url} : {type(last).__name__}: {last}")

    def filings(self, lei: str) -> list[dict]:
        """Rapports annuels ESEF de l'entité, dédoublonnés (versions linguistiques), plus anciens d'abord."""
        key = f"esef_filings:{lei}"
        data = self._cached(key, config.ESEF_CACHE_DAYS_FILINGS)
        if data is None:
            data = self._fetch(f"{API}/api/entities/{lei}/filings", api=True)
            self._store(key, data)
        rows = [f["attributes"] for f in data.get("data", []) if f["attributes"].get("json_url")]
        # Plusieurs versions par exercice (langues, refilings) : la plus récente, puis la moins d'erreurs
        best: dict[str, dict] = {}
        for a in rows:
            k = a.get("fxo_id", "").rsplit("-", 2)[0] or a["json_url"]
            cur = best.get(k)
            if cur is None or (a.get("date_added", ""), -a.get("error_count", 0)) > \
                    (cur.get("date_added", ""), -cur.get("error_count", 0)):
                best[k] = a
        return sorted(best.values(), key=lambda a: a.get("date_added", ""))

    def companyfacts(self, lei: str, name: str = "") -> dict:
        """Structure companyfacts (namespace ifrs-full) fusionnant tous les rapports annuels."""
        merged: dict[str, dict[str, list]] = {}
        for a in self.filings(lei):
            key = f"esef_facts:{a['json_url']}"
            facts = self._cached(key, max_age_days=None)     # rapport publié = immuable
            if facts is None:
                doc = self._fetch(API + a["json_url"], api=False)
                accn = f"ESEF|{a.get('date_added', '')}|{a.get('viewer_url', '')}"
                facts = convert_xbrl_json(doc, accn)
                # On ne garde que la version convertie (≈ 50 Ko au lieu de ≈ 4 Mo)
                self._store(key, facts)
            for concept, units in facts.items():
                for unit, items in units.items():
                    merged.setdefault(concept, {}).setdefault(unit, []).extend(items)
        return {"entityName": name, "lei": lei, "facts": {"ifrs-full": {
            c: {"units": u} for c, u in merged.items()}}}


def filing_url(accn: str) -> str:
    """accn ESEF → URL du visualiseur iXBRL sur filings.xbrl.org (lien source par chiffre)."""
    parts = accn.split("|", 2)
    return API + parts[2] if len(parts) == 3 and parts[2] else ""
