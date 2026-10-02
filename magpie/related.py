"""Related links: things an item points to that are worth a look.

An article about a utility links its repository, the app's homepage, the paper or the company behind it.
Those come from three places: the model (what a screenshot or article mentions), the page itself (its
outbound links), and the sources Magpie already looks up (a model's paper, a repository's parent).
Each is {kind, label, url, why?}; the detail sheet shows them under "Related".
"""

import asyncio
import re
from urllib.parse import urlsplit

KINDS = ("repo", "package", "app", "paper", "company", "docs", "video", "reference", "site")
MAX_RELATED = 8
GITHUB_RESERVED = {"features", "topics", "sponsors", "orgs", "login", "join", "about", "pricing", "marketplace", "explore",
                   "collections", "trending", "settings", "notifications", "pulls", "issues", "search", "site", "readme", "customer-stories"}
# hosts whose links are chrome, sharing buttons or tracking, never "related"
NOISE_HOSTS = ("facebook.com", "twitter.com", "x.com", "linkedin.com", "pinterest.com", "reddit.com", "instagram.com", "t.me", "wa.me",
               "whatsapp.com", "flipboard.com", "tumblr.com", "doubleclick.net", "googletagmanager.com", "google.com", "googleadservices.com",
               "bing.com", "amazon.com", "amzn.to", "bit.ly", "t.co", "feedburner.com", "gravatar.com", "wp.com", "wordpress.com",
               "disqus.com", "mailchimp.com", "patreon.com", "paypal.com", "ko-fi.com", "buymeacoffee.com", "archive.org", "archive.ph")
_ANCHOR = re.compile(r"<a\b[^>]*?\bhref=[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _same_site(a: str, b: str) -> bool:
    ha, hb = _host(a).split("."), _host(b).split(".")
    return ha[-2:] == hb[-2:]


same_site = _same_site


def norm_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc.lower().removeprefix('www.')}{parts.path.rstrip('/')}"


def classify_url(url: str, text: str = "") -> dict | None:
    """What a link is, from its address (and anchor text for a plain site). None for chrome and noise."""
    parts = urlsplit(url)
    host, segs = _host(url), [s for s in parts.path.split("/") if s]
    if parts.scheme not in ("http", "https") or not host:
        return None
    if host == "github.com":
        if len(segs) >= 2 and segs[0].lower() not in GITHUB_RESERVED and not segs[1].startswith("."):
            full = f"{segs[0]}/{segs[1].removesuffix('.git')}"
            return {"kind": "repo", "label": full, "url": f"https://github.com/{full}"}
        return None
    if host.endswith("npmjs.com") and len(segs) >= 2 and segs[0] == "package":
        name = "/".join(segs[1:3]) if segs[1].startswith("@") and len(segs) > 2 else segs[1]
        return {"kind": "package", "label": name, "url": f"https://www.npmjs.com/package/{name}"}
    if host == "pypi.org" and len(segs) >= 2 and segs[0] == "project":
        return {"kind": "package", "label": segs[1], "url": f"https://pypi.org/project/{segs[1]}/"}
    if host == "crates.io" and len(segs) >= 2 and segs[0] == "crates":
        return {"kind": "package", "label": segs[1], "url": f"https://crates.io/crates/{segs[1]}"}
    if host.endswith("huggingface.co") and len(segs) >= 2 and segs[0] not in ("docs", "blog", "papers", "join", "login", "pricing"):
        return {"kind": "repo", "label": "/".join(segs[1:3] if segs[0] in ("datasets", "spaces") else segs[:2]), "url": url.split("?")[0]}
    if host in ("apps.apple.com", "itunes.apple.com") or (host == "play.google.com" and "apps" in segs) or host == "microsoftstore.com":
        return {"kind": "app", "label": (text or "App").strip()[:60] or "App", "url": url.split("?")[0]}
    if host == "arxiv.org" and len(segs) >= 2 and segs[0] in ("abs", "pdf"):
        return {"kind": "paper", "label": f"arXiv {segs[1].removesuffix('.pdf')}", "url": f"https://arxiv.org/abs/{segs[1].removesuffix('.pdf')}"}
    if host.endswith("wikipedia.org") and len(segs) >= 2 and segs[0] == "wiki" and ":" not in segs[1]:
        return {"kind": "reference", "label": segs[1].replace("_", " "), "url": url.split("#")[0]}
    if host in ("youtube.com", "youtu.be") and (parts.path == "/watch" or host == "youtu.be" or segs[:1] == ["shorts"]):
        return {"kind": "video", "label": (text or "Video").strip()[:60] or "Video", "url": url}
    if host in ("producthunt.com", "news.ycombinator.com") or host.startswith(("docs.", "developer.", "developers.")):
        return {"kind": "docs" if host.startswith(("docs.", "develop")) else "site", "label": (text or host).strip()[:60] or host, "url": url}
    if any(host == h or host.endswith("." + h) for h in NOISE_HOSTS):
        return None
    return {"kind": "site", "label": (text or host).strip()[:60] or host, "url": url}


def outbound_related(page, limit: int = MAX_RELATED) -> list[dict]:
    """The links in an article that lead to something worth a look: repositories, packages, apps, papers, and
    the homepages of projects and companies it names. Sharing buttons, same-site navigation and ads are dropped."""
    found: list[dict] = []
    seen: set[str] = set()
    for href, inner in _ANCHOR.findall(page.html or ""):
        text = re.sub(r"\s+", " ", _TAG.sub("", inner)).strip()
        url = href if href.startswith("http") else (f"https:{href}" if href.startswith("//") else None)
        if not url or _same_site(url, page.url):
            continue
        item = classify_url(url, text)
        if not item or norm_url(item["url"]) in seen:
            continue
        if item["kind"] == "site":
            # a plain site is worth listing only as a homepage-like link with a name for anchor text
            if len(urlsplit(url).path.strip("/").split("/")) > 1 or not (2 <= len(text) <= 40) or len(text.split()) > 4 or not text[:1].isupper():
                continue
        seen.add(norm_url(item["url"]))
        found.append(item)
    order = {k: i for i, k in enumerate(KINDS)}
    found.sort(key=lambda i: order.get(i["kind"], 99))   # stable: article order within a kind
    return found[:limit]


def clean_related(items, exclude: list[str | None] = (), limit: int = MAX_RELATED) -> list[dict]:
    """Normalize what a model or an enricher produced: drop malformed and duplicate links, and ones the item already
    shows elsewhere (its own address, its links)."""
    skip = {norm_url(u) for u in exclude if u}
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict) or not isinstance(it.get("url"), str) or not it["url"].startswith(("http://", "https://")):
            continue
        key = norm_url(it["url"])
        if key in skip:
            continue
        skip.add(key)
        kind = it.get("kind") if it.get("kind") in KINDS else (classify_url(it["url"]) or {"kind": "site"})["kind"]
        label = re.sub(r"\s+", " ", str(it.get("label") or "")).strip()[:80] or _host(it["url"])
        out.append({"kind": kind, "label": label, "url": it["url"].strip(), **({"why": str(it["why"])[:160]} if it.get("why") else {})})
    order = {k: i for i, k in enumerate(KINDS)}
    out.sort(key=lambda i: order.get(i["kind"], 99))
    return out[:limit]


async def drop_dead(items: list[dict], http, fetch_page, link_is_gone) -> list[dict]:
    """Keep the links that open (or that we simply can't check: blocked, slow); drop the ones that are gone, which is what
    a link a model made up looks like."""
    async def alive(it: dict) -> bool:
        page = await fetch_page(it["url"], http)
        return bool(page) or not link_is_gone(it["url"])
    verdicts = await asyncio.gather(*(alive(it) for it in items[:MAX_RELATED + 4]), return_exceptions=True)
    return [it for it, ok in zip(items, verdicts) if ok is True]


_MD_LINK = re.compile(r"(?<!!)\[([^\]]{1,80})\]\(\s*<?(https?://[^)\s>]+)")
_HTML_LINK = re.compile(r"<a\b[^>]*?\bhref=[\"'](https?://[^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
# README links worth listing even though they're plain sites: what the project points you to
_README_SITE_WORDS = re.compile(r"\b(website|homepage|home page|docs|documentation|demo|playground|try it|blog|discord|community|"
                                r"forum|paper|tutorial|guide|examples?|showcase|changelog|roadmap|app|download)\b", re.I)
_BADGE = re.compile(r"shields\.io|badge|/workflows/|codecov|travis-ci|circleci|coveralls|img\.|star-history|contrib\.rocks", re.I)


def readme_related(markdown: str, full_name: str, exclude: list[str | None] = (), limit: int = 6) -> list[dict]:
    """What a repository's README points to: its package (npm, PyPI, crates), docs and demo, the paper, the app
    in the stores, the community. Badges, the repo's own pages and its contributors are left out."""
    if not markdown:
        return []
    own = f"github.com/{full_name}".lower()
    pairs = [(u, t) for t, u in _MD_LINK.findall(markdown)] + [(u, _TAG.sub("", t)) for u, t in _HTML_LINK.findall(markdown)]
    skip = {norm_url(u) for u in exclude if u}
    found: list[dict] = []
    for url, text in pairs:
        url, text = url.strip().rstrip(".,;"), re.sub(r"\s+", " ", text).strip()
        if _BADGE.search(url) or own in url.lower() or norm_url(url) in skip:
            continue
        item = classify_url(url, text)
        if not item:
            continue
        if item["kind"] == "repo" and _host(url) == "github.com":
            continue   # other repositories a README lists are usually dependencies or "see also"; too noisy
        if item["kind"] == "site" and not _README_SITE_WORDS.search(text):
            continue
        skip.add(norm_url(item["url"]))
        found.append({**item, "why": "linked from the README"})
        if len(found) >= limit:
            break
    return found


_TEXT_URL = re.compile(r"(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|app|co|so|sh|me|news|blog|tech|xyz|"
                       r"gg|tv|fm|info|edu|gov|uk|de|fr|il|ca|au|in|us)(?:/[^\s<>\"')\]]*)?)", re.I)


def text_related(text: str, limit: int = 6) -> list[dict]:
    """Links written in a screenshot (or a shared post): addresses, bare domains and github.com/owner/repo, each as a
    related link with what kind of thing it is. Social sites and the like are left out (classify_url)."""
    found: list[dict] = []
    seen: set[str] = set()
    for m in _TEXT_URL.finditer(text or ""):
        raw = m.group(0)
        url = raw if raw.lower().startswith("http") else "https://" + m.group(1)
        url = url.rstrip(".,;:!?")
        item = classify_url(url, "")
        if not item or norm_url(item["url"]) in seen:
            continue
        if item["kind"] == "site":
            item = {**item, "label": _host(url) + (urlsplit(url).path.rstrip("/") if len(urlsplit(url).path) > 1 else "")}
        seen.add(norm_url(item["url"]))
        found.append({**item, "why": "mentioned in the capture"})
        if len(found) >= limit:
            break
    return found
