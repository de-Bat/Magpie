"""Checking the link a model gave for an item, and finding the real one when it's wrong.

A model reading a screenshot ("Two old GPUs I salvaged ... xda-developers") often names the right
site and article but invents the address. We open the link; if it's gone (404) or the page is about
something else, we search for the article by title, prefer results on the same site, and use the
first page whose title really matches. If nothing checks out, no link is better than a dead one.
"""

import logging
import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .enrich import Page, fetch_page, link_is_gone
from .fetch import BlockedURL, safe_get

log = logging.getLogger(__name__)

# Types whose link is a page about the thing, so a wrong or dead address is worth repairing.
REPAIRABLE = {"article", "video", "product", "place", "event", "course", "app", "podcast", "music", "other"}
_STOP = {"the", "and", "for", "with", "that", "this", "from", "your", "you", "are", "was", "how", "why", "what", "into", "have", "has"}
SEARCH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W_]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def page_matches(title: str, page: Page) -> bool:
    """Is this page about `title`? At least 60% of the title's significant words appear in the page's own headline."""
    wanted = _words(title)
    if not wanted:
        return False
    have = _words(" ".join(filter(None, [page.meta.get("og:title"), page.meta.get("twitter:title"), page.title,
                                         page.meta.get("og:description"), page.meta.get("description")])))
    return len(wanted & have) / len(wanted) >= 0.6


def _unwrap(href: str) -> str | None:
    """Search engines wrap result links (//duckduckgo.com/l/?uddg=<real url>)."""
    if href.startswith("//"):
        href = "https:" + href
    q = parse_qs(urlsplit(href).query)
    real = unquote(q["uddg"][0]) if "uddg" in q else href
    return real if real.startswith("http") and "duckduckgo.com" not in urlsplit(real).netloc else None


async def search_results(query: str, http: httpx.AsyncClient, limit: int = 6) -> list[str]:
    """Result addresses from DuckDuckGo's HTML page (no key). Empty when it refuses or changes shape."""
    try:
        r = await safe_get(http, "https://html.duckduckgo.com/html/", params={"q": query},
                           headers={"User-Agent": SEARCH_UA, "Accept": "text/html"}, timeout=12)
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Search failed: %s", e)
        return []
    if r.status_code != 200:
        return []
    found: list[str] = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
        url = _unwrap(href.replace("&amp;", "&"))
        if url and url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


async def find_article_url(title: str, wrong_url: str | None, http: httpx.AsyncClient) -> str | None:
    """The address of the page titled `title`, looked for on the same site first."""
    host = (urlsplit(wrong_url or "").hostname or "").removeprefix("www.")
    queries = ([f'"{title}" site:{host}'] if host else []) + [f'"{title}"']
    for query in queries:
        for url in await search_results(query, http):
            page = await fetch_page(url, http)
            if page and page_matches(title, page):
                return page.url
    return None


async def repair_link(analysis: dict, http: httpx.AsyncClient) -> dict:
    """The analysis with a working link: unchanged if the model's link opens and fits the title; otherwise the
    real article's address, or no link at all when it can't be found. Links you shared yourself are never touched."""
    url, title = analysis.get("canonical_url"), analysis.get("title")
    if (not url or not title or analysis.get("category") not in REPAIRABLE or not url.startswith("http")
            or "link" in (analysis.get("_analyzer") or [])):
        return analysis
    page = await fetch_page(url, http)
    if page and page_matches(title, page):
        return analysis
    if page is None and not link_is_gone(url):
        return analysis   # blocked, timing out or not a web page: we can't tell that it's wrong
    found = await find_article_url(title, url, http)
    log.info("Link %s for %r didn't check out; using %s", url, title, found)
    note = "" if found else " The link the model suggested doesn't work, so none is shown."
    return {**analysis, "canonical_url": found,
            "confidence_reason": ((analysis.get("confidence_reason") or "") + note).strip()}
