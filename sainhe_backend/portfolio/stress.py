"""BLOC 4 Portfolio — Stress tests historiques (.txt §4 + audit points 43-47).
Rejouer le portefeuille ACTUEL sur les régimes de stress passés :
  GFC 2008 / COVID mars 2020 / Taper Tantrum 2013 / Volmageddon fév 2018 / hike cycle 2022.
Output : drawdown rejoué + durée de récupération par scénario.
"""
from __future__ import annotations
import pandas as pd

from portfolio._common import MIN_COVERAGE, missing_holdings, portfolio_returns

SCENARIOS = {
    "GFC_2008":         ("2008-09-01", "2009-03-31"),
    "COVID_2020":       ("2020-02-19", "2020-04-30"),
    "Taper_Tantrum_2013": ("2013-05-22", "2013-09-30"),
    "Volmageddon_2018": ("2018-02-01", "2018-02-15"),
    "Hike_Cycle_2022":  ("2022-01-01", "2022-10-31"),
}


def replay_scenario(prices: pd.DataFrame, weights: dict[str, float],
                    start: str, end: str) -> dict:
    """Cumule les returns du portefeuille actuel sur la fenêtre du scénario.
    Titres pas encore cotés à l'époque : exclus, poids renormalisés, et LISTÉS ; la part du
    portefeuille réellement rejouée (coverage) est renvoyée (portfolio/_common.py)."""
    rets, cover = portfolio_returns(prices, weights)
    win = rets.loc[start:end]
    missing = missing_holdings(prices, weights, start, end)
    if win.empty:
        return {"available": False, "missing_holdings": missing,
                "note": f"pas assez de titres cotés entre {start} et {end} "
                        f"(< {int(MIN_COVERAGE * 100)} % du portefeuille)"}
    eq = (1 + win).cumprod()
    peak = eq.cummax()
    dd = ((eq - peak) / peak).min()
    final = eq.iloc[-1] - 1
    # Durée de récupération : combien de jours après end pour revenir au peak ?
    after = rets.loc[end:]
    recovery_days = None
    if len(after) > 1:
        eq_after = (1 + after).cumprod() * eq.iloc[-1]
        target = peak.iloc[-1] if len(peak) else 1.0
        recovered = eq_after[eq_after >= target]
        if len(recovered):
            recovery_days = int((recovered.index[0] - pd.Timestamp(end)).days)
    return {"available": True, "start": start, "end": end,
            "scenario_return": float(final),
            "max_drawdown_during": float(dd),
            "recovery_days_after": recovery_days,
            "n_days": int(len(win)),
            "coverage_weight": float(cover.loc[start:end].mean()),
            "missing_holdings": missing}


def run_all(prices: pd.DataFrame, weights: dict[str, float]) -> dict:
    """Tous les scénarios. Format : 5 cartes horizontales pour le front (.txt §4)."""
    return {name: replay_scenario(prices, weights, s, e)
            for name, (s, e) in SCENARIOS.items()}
