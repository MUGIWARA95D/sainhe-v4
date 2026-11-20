"""Entrées de marché du WACC branchées sur le module Macro SAINHE (README « reste à faire » n°1).

Rf = US 10Y publié par le dashboard (/data/macro.json, champ us_10y, en %).
Repli documenté : config.DEFAULT_RISK_FREE si le site est injoignable ou la valeur absente.
Lu une seule fois par exécution (cache en mémoire).
"""
from __future__ import annotations
import logging
import os
import requests
import config

log = logging.getLogger("sainhe.market_inputs")
_cache: dict[str, tuple[float, str]] = {}


def risk_free() -> tuple[float, str]:
    """(taux décimal, source). Ex. (0.0505, 'sainhe macro.json us_10y')."""
    if "rf" in _cache:
        return _cache["rf"]
    rf, src = config.DEFAULT_RISK_FREE, "config.DEFAULT_RISK_FREE (repli)"
    if os.environ.get("SAINHE_RF_LIVE", "1") == "0":   # tests hermétiques (sans réseau)
        _cache["rf"] = (rf, "config.DEFAULT_RISK_FREE (SAINHE_RF_LIVE=0)")
        return _cache["rf"]
    try:
        r = requests.get(config.MACRO_JSON_URL, timeout=10)
        r.raise_for_status()
        node = r.json().get("data", {}).get("us_10y")
        val = node.get("value") if isinstance(node, dict) else node
        if isinstance(val, (int, float)) and 0 < val < 20:
            rf, src = val / 100, f"sainhe macro.json us_10y ({config.MACRO_JSON_URL})"
    except Exception as e:  # réseau absent : repli documenté, jamais d'exception silencieuse
        log.warning("Rf live indisponible (%s) → repli %.2f %%", type(e).__name__, rf * 100)
    _cache["rf"] = (rf, src)
    return _cache["rf"]


def blume_beta(raw: float | None) -> float | None:
    """Beta ajusté de Blume : 0,67·β + 0,33 (retour vers 1 des betas historiques).
    Évite des Ke irréalistes (KO β brut 0,27 → Ke 5,5 %). None → None."""
    if raw is None:
        return None
    return config.BLUME_WEIGHT * raw + (1 - config.BLUME_WEIGHT)
