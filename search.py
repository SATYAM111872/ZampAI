"""Stage 3 search tool. Tries backends in order until one returns results:

1. ddgs (metasearch; good snippets). Its bundled HTTP client uses its own DNS
   resolver, which fails intermittently on some networks.
2. Google News RSS (system DNS; real publication dates; respects quotes/OR).
   Links are news.google.com redirects to the article.
3. Bing web RSS (system DNS). Its pubDate is a crawl date, so it is discarded.

Each result: url, title, snippet, published_date (ISO or None), query, origin, backend.
"""

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

import config as C

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
_TAG = re.compile(r"<[^>]+>")


def _rec(query, url, title, snippet, published, backend):
    return {"url": url, "title": (title or "").strip(), "snippet": (snippet or "").strip(),
            "published_date": published, "query": query, "origin": "search", "backend": backend}


def _ddgs(query, news):
    from ddgs import DDGS
    d = DDGS(timeout=C.SEARCH_TIMEOUT_S)
    if news:
        rows = d.news(query, max_results=C.RESULTS_PER_QUERY) or []
        return [_rec(query, r.get("url"), r.get("title"), r.get("body"), r.get("date"), "ddgs-news") for r in rows]
    rows = d.text(query, max_results=C.RESULTS_PER_QUERY) or []
    return [_rec(query, r.get("href"), r.get("title"), r.get("body"), None, "ddgs-web") for r in rows]


def _rss_items(url, params):
    r = requests.get(url, params=params, headers=_UA, timeout=C.SEARCH_TIMEOUT_S)
    r.raise_for_status()
    return list(ET.fromstring(r.content).iter("item"))


def _iso(pub):
    try:
        return parsedate_to_datetime(pub).date().isoformat() if pub else None
    except (TypeError, ValueError):
        return None


def _google_news(query):
    items = _rss_items("https://news.google.com/rss/search",
                       {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    out = []
    for it in items[:C.RESULTS_PER_QUERY]:
        title = it.findtext("title") or ""
        src = it.find("source")
        outlet = src.text if src is not None else ""
        desc = html.unescape(_TAG.sub(" ", it.findtext("description") or "")).strip()
        snippet = desc if desc and desc not in title else f"{title} ({outlet})"
        out.append(_rec(query, it.findtext("link"), title, snippet, _iso(it.findtext("pubDate")), "google-news-rss"))
    return out


def _bing_web(query):
    items = _rss_items("https://www.bing.com/search", {"q": query, "format": "rss", "count": C.RESULTS_PER_QUERY})
    return [_rec(query, it.findtext("link"), it.findtext("title"), it.findtext("description"), None, "bing-rss")
            for it in items[:C.RESULTS_PER_QUERY]]


def search(q):
    """q = {"bucket": "company"|"person", "query": str}. Returns a list of results.
    Raises RuntimeError with every backend's error if all of them fail."""
    query = q["query"]
    news_first = q["bucket"] == "company" and not query.startswith("site:")
    chain = [("ddgs", lambda: _ddgs(query, news=news_first))]
    if news_first:
        chain.append(("ddgs-web", lambda: _ddgs(query, news=False)))
    chain += [("google-news-rss", lambda: _google_news(query)), ("bing-rss", lambda: _bing_web(query))]

    errors = []
    for name, fn in chain:
        try:
            rows = [r for r in fn() if r["url"]]
        except Exception as e:  # network, rate limit, parse error, "No results found"
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if rows:
            return rows
        errors.append(f"{name}: no results")
    if all(e.endswith("no results") or "No results" in e for e in errors):
        return []
    raise RuntimeError(" | ".join(errors))
