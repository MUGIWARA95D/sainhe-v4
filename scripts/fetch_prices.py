"""
fetch_prices.py — Télécharge OHLCV via curl_cffi (Chrome impersonation).
Remplace yfinance.download() — résistant au rate-limit Yahoo.
Ticker par ticker avec pause aléatoire entre chaque.

Stocke dans :
  - prices      : OHLCV journalier (INSERT OR REPLACE — corrige les rows stale)

⚠  Garde anti-intraday : l'API Yahoo v8 renvoie la journée COURANTE en cours
   (partial intraday) pendant les heures de trading. On la détecte et on la
   drop avant insertion via 2 critères :
     - volume < 30% de la médiane des 20 dernières → partial
     - pas de volume (indices ^VIX, ^TNX…) et date == aujourd'hui UTC → partial

Tickers fetchés : ALL_SECTOR_ETFS + ALL_INDEX_TICKERS + WATCHLIST

Lancement :
    python scripts/fetch_prices.py                           # update 7 jours (tous)
    python scripts/fetch_prices.py --mode full               # 2 ans complets (tous)
    python scripts/fetch_prices.py --mode full --half 0      # première moitié
    python scripts/fetch_prices.py --mode full --half 1      # deuxième moitié
    python scripts/fetch_prices.py --mode full --tickers LDO.MI CAT KAP.L
    python scripts/fetch_prices.py --mode backfill           # +30 jours en arrière/ticker → cible 7 ans
    python scripts/fetch_prices.py --mode backfill --tickers NVDA AAPL

Stratégie backfill :
    Chaque nuit, backfill étend l'historique de 30 jours en arrière par ticker.
    Après ~85 nuits (≈ 3 mois), la profondeur cible de 7 ans est atteinte.
    Les tickers sans données (GAZP, SPACEX, nouveaux) sont silencieusement skippés.
"""

import random
import sqlite3
import statistics
import time
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

from curl_cffi import requests as cffi

import db
from config import (
    ALL_SECTOR_ETFS,
    ALL_INDEX_TICKERS,
    WATCHLIST,
    PRICE_SOURCES,
)

# ============================================================
#  CONFIG
# ============================================================

SLEEP_MIN      = 10.0   # pause min entre tickers (aléatoire)
SLEEP_MAX      = 30.0   # pause max entre tickers
SLEEP_429      = 90.0   # attente sur rate-limit
MAX_RETRIES    = 2

NEW_TICKER_MIN_ROWS = 30  # en mode update, un ticker avec moins de lignes en base est "neuf"
                          # → fetch 2 ans d'un coup (sinon il n'aurait que 7 jours et
                          #   tous ses indicateurs resteraient N/D)

UPDATE_WINDOW_DAYS = 400  # mode update : fenêtre re-téléchargée (INSERT OR REPLACE).
                          # Même nombre de requêtes qu'avant, seulement plus de lignes.
REBASE_MIN_GAP     = 0.02 # écart DB/Yahoo au-delà duquel on considère l'historique réajusté
REBASE_MAX_SPREAD  = 0.01 # dispersion max des ratios sur les dates témoins (sinon : glitch)
REBASE_PROBE_DATES = 5    # nb de dates communes les plus anciennes utilisées comme témoins

BACKFILL_DAYS  = 30     # jours étendus en arrière par ticker et par run (backfill)
TARGET_YEARS   = 7      # profondeur cible en années pour le mode backfill

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fetch_prices] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
#  HELPERS
# ============================================================

def _all_tickers() -> list[str]:
    seen, result = set(), []
    for t in ALL_SECTOR_ETFS + ALL_INDEX_TICKERS + list(WATCHLIST.keys()):
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _periods_for_mode(mode: str) -> tuple[int, int]:
    """Retourne (period1, period2) en timestamps Unix pour update et full."""
    now = int(datetime.now(timezone.utc).timestamp())
    if mode == "update":
        # Fenêtre longue (et non 7 j) : elle chevauche l'historique en base, ce qui
        # permet à _rebase_if_needed() de détecter un split/spin-off réajusté par Yahoo.
        start = int((datetime.now(timezone.utc) - timedelta(days=UPDATE_WINDOW_DAYS)).timestamp())
    else:
        start = int((datetime.now(timezone.utc) - timedelta(days=730)).timestamp())  # 2 ans
    return start, now


def _backfill_window(con: sqlite3.Connection, ticker: str) -> tuple[int, int] | None:
    """
    Calcule la fenêtre backfill pour un ticker :
    - Trouve la date la plus ancienne dans prices
    - Remonte de BACKFILL_DAYS jours avant cette date
    - Retourne None si : pas de données existantes (besoin --mode full d'abord)
                         ou cible 7 ans déjà atteinte
    """
    row = con.execute("SELECT MIN(date) FROM prices WHERE ticker=?", (ticker,)).fetchone()
    if not row or not row[0]:
        return None  # pas de données — skip (besoin --mode full d'abord)

    oldest_dt = datetime.strptime(row[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    target_dt = datetime.now(timezone.utc) - timedelta(days=TARGET_YEARS * 365)

    if oldest_dt <= target_dt + timedelta(days=1):
        return None  # cible 7 ans déjà atteinte pour ce ticker

    period2 = int((oldest_dt - timedelta(days=1)).timestamp())
    start_dt = max(oldest_dt - timedelta(days=BACKFILL_DAYS + 1), target_dt)
    period1  = int(start_dt.timestamp())
    return period1, period2


def _filter_partial_intraday(ticker: str, rows: list[tuple]) -> list[tuple]:
    """
    Drop la dernière row si elle ressemble à de l'intraday partiel.

    Critères (dans l'ordre) :
      1. Si volume disponible et > 0 : vol < 30% de la médiane des 20 précédentes
      2. Sinon (indices sans volume) : date == aujourd'hui UTC

    Ne drop qu'UNE seule row (la dernière). Retourne les rows conservées.
    """
    if len(rows) < 5:
        return rows

    last = rows[-1]
    last_date = last[1]
    last_vol  = last[7]

    # ── Règle 1 : volume heuristic ──────────────────────────────
    if last_vol and last_vol > 0:
        prior_vols = [r[7] for r in rows[-21:-1] if r[7] and r[7] > 0]
        if len(prior_vols) >= 5:
            median_vol = statistics.median(prior_vols)
            if last_vol < median_vol * 0.3:
                log.warning(
                    "%s — drop partial intraday %s (vol=%s vs median=%s = %.0f%%)",
                    ticker, last_date, f"{int(last_vol):,}", f"{int(median_vol):,}",
                    last_vol / median_vol * 100,
                )
                return rows[:-1]
        return rows

    # ── Règle 2 : no volume (indices) → date-based ─────────────────
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if last_date == today_utc:
        log.warning("%s — drop partial intraday %s (no volume, date=today)", ticker, last_date)
        return rows[:-1]

    return rows


# ============================================================
#  FETCH — curl_cffi Chrome impersonation
# ============================================================

def _maybe_update_devise(con: sqlite3.Connection, ticker: str, currency: str) -> None:
    """
    Met à jour ticker_info.devise si Yahoo Finance rapporte une devise différente
    de celle actuellement stockée. Garde une trace dans les logs.
    """
    row = con.execute(
        "SELECT devise FROM ticker_info WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row and row[0] != currency:
        con.execute(
            "UPDATE ticker_info SET devise = ? WHERE ticker = ?", (currency, ticker)
        )
        con.commit()
        log.info("devise auto-update: %s  %s → %s  (source: Yahoo meta.currency)",
                 ticker, row[0], currency)


def _fetch_moex(ticker: str, period1: int, period2: int) -> tuple[list[tuple] | None, str | None]:
    """Bourse de Moscou (moex.py) : pas de dividende ajusté dans l'ISS → adj_close = close."""
    import moex
    start = datetime.fromtimestamp(period1, tz=timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(period2, tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        hist = moex.history(ticker, start, end)
    except Exception as exc:
        log.error("%s — MOEX indisponible : %s", ticker, exc)
        return None, None
    rows = [(ticker, h["date"], h["open"], h["high"], h["low"], h["close"], h["close"], h["volume"]) for h in hist]
    return (rows or None), "RUB"


def fetch_one(ticker: str, period1: int, period2: int) -> tuple[list[tuple] | None, str | None]:
    """
    Télécharge l'historique OHLCV pour un ticker via Yahoo Finance v8.
    Retourne (rows, detected_currency) :
      - rows    : liste de tuples (ticker, date, open, high, low, close, adj_close, volume)
      - currency: devise native détectée dans meta.currency (ex: "JPY", "USD"…)
    Retourne (None, None) sur échec.
    """
    if PRICE_SOURCES.get(ticker) == "moex":
        return _fetch_moex(ticker, period1, period2)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "period1"  : period1,
        "period2"  : period2,
        "interval" : "1d",
        "events"   : "history",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = cffi.get(url, params=params, impersonate="chrome110", timeout=15)

            if resp.status_code == 429:
                log.warning("%s — 429 rate-limit, attente %.0fs (tentative %d/%d)",
                            ticker, SLEEP_429, attempt, MAX_RETRIES)
                time.sleep(SLEEP_429)
                continue

            if resp.status_code == 404:
                log.warning("%s — 404 introuvable, skip", ticker)
                return None, None

            if resp.status_code != 200:
                log.warning("%s — HTTP %d (tentative %d/%d)",
                            ticker, resp.status_code, attempt, MAX_RETRIES)
                time.sleep(10)
                continue

            data   = resp.json()
            result = data.get("chart", {}).get("result")
            if not result:
                log.warning("%s — réponse vide", ticker)
                return None, None

            res               = result[0]
            detected_currency = res.get("meta", {}).get("currency")   # ex: "JPY", "USD", "CHF"
            ts_list           = res.get("timestamp", [])
            quote             = res["indicators"]["quote"][0]
            adj_list          = res["indicators"].get("adjclose", [{}])[0].get("adjclose", [None] * len(ts_list))

            rows = []
            for j, ts in enumerate(ts_list):
                c = quote["close"][j]
                if c is None:
                    continue
                date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                rows.append((
                    ticker,
                    date,
                    quote["open"][j],
                    quote["high"][j],
                    quote["low"][j],
                    c,
                    adj_list[j] if adj_list[j] else c,
                    quote["volume"][j],
                ))

            # Garde anti-intraday : drop la dernière row si partial
            rows = _filter_partial_intraday(ticker, rows)
            return rows, detected_currency

        except Exception as exc:
            log.warning("%s — erreur tentative %d/%d : %s", ticker, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(SLEEP_429 if "429" in str(exc) else 10)

    log.error("%s — abandon après %d tentatives.", ticker, MAX_RETRIES)
    return None, None


# ============================================================
#  INSERT
# ============================================================

def _rebase_if_needed(con: sqlite3.Connection, ticker: str, rows: list[tuple]) -> float | None:
    """
    Corrige les splits / spin-offs. Yahoo réajuste rétroactivement TOUT l'historique
    (close = ajusté des splits), mais la base ne réécrit que la fenêtre téléchargée :
    les lignes plus anciennes restaient à l'ancienne échelle. Cas réel : CTVA, spin-off
    2026-10-01 (facteur 6,665) → ret_1y affiché −89 % au lieu de −18 %.

    Méthode (sans dépendre des "events" Yahoo, incomplets pour les spin-offs) :
      k = close_DB / close_Yahoo sur les REBASE_PROBE_DATES dates communes les plus
      anciennes. Si tous les k concordent (dispersion < REBASE_MAX_SPREAD) et que
      |k − 1| > REBASE_MIN_GAP, les lignes en base ANTÉRIEURES à la fenêtre sont
      divisées par k (prix) et multipliées par k (volume). Celles de la fenêtre sont
      ensuite remplacées par insert_prices. Idempotent : une fois corrigé, k ≈ 1.

    Retourne k si une correction a été appliquée, sinon None.
    """
    if not rows:
        return None
    window_start = rows[0][1]
    yahoo_close = {r[1]: r[5] for r in rows if r[5]}
    db_rows = con.execute(
        "SELECT date, close FROM prices WHERE ticker = ? AND date >= ? ORDER BY date ASC",
        (ticker, window_start),
    ).fetchall()
    ratios = [
        c_db / yahoo_close[d]
        for d, c_db in db_rows
        if c_db and d in yahoo_close and yahoo_close[d]
    ][:REBASE_PROBE_DATES]
    if len(ratios) < 3:
        return None

    k = statistics.median(ratios)
    spread = (max(ratios) - min(ratios)) / k
    if abs(k - 1) <= REBASE_MIN_GAP:
        return None
    if spread > REBASE_MAX_SPREAD:
        log.warning("%s — écart DB/Yahoo incohérent (ratios %s) : pas de rebase, à vérifier",
                    ticker, [round(r, 4) for r in ratios])
        return None

    n = con.execute(
        """
        UPDATE prices
           SET open = open / ?, high = high / ?, low = low / ?,
               close = close / ?, adj_close = adj_close / ?, volume = volume * ?
         WHERE ticker = ? AND date < ?
        """,
        (k, k, k, k, k, k, ticker, window_start),
    ).rowcount
    con.commit()
    log.warning("REBASE %s — historique réajusté par Yahoo (split/spin-off), facteur %.4f "
                "appliqué à %d lignes antérieures au %s", ticker, k, n, window_start)
    return k


def insert_prices(con: sqlite3.Connection, rows: list[tuple]) -> int:
    """
    INSERT OR REPLACE : écrase les rows stales avec les EOD finaux.
    Couplé à _filter_partial_intraday() en amont pour ne pas écrire de partial.
    """
    cur = con.cursor()
    cur.executemany(
        """
        INSERT OR REPLACE INTO prices
            (ticker, date, open, high, low, close, adj_close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    return cur.rowcount


# ============================================================
#  MAIN
# ============================================================

def main(mode: str = "update", half: int = -1, tickers_filter: list[str] | None = None):
    """
    mode           : "update" | "full" | "backfill"
    half           : -1 = tous | 0 = première moitié | 1 = deuxième moitié (ignoré si tickers_filter)
    tickers_filter : liste de tickers à restreindre (ex: ["LDO.MI","CAT","KAP.L"])
    """
    tickers = _all_tickers()

    if tickers_filter:
        wanted = set(tickers_filter)
        tickers = [t for t in tickers if t in wanted]
        missing = wanted - set(tickers)
        if missing:
            log.warning("--tickers non reconnus (absents de config) : %s", ", ".join(sorted(missing)))
        log.info("Mode=%s | tickers=%s (%d)", mode, ", ".join(tickers), len(tickers))
    elif half in (0, 1):
        mid     = len(tickers) // 2
        tickers = tickers[:mid] if half == 0 else tickers[mid:]
        label   = "A (1-50)" if half == 0 else "B (51-100)"
        log.info("Mode=%s | half=%s | %d tickers", mode, label, len(tickers))
    else:
        log.info("Mode=%s | %d tickers (tous)", mode, len(tickers))

    if mode != "backfill":
        global_period1, global_period2 = _periods_for_mode(mode)

    con            = db.connect()
    total_inserted = 0
    total_ok       = 0
    total_skip     = 0
    total_done     = 0

    for i, ticker in enumerate(tickers):
        if mode == "backfill":
            window = _backfill_window(con, ticker)
            if window is None:
                row = con.execute("SELECT MIN(date) FROM prices WHERE ticker=?", (ticker,)).fetchone()
                if not row or not row[0]:
                    log.info("SKIP_BF  %s — aucune donnée locale (lancer --mode full d'abord)", ticker)
                else:
                    log.info("DONE_BF  %s — cible %dy déjà atteinte (oldest: %s)", ticker, TARGET_YEARS, row[0])
                    total_done += 1
                total_skip += 1
                continue
            period1, period2 = window
        else:
            period1, period2 = global_period1, global_period2
            if mode == "update":
                n_local = con.execute(
                    "SELECT COUNT(*) FROM prices WHERE ticker=?", (ticker,)
                ).fetchone()[0]
                if n_local < NEW_TICKER_MIN_ROWS:
                    period1, period2 = _periods_for_mode("full")
                    log.info("NEW  %s — %d lignes en base (< %d) → fetch 2 ans",
                             ticker, n_local, NEW_TICKER_MIN_ROWS)

        rows, detected_currency = fetch_one(ticker, period1, period2)

        if rows is None:
            total_skip += 1
            log.info("SKIP %s", ticker)
        else:
            if mode != "backfill":
                _rebase_if_needed(con, ticker, rows)   # AVANT insert : compare à l'ancien état
            n = insert_prices(con, rows)
            total_inserted += n
            total_ok       += 1
            log.info("OK   %s — %d rows (%d new)%s",
                     ticker, len(rows), n,
                     f"  [devise={detected_currency}]" if detected_currency else "")

        if detected_currency:
            _maybe_update_devise(con, ticker, detected_currency)

        if i < len(tickers) - 1:
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

    con.close()
    summary = f"Terminé — {total_ok} OK, {total_skip} skip"
    if mode == "backfill":
        summary += f" ({total_done} déjà à cible)"
    summary += f", {total_inserted} nouvelles lignes insérées."
    log.info(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch OHLCV via curl_cffi (Chrome)")
    parser.add_argument(
        "--mode", choices=["full", "update", "backfill"], default="update",
        help="full = 2 ans | update = 7 jours | backfill = +30j en arrière vers cible 7 ans",
    )
    parser.add_argument(
        "--half", type=int, choices=[-1, 0, 1], default=-1,
        help="-1 = tous | 0 = première moitié | 1 = deuxième moitié (ignoré si --tickers)",
    )
    parser.add_argument(
        "--tickers", nargs="+", default=None, metavar="TICKER",
        help="Restreindre à une liste de tickers (ex: --tickers LDO.MI CAT KAP.L)",
    )
    args = parser.parse_args()
    main(mode=args.mode, half=args.half, tickers_filter=args.tickers)
