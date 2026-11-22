"""moex.py — daily share prices from the Moscow Exchange (ISS API: official, free, no key).

Gazprom (GAZP) has had no Western quote since the 2022 sanctions: Yahoo's GAZP.ME stops on
8 July 2022 and the London/US depositary receipts were delisted. The exchange's own feed is
the primary source. Used by scripts/fetch_prices.py (dashboard) and portfolio_runner.py (Lab).
"""
from __future__ import annotations

URL = "https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities/{secid}.json"
PAGE = 100            # the ISS API returns at most 100 rows per call


def history(secid: str, start: str = "2000-01-01", end: str | None = None, session=None) -> list[dict]:
    """[{date, open, high, low, close, volume}] oldest first; close in roubles (RUB)."""
    if session is None:
        from curl_cffi import requests as cffi
        session = cffi.Session(impersonate="chrome")
    out, offset = [], 0
    while True:
        params = {"from": start, "start": offset, "iss.meta": "off",
                  "history.columns": "TRADEDATE,OPEN,HIGH,LOW,CLOSE,LEGALCLOSEPRICE,VOLUME"}
        if end:
            params["till"] = end
        r = session.get(URL.format(secid=secid), params=params, timeout=30)
        r.raise_for_status()
        block = r.json()["history"]
        rows = block.get("data") or []
        cols = block["columns"]
        for row in rows:
            d = dict(zip(cols, row))
            close = d.get("CLOSE") or d.get("LEGALCLOSEPRICE")
            if not close:
                continue
            out.append({"date": d["TRADEDATE"], "open": d.get("OPEN") or close, "high": d.get("HIGH") or close,
                        "low": d.get("LOW") or close, "close": float(close), "volume": d.get("VOLUME") or 0})
        offset += len(rows)
        if len(rows) < PAGE:
            break
    return out
