"""Web search through an API the user chose: Brave, Tavily, Serper, SerpApi, or their own SearXNG.

The keyless search (DuckDuckGo's and Bing's HTML pages) refuses servers after a few queries or answers them with
unrelated results, so finding an article's real address worked one time and not the next. A search API answers
every time; each one listed here has a free tier, and SearXNG costs nothing when you run it yourself.
"""

import logging
from typing import Any, NamedTuple

import httpx

from .fetch import BlockedURL

log = logging.getLogger(__name__)

# provider -> (label, settings attribute holding its key, or None when it needs none)
PROVIDERS = {
    "brave": ("Brave Search API", "brave_search_api_key"),
    "tavily": ("Tavily", "tavily_api_key"),
    "serper": ("Serper (Google results)", "serper_api_key"),
    "serpapi": ("SerpApi (Google results)", "serpapi_api_key"),
    "searxng": ("SearXNG (self-hosted)", None),
}

_settings: Any = None


class Hit(NamedTuple):
    url: str
    title: str = ""


def configure(settings: Any) -> None:
    """Remember the settings object (read on every search, so a change made in the app applies at once)."""
    global _settings
    _settings = settings


def active() -> str | None:
    """The chosen provider when it is ready to use (its key or server address is set), else None."""
    name = getattr(_settings, "search_provider", None) if _settings is not None else None
    if name not in PROVIDERS:
        return None
    if name == "searxng":
        return name if (getattr(_settings, "searxng_url", None) or "").startswith("http") else None
    return name if getattr(_settings, PROVIDERS[name][1], None) else None


async def api_results(query: str, http: httpx.AsyncClient, limit: int = 6) -> tuple[list[Hit], str]:
    """Results from the chosen search API, and how the search went ("not configured" when there is none)."""
    name = active()
    if not name:
        return [], "not configured"
    key = getattr(_settings, PROVIDERS[name][1], None) if PROVIDERS[name][1] else None
    try:
        r = await _request(name, key, query, http, limit)
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("%s search failed: %s", name, e)
        return [], f"failed: {type(e).__name__}"
    if r.status_code != 200:
        hint = " (enable the json format in SearXNG's settings.yml)" if name == "searxng" and r.status_code == 403 else ""
        return [], f"refused (HTTP {r.status_code}){hint}"
    try:
        hits = _parse(name, r.json())
    except (ValueError, AttributeError, TypeError) as e:
        return [], f"unreadable answer: {type(e).__name__}"
    found: list[Hit] = []
    for h in hits:
        if h.url.startswith("http") and all(h.url != f.url for f in found):
            found.append(h)
    return found[:limit], "ok" if found else "no results"


async def _request(name: str, key: str | None, query: str, http: httpx.AsyncClient, limit: int) -> httpx.Response:
    if name == "brave":
        return await http.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": limit},
                              headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=12)
    if name == "tavily":
        return await http.post("https://api.tavily.com/search", json={"query": query, "max_results": limit},
                               headers={"Authorization": f"Bearer {key}"}, timeout=15)
    if name == "serper":
        return await http.post("https://google.serper.dev/search", json={"q": query, "num": limit},
                               headers={"X-API-KEY": key}, timeout=12)
    if name == "serpapi":
        return await http.get("https://serpapi.com/search.json", params={"engine": "google", "q": query, "num": limit, "api_key": key},
                              timeout=15)
    # SearXNG usually runs next to Magpie (http://searxng:8080), so it is asked directly, without the public-address
    # check other fetches get: it is the address you entered, and its redirects aren't followed
    base = _settings.searxng_url.rstrip("/")
    return await http.get(f"{base}/search", params={"q": query, "format": "json"}, headers={"Accept": "application/json"},
                          timeout=12, follow_redirects=False)


def _parse(name: str, data: dict) -> list[Hit]:
    if name == "brave":
        rows, url_key = (data.get("web") or {}).get("results") or [], "url"
    elif name in ("tavily", "searxng"):
        rows, url_key = data.get("results") or [], "url"
    elif name == "serper":
        rows, url_key = data.get("organic") or [], "link"
    else:
        rows, url_key = data.get("organic_results") or [], "link"
    return [Hit(str(r[url_key]), str(r.get("title") or "")) for r in rows if isinstance(r, dict) and r.get(url_key)]

