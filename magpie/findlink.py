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
from typing import NamedTuple
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from . import convlog
from .enrich import Page, canonical_link, fetch_page, link_is_gone, strip_tracking
from .fetch import BlockedURL, safe_get
from .related import NOISE_HOSTS, _TEXT_URL, _host

log = logging.getLogger(__name__)

# Types whose link is a page about the thing, so a wrong or dead address is worth repairing.
REPAIRABLE = {"article", "video", "product", "place", "event", "course", "app", "podcast", "music", "other"}
_STOP = {"the", "and", "for", "with", "that", "this", "from", "your", "you", "are", "was", "how", "why", "what", "into", "have", "has"}
SEARCH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

AGGREGATOR_HOSTS = {
    "daily.dev", "app.daily.dev",
    "reddit.com", "old.reddit.com",
    "news.ycombinator.com",
    "flipboard.com",
    "feedly.com",
    "threads.net",
    "bsky.app",
    "twitter.com", "x.com",
    "facebook.com",
    "linkedin.com",
}


def is_aggregator_host(host: str) -> bool:
    h = (host or "").lower().removeprefix("www.")
    return any(h == a or h.endswith("." + a) for a in AGGREGATOR_HOSTS)


def is_aggregator(url: str | None) -> bool:
    if not url:
        return False
    return is_aggregator_host(urlsplit(url).hostname or "")


def is_aggregator_name(name: str | None) -> bool:
    if not name:
        return False
    clean = name.strip().lower()
    if is_aggregator_host(clean):
        return True
    return clean in {"daily.dev", "daily dev", "reddit", "hacker news", "hackernews", "flipboard", "feedly", "threads", "bluesky", "twitter", "x"}


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


class Hit(NamedTuple):
    url: str
    title: str = ""


def _text(fragment: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


async def bing_results(query: str, http: httpx.AsyncClient, limit: int = 6) -> tuple[list[Hit], str]:
    """Results (address and title) from Bing's HTML page, and how the search went."""
    try:
        r = await safe_get(http, "https://www.bing.com/search", params={"q": query, "setlang": "en"},
                           headers={"User-Agent": SEARCH_UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"}, timeout=12)
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Bing search failed: %s", e)
        return [], f"failed: {type(e).__name__}"
    if r.status_code != 200:
        return [], f"refused (HTTP {r.status_code})"
    found: list[Hit] = []
    for href, title in re.findall(r'<li class="b_algo"[^>]*>.*?<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        url = _unwrap_bing(href)
        if url and all(url != h.url for h in found):
            found.append(Hit(url, _text(title)))
        if len(found) >= limit:
            break
    return found, "ok" if found else "no results (blocked or changed page?)"


async def search_results(query: str, http: httpx.AsyncClient, limit: int = 6, trace: list | None = None) -> list[Hit]:
    """Results for a query: DuckDuckGo's HTML page (no key), then Bing's. Empty when both refuse. `trace` collects how
    each engine answered, so a failed search can be told from a search that found nothing."""
    outcome: dict = {"query": query, "engines": {}}
    hits, outcome["engines"]["duckduckgo"] = await ddg_results(query, http, limit)
    if not hits:
        hits, outcome["engines"]["bing"] = await bing_results(query, http, limit)
    outcome["results"] = len(hits)
    if trace is not None:
        trace.append(outcome)
    return hits


async def ddg_results(query: str, http: httpx.AsyncClient, limit: int = 6) -> tuple[list[Hit], str]:
    """Results (address and title) from DuckDuckGo's HTML page (no key), and how the search went."""
    try:
        r = await safe_get(http, "https://html.duckduckgo.com/html/", params={"q": query},
                           headers={"User-Agent": SEARCH_UA, "Accept": "text/html"}, timeout=12)
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Search failed: %s", e)
        return [], f"failed: {type(e).__name__}"
    if r.status_code != 200:
        return [], f"refused (HTTP {r.status_code})"
    found: list[Hit] = []
    for href, title in re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        url = _unwrap(href.replace("&amp;", "&"))
        if url and all(url != h.url for h in found):
            found.append(Hit(url, _text(title)))
        if len(found) >= limit:
            break
    return found, "ok" if found else "no results (blocked or changed page?)"


def capture_sites(analysis: dict) -> list[str]:
    """Sites named in the capture ("xda-developers.com" under a shared headline): where to look for the article first."""
    text = "\n".join(filter(None, [analysis.get("screenshot_text"), analysis.get("_ocr_text")]))
    out: list[str] = []
    for m in _TEXT_URL.finditer(text):
        host = _host("https://" + m.group(1).split("/")[0])
        if host and host not in out and not any(host == h or host.endswith("." + h) for h in NOISE_HOSTS) and not is_aggregator_host(host):
            out.append(host)
    return out[:2]


def _slug_words(url: str) -> set[str]:
    return _words(re.sub(r"[-_/.]+", " ", unquote(urlsplit(url).path)))


def _share(title: str, text_words: set[str]) -> float:
    wanted = _words(title)
    return len(wanted & text_words) / len(wanted) if wanted else 0.0


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _clean(url: str) -> str:
    """The address without tracking parameters or a fragment (a page we couldn't open has no canonical link to use).
    Other query parameters stay: they can identify the page (watch?v=..., ?id=...)."""
    return strip_tracking(url) or url


def _extract_page_urls(page: Page) -> list[str]:
    """Find external URLs in page HTML, JSON-LD, and meta tags."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str | None) -> None:
        if not u or not isinstance(u, str):
            return
        u = html_lib.unescape(u.strip())
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http://") and not u.startswith("https://"):
            return
        cleaned = strip_tracking(u) or u
        if cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)

    add(page.meta.get("og:see_also"))

    for node in page.ld:
        if isinstance(node, dict):
            for key in ("mainEntityOfPage", "isBasedOn", "isBasedOnUrl", "citation", "sameAs", "url"):
                val = node.get(key)
                if isinstance(val, str):
                    add(val)
                elif isinstance(val, dict):
                    add(val.get("@id") or val.get("url"))
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            add(item)
                        elif isinstance(item, dict):
                            add(item.get("@id") or item.get("url"))

    for m in re.finditer(r'"(?:sourceUrl|originalUrl|canonicalUrl|articleUrl|permalink|targetUrl|originUrl|externalUrl)":\s*"([^"]+)"', page.html or ""):
        val = m.group(1).replace(r"\/", "/")
        add(val)

    for href in re.findall(r'<a\b[^>]*?\bhref=["\']([^"\']+)["\']', page.html or "", re.I):
        add(href)

    return urls


async def _outbound_article_link(page: Page, title: str, wanted_hosts: list[str], http: httpx.AsyncClient) -> str | None:
    raw_urls = _extract_page_urls(page)
    candidates: list[str] = []
    page_host = _site(page.url)

    for u in raw_urls:
        u_host = _site(u)
        if not u_host or u_host == page_host or is_aggregator(u):
            continue
        if any(u_host == nh or u_host.endswith("." + nh) for nh in NOISE_HOSTS):
            continue
        path = urlsplit(u).path.strip("/")
        if not path or path in ("login", "signup", "about", "terms", "privacy", "contact"):
            continue
        candidates.append(u)

    if not candidates:
        return None

    def score(u: str) -> tuple[int, float]:
        on_wanted = 1 if any(_site(u) == wh or _site(u).endswith("." + wh) for wh in wanted_hosts) else 0
        slug_fit = _share(title, _slug_words(u))
        return (on_wanted, slug_fit)

    scored = [c for c in candidates if score(c)[0] == 1 or score(c)[1] >= 0.3]
    scored.sort(key=score, reverse=True)

    for cand in scored[:4]:
        verdict, found = await judge(title, cand, "", wanted_hosts, http)
        if found:
            return found

    return None


async def judge(title: str, url: str, search_title: str, wanted_hosts: list[str], http: httpx.AsyncClient) -> tuple[str, str | None]:
    """(verdict, address) for a candidate page. When the page can't be opened (a bot wall), the search result's own
    title and the address's slug stand in for it: they must fit the title and the site must be one we were looking at."""
    if is_aggregator(url):
        return "aggregator link", None
    page = await fetch_page(url, http)
    if page is not None:
        target = canonical_link(page)
        if is_aggregator(target):
            return "aggregator link", None
        return ("matched", target) if page_matches(title, page) else ("different page", None)
    if link_is_gone(url):
        return "gone", None
    # The page is walled off. A real article's search title is its headline, and its address carries the headline's words.
    evidence = _share(title, _words(search_title) | _slug_words(url))
    on_site = any(_site(url) == h or _site(url).endswith("." + h) for h in wanted_hosts)
    if evidence >= (0.6 if on_site else 0.85):
        return "accepted from the result's title (page blocked)", _clean(url)
    return "page blocked, title doesn't fit", None


async def find_article_url(title: str, wrong_url: str | None, http: httpx.AsyncClient, publisher: str | None = None,
                           sites: list[str] = (), sources: list[str] = (),
                           source_titles: dict[str, str] | None = None) -> str | None:
    """The address of the page titled `title`: first the pages the model's own web search found, then looked for on the same
    site (or a site named in the capture, or at the named publisher), then anywhere, then with the headline's first words
    unquoted (a capture often cuts it). What was tried and why each was taken or refused goes into the conversation log."""
    if is_aggregator_name(publisher):
        publisher = None
    host = _site(wrong_url or "")
    if is_aggregator_host(host):
        host = ""
    clean_sites = [s for s in sites if s and not is_aggregator_host(s)]
    hosts = [h for h in [host, *clean_sites] if h]
    pub_host = _site("https://" + publisher) if publisher and "." in publisher else ""
    if is_aggregator_host(pub_host):
        pub_host = ""
    wanted = list(dict.fromkeys([*hosts, *([pub_host] if pub_host else [])]))
    words = title.split()
    queries = ([f'"{title}" site:{h}' for h in dict.fromkeys(hosts)] + ([f'"{title}" {publisher}'] if publisher else [])
               + [f'"{title}"'] + ([" ".join(words[:10]) + (f" {publisher}" if publisher else "")] if len(words) > 4 else []))
    tried: set[str] = set()
    trace: list[dict] = []
    checked: list[dict] = []

    async def check(hit: Hit, origin: str) -> str | None:
        if hit.url in tried or is_aggregator(hit.url):
            return None
        tried.add(hit.url)
        verdict, found = await judge(title, hit.url, hit.title, wanted, http)
        checked.append({"url": hit.url, "title": hit.title or None, "from": origin, "verdict": verdict})
        return found

    def report(found: str | None) -> None:
        convlog.event("link_search", title=title, found=found, model_sources=list(sources), queries=trace, checked=checked)

    # the pages Gemini's (or any model's) web search read: best first, those on the site we're after
    clean_sources = [u for u in sources if not is_aggregator(u)]
    ordered = sorted(dict.fromkeys(clean_sources), key=lambda u: not any(_site(u) == h or _site(u).endswith("." + h) for h in wanted))
    for url in ordered[:6]:
        hit_title = (source_titles or {}).get(url, "")
        if (found := await check(Hit(url, hit_title), "the model's web search")):
            report(found)
            return found
    for query in queries:
        for hit in await search_results(query, http, trace=trace):
            if (found := await check(hit, query)):
                report(found)
                return found
    report(None)
    return None


async def repair_link(analysis: dict, http: httpx.AsyncClient) -> dict:
    """The analysis with a working link: unchanged if the model's link opens and fits the title; otherwise the
    real article's address, or no link at all when it can't be found. Links you shared yourself are never touched."""
    url, title = analysis.get("canonical_url"), analysis.get("title")
    source_titles = dict(analysis.get("_source_titles") or {})
    sources = list(analysis.get("_sources") or ())
    for item in analysis.get("links") or []:
        u, lbl = item.get("url"), item.get("label")
        if u and u.startswith("http") and u not in sources and not is_aggregator(u):
            sources.append(u)
            if lbl:
                source_titles.setdefault(u, lbl)
    for item in analysis.get("related") or []:
        u, lbl = item.get("url"), item.get("label")
        if u and u.startswith("http") and u not in sources and not is_aggregator(u):
            sources.append(u)
            if lbl:
                source_titles.setdefault(u, lbl)

    if url and "vertexaisearch.cloud.google.com" in url:
        try:
            r = await http.get(url, follow_redirects=False, timeout=8)
            if r.is_redirect and (target := r.headers.get("location")):
                if target.startswith("http") and "google.com/grounding" not in target:
                    url = target
                    analysis = {**analysis, "canonical_url": url}
        except httpx.HTTPError:
            pass

    publisher = ((analysis.get("details") or {}).get("publisher") or "").strip() or None
    if is_aggregator_name(publisher):
        publisher = None

    if not url and title and analysis.get("category") == "article" and "link" not in (analysis.get("_analyzer") or []):
        # the model named the article but found no address for it: look it up
        found = await find_article_url(title, None, http, publisher, capture_sites(analysis), sources, source_titles=source_titles)
        return {**analysis, "canonical_url": found} if found else analysis

    if (not url or not title or analysis.get("category") not in REPAIRABLE or not url.startswith("http")):
        return analysis

    is_agg = is_aggregator(url)
    if "link" in (analysis.get("_analyzer") or []) and not is_agg:
        return analysis

    page = await fetch_page(url, http)

    if is_agg and analysis.get("category") == "article":
        wanted_hosts = [h for h in capture_sites(analysis) if not is_aggregator_host(h)]
        if publisher and "." in publisher and not is_aggregator_host(publisher):
            pub_host = _site("https://" + publisher)
            if pub_host and pub_host not in wanted_hosts and not is_aggregator_host(pub_host):
                wanted_hosts.append(pub_host)
        real = None
        if page:
            real = await _outbound_article_link(page, title, wanted_hosts, http)
        if not real:
            real = await find_article_url(title, None, http, publisher, capture_sites(analysis), sources, source_titles=source_titles)
        if real:
            log.info("Aggregator link %s for %r resolved to %s", url, title, real)
            links = list(analysis.get("links") or [])
            if not any(l.get("url") == url for l in links):
                label = "Shared link" if "link" in (analysis.get("_analyzer") or []) else "Original post"
                links.insert(0, {"label": label, "url": url})
            details = dict(analysis.get("details") or {})
            if not details.get("post_url"):
                details["post_url"] = url
            return {**analysis, "canonical_url": real, "links": links, "details": details}
        if page is not None or not link_is_gone(url):
            return {**analysis, "canonical_url": _clean(url)}

    if page and page_matches(title, page):
        cl = canonical_link(page)
        if not is_aggregator(cl):
            return {**analysis, "canonical_url": cl}
    if page is None and not link_is_gone(url):
        slug = _slug_words(url)
        if not slug or _share(title, slug) >= 0.6:
            return {**analysis, "canonical_url": _clean(url)}
    found = await find_article_url(title, url, http, publisher, capture_sites(analysis), sources, source_titles=source_titles)
    if found:
        log.info("Link %s for %r didn't match; using %s", url, title, found)
        return {**analysis, "canonical_url": found}
    if page is not None or not link_is_gone(url):
        # The address opens, so it isn't made up (or was blocked and couldn't be replaced): keep it rather than lose a working link;
        # only a dead one is dropped.
        return {**analysis, "canonical_url": _clean(url)}
    log.info("Link %s for %r is dead and no replacement was found", url, title)
    note = " The link the model suggested doesn't work, so none is shown."
    return {**analysis, "canonical_url": None,
            "confidence_reason": ((analysis.get("confidence_reason") or "") + note).strip()}
