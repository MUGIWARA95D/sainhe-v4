"""Rendements du portefeuille ACTUEL rejoué dans le passé, sans reconstitution de données.

Avant : chaque module faisait prices.pct_change().dropna() → un seul titre récent (GEV, coté
depuis 2024-03-27) supprimait TOUTES les journées antérieures : plus de rendements 3Y/5Y et
aucun stress test (2008, 2013, 2018, 2020, 2022) rejouable.

Règle (standard « backfill-free ») : chaque jour, seuls les titres cotés ce jour-là comptent,
poids renormalisés entre eux. Un jour où la part du portefeuille couverte est inférieure à
min_coverage est écarté. La couverture est renvoyée pour être affichée, jamais masquée.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

MIN_COVERAGE = 0.5


def portfolio_returns(prices: pd.DataFrame, weights: dict[str, float],
                      min_coverage: float = MIN_COVERAGE) -> tuple[pd.Series, pd.Series]:
    """(rendements journaliers du portefeuille, couverture en poids de chaque jour)."""
    cols = [t for t in weights if t in prices.columns]
    w = pd.Series({t: weights[t] for t in cols}, dtype=float)
    w = w / w.sum()
    rets = prices[cols].pct_change(fill_method=None)
    avail = rets.notna()
    cover = (avail * w).sum(axis=1)
    port = (rets.fillna(0.0) * w).sum(axis=1) / cover.replace(0, np.nan)
    keep = cover >= min_coverage
    keep.iloc[0] = False                       # première ligne : pas de rendement
    return port[keep], cover[keep]


def missing_holdings(prices: pd.DataFrame, weights: dict[str, float], start: str, end: str) -> list[str]:
    """Titres du portefeuille sans aucune cotation sur [start, end]."""
    win = prices.loc[start:end]
    return sorted(t for t in weights if t in prices.columns and win[t].notna().sum() == 0)
