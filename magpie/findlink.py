"""Checking the link a model gave for an item, and finding the real one when it's wrong.

A model reading a screenshot ("Two old GPUs I salvaged ... xda-developers") often names the right
site and article but invents the address. We open the link; if it's gone (404) or the page is about
something else, we search for the article by title, prefer results on the same site, and use the
first page whose title really matches. If nothing checks out, no link is better than a dead one.
"""

import base64
import html as html_lib
import logging
import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .enrich import Page, canonical_link, fetch_page, link_is_gone
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


def _unwrap_bing(href: str) -> str | None:
    """Bing wraps result links as bing.com/ck/a?...&u=a1<base64 of the real url>."""
    href = html_lib.unescape(href)
    if "bing.com/ck/" in href:
        u = (parse_qs(urlsplit(href).query).get("u") or [""])[0]
        if not u.startswith("a1"):
            return None
        try:
            href = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return None
    host = urlsplit(href).netloc
    return href if href.startswith("http") and not host.endswith(("bing.com", "microsoft.com", "msn.com")) else None


async def bing_results(query: str, http: httpx.AsyncClient, limit: int = 6) -> list[str]:
    """Result addresses from Bing's HTML page, when DuckDuckGo gives nothing."""
    try:
        r = await safe_get(http, "https://www.bing.com/search", params={"q": query, "setlang": "en"},
                           headers={"User-Agent": SEARCH_UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"}, timeout=12)
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Bing search failed: %s", e)
        return []
    if r.status_code != 200:
        return []
    found: list[str] = []
    for href in re.findall(r'<li class="b_algo"[^>]*>.*?<h2[^>]*>\s*<a[^>]*href="([^"]+)"', r.text, re.S):
        url = _unwrap_bing(href)
        if url and url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


async def search_results(query: str, http: httpx.AsyncClient, limit: int = 6) -> list[str]:
    """Result addresses for a query: DuckDuckGo's HTML page (no key), then Bing's. Empty when both refuse."""
    return await ddg_results(query, http, limit) or await bing_results(query, http, limit)


async def ddg_results(query: str, http: httpx.AsyncClient, limit: int = 6) -> list[str]:
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


def capture_sites(analysis: dict) -> list[str]:
    """Sites named in the capture ("xda-developers.com" under a shared headline): where to look for the article first."""
    from .related import NOISE_HOSTS, _TEXT_URL, _host
    text = "\n".join(filter(None, [analysis.get("screenshot_text"), analysis.get("_ocr_text")]))
    out: list[str] = []
    for m in _TEXT_URL.finditer(text):
        host = _host("https://" + m.group(1).split("/")[0])
        if host and host not in out and not any(host == h or host.endswith("." + h) for h in NOISE_HOSTS):
            out.append(host)
    return out[:2]


async def find_article_url(title: str, wrong_url: str | None, http: httpx.AsyncClient, publisher: str | None = None,
                           sites: list[str] = ()) -> str | None:
    """The address of the page titled `title`: looked for on the same site (or a site named in the capture, or at the
    named publisher) first, then anywhere, then with the headline's first words unquoted (a capture often cuts it)."""
    host = (urlsplit(wrong_url or "").hostname or "").removeprefix("www.")
    hosts = [h for h in [host, *sites] if h]
    words = title.split()
    queries = ([f'"{title}" site:{h}' for h in dict.fromkeys(hosts)] + ([f'"{title}" {publisher}'] if publisher else [])
               + [f'"{title}"'] + ([" ".join(words[:10]) + (f" {publisher}" if publisher else "")] if len(words) > 4 else []))
    tried: set[str] = set()
    for query in queries:
        for url in await search_results(query, http):
            if url in tried:
                continue
            tried.add(url)
            page = await fetch_page(url, http)
            if page and page_matches(title, page):
                return canonical_link(page)
    return None


async def repair_link(analysis: dict, http: httpx.AsyncClient) -> dict:
    """The analysis with a working link: unchanged if the model's link opens and fits the title; otherwise the
    real article's address, or no link at all when it can't be found. Links you shared yourself are never touched."""
    url, title = analysis.get("canonical_url"), analysis.get("title")
    if not url and title and analysis.get("category") == "article" and "link" not in (analysis.get("_analyzer") or []):
        # the model named the article but found no address for it: look it up
        publisher = ((analysis.get("details") or {}).get("publisher") or "").strip() or None
        found = await find_article_url(title, None, http, publisher, capture_sites(analysis))
        return {**analysis, "canonical_url": found} if found else analysis
    if (not url or not title or analysis.get("category") not in REPAIRABLE or not url.startswith("http")
            or "link" in (analysis.get("_analyzer") or [])):
        return analysis
    page = await fetch_page(url, http)
    if page and page_matches(title, page):
        return analysis
    if page is None and not link_is_gone(url):
        return analysis   # blocked, timing out or not a web page: we can't tell that it's wrong
    publisher = ((analysis.get("details") or {}).get("publisher") or "").strip() or None
    found = await find_article_url(title, url, http, publisher, capture_sites(analysis))
    log.info("Link %s for %r didn't check out; using %s", url, title, found)
    note = "" if found else " The link the model suggested doesn't work, so none is shown."
    return {**analysis, "canonical_url": found,
            "confidence_reason": ((analysis.get("confidence_reason") or "") + note).strip()}
