"""news_filter.py — drops programme blurbs (TV, radio, podcasts) from the RSS news.
Used by scripts/fetch_news.py (never stored) and boxes/box_03_news.py (rows stored before the filter)."""
import re

from config import NEWS_EXCLUDE_PATTERNS

_EXCLUDE = [re.compile(p, re.I | re.M) for p in NEWS_EXCLUDE_PATTERNS]


def is_programme_blurb(titre: str, resume: str) -> bool:
    text = (titre or "") + "\n" + (resume or "")
    return any(p.search(text) for p in _EXCLUDE)
