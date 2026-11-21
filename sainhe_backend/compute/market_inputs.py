"""Entrées de marché du WACC branchées sur le module Macro SAINHE (README « reste à faire » n°1).

Rf = taux souverain 10 ans de la DEVISE DES COMPTES, publié par le dashboard
(/data/macro.json, en %) : USD us_10y, EUR bund_10y, GBP uk_10y, JPY jgb_10y, CNY cgb_10y
(config.RF_FIELD_BY_CURRENCY). Devise sans taux publié (TWD) → US 10Y en SUBSTITUT SIGNALÉ
(proxy=True, flag rf_currency_proxy dans le rapport), jamais en silence.
Repli documenté : config.DEFAULT_RISK_FREE si le site est injoignable ou la valeur absente.
macro.json est lu une seule fois par exécution (cache en mémoire).
"""
from __future__ import annotations
import logging
import os
import requests
import config

log = logging.getLogger("sainhe.market_inputs")
_cache: dict = {}


def _macro_data() -> dict | None:
    """Champ « data » de macro.json, ou None (réseau absent / tests hermétiques)."""
    if "macro" in _cache:
        return _cache["macro"]
    data = None
    if os.environ.get("SAINHE_RF_LIVE", "1") != "0":
        try:
            r = requests.get(config.MACRO_JSON_URL, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {})
        except Exception as e:  # réseau absent : repli documenté, jamais d'exception silencieuse
            log.warning("macro.json indisponible (%s) → repli %.2f %%", type(e).__name__,
                        config.DEFAULT_RISK_FREE * 100)
    _cache["macro"] = data
    return data


def _field_rate(data: dict | None, field: str) -> float | None:
    node = (data or {}).get(field)
    val = node.get("value") if isinstance(node, dict) else node
    return val / 100 if isinstance(val, (int, float)) and 0 < val < 20 else None


def risk_free_info(currency: str | None = "USD") -> dict:
    """{rf, source, currency, field, proxy}. proxy=True : la devise n'a pas de taux publié,
    le US 10Y est utilisé à sa place (à signaler dans le rapport)."""
    cur = (currency or "USD").upper()
    if cur in ("GBP", "GBX"):
        cur = "GBP"
    key = f"rf_{cur}"
    if key in _cache:
        return _cache[key]
    if os.environ.get("SAINHE_RF_LIVE", "1") == "0":   # tests hermétiques (sans réseau)
        info = {"rf": config.DEFAULT_RISK_FREE, "currency": cur, "field": None, "proxy": False,
                "source": "config.DEFAULT_RISK_FREE (SAINHE_RF_LIVE=0)"}
        _cache[key] = info
        return info
    data = _macro_data()
    field = config.RF_FIELD_BY_CURRENCY.get(cur)
    rate = _field_rate(data, field) if field else None
    if rate is not None:
        info = {"rf": rate, "currency": cur, "field": field, "proxy": False,
                "source": f"sainhe macro.json {field} ({config.MACRO_JSON_URL})"}
    else:
        us = _field_rate(data, config.RF_FIELD_BY_CURRENCY["USD"])
        if us is not None:
            info = {"rf": us, "currency": cur, "field": "us_10y", "proxy": cur != "USD",
                    "source": (f"PROXY : aucun taux 10 ans {cur} publié → US 10Y "
                               f"(sainhe macro.json us_10y)") if cur != "USD"
                    else f"sainhe macro.json us_10y ({config.MACRO_JSON_URL})"}
        else:
            info = {"rf": config.DEFAULT_RISK_FREE, "currency": cur, "field": None, "proxy": cur != "USD",
                    "source": "config.DEFAULT_RISK_FREE (repli, macro.json indisponible)"}
    _cache[key] = info
    return info


def risk_free(currency: str | None = "USD") -> tuple[float, str]:
    """(taux décimal, source). Ex. (0.0505, 'sainhe macro.json us_10y')."""
    i = risk_free_info(currency)
    return i["rf"], i["source"]


def blume_beta(raw: float | None) -> float | None:
    """Beta ajusté de Blume : 0,67·β + 0,33 (retour vers 1 des betas historiques).
    Évite des Ke irréalistes (KO β brut 0,27 → Ke 5,5 %). None → None."""
    if raw is None:
        return None
    return config.BLUME_WEIGHT * raw + (1 - config.BLUME_WEIGHT)
