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


def _fred_rate(series: str | None) -> tuple[float, str] | None:
    """Dernière valeur (décimale, mois) d'une série FRED via le CSV public, ou None."""
    if not series:
        return None
    key = f"fred_{series}"
    if key in _cache:
        return _cache[key]
    out = None
    try:
        r = requests.get(config.FRED_CSV_URL.format(series=series), timeout=15)
        r.raise_for_status()
        for line in reversed(r.text.strip().splitlines()[1:]):
            d, _, v = line.partition(",")
            try:
                val = float(v)
            except ValueError:
                continue
            if 0 < abs(val) < 20:
                out = (val / 100, d)
                break
    except Exception as e:
        log.warning("FRED %s indisponible (%s)", series, type(e).__name__)
    _cache[key] = out
    return out


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
    fred = None if rate is not None else _fred_rate(config.RF_FRED_SERIES_BY_CURRENCY.get(cur))
    if rate is not None:
        info = {"rf": rate, "currency": cur, "field": field, "proxy": False,
                "source": f"sainhe macro.json {field} ({config.MACRO_JSON_URL})"}
    elif fred is not None:
        info = {"rf": fred[0], "currency": cur, "field": config.RF_FRED_SERIES_BY_CURRENCY[cur], "proxy": False,
                "source": f"FRED {config.RF_FRED_SERIES_BY_CURRENCY[cur]} (OECD long-term yield, {fred[1]})"}
    else:
        us = _field_rate(data, config.RF_FIELD_BY_CURRENCY["USD"])
        if us is not None:
            peg = config.RF_USD_PEGS.get(cur)
            info = {"rf": us, "currency": cur, "field": "us_10y", "proxy": cur != "USD",
                    "proxy_reason": ("usd_peg" if peg else "no_source") if cur != "USD" else None,
                    "source": (f"PROXY : {peg} → US 10Y (sainhe macro.json us_10y)" if peg else
                               f"PROXY : aucun taux 10 ans {cur} publié → US 10Y (sainhe macro.json us_10y)")
                    if cur != "USD" else f"sainhe macro.json us_10y ({config.MACRO_JSON_URL})"}
        else:
            info = {"rf": config.DEFAULT_RISK_FREE, "currency": cur, "field": None, "proxy": cur != "USD",
                    "source": "config.DEFAULT_RISK_FREE (repli, macro.json indisponible)"}
    _cache[key] = info
    return info


def erp_info() -> dict:
    """{erp, date, source, url} — ERP implicite mensuelle de Damodaran ; repli DEFAULT_ERP signalé."""
    if "erp" in _cache:
        return _cache["erp"]
    info = {"erp": config.DEFAULT_ERP, "date": None, "url": None,
            "source": "convention 5% (Damodaran file unavailable)"}
    if os.environ.get("SAINHE_RF_LIVE", "1") == "0":
        info["source"] = "config.DEFAULT_ERP (SAINHE_RF_LIVE=0)"
    else:
        try:
            import io
            import pandas as pd
            r = requests.get(config.ERP_SOURCE_URL, timeout=30)
            r.raise_for_status()
            df = pd.read_excel(io.BytesIO(r.content), sheet_name="Last 12 months data")
            df = df[pd.to_numeric(df["ERP"], errors="coerce").notna() & pd.to_datetime(df["Date"], errors="coerce").notna()]
            last = df.iloc[-1]
            val = float(last["ERP"])
            if 0.01 < val < 0.12:
                info = {"erp": val, "date": str(pd.to_datetime(last["Date"]).date()), "url": config.ERP_SOURCE_URL,
                        "source": config.ERP_SOURCE_NAME}
        except Exception as e:
            log.warning("ERP Damodaran indisponible (%s) → convention %.1f %%", type(e).__name__, config.DEFAULT_ERP * 100)
    _cache["erp"] = info
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
