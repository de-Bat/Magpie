"""Finding a good picture for an item: poster, cover, header or logo.

Pages and repositories offer several candidate images (OpenGraph, structured data, the README's
header, the site's icon). Some are dead links, tracking pixels, badges or tiny icons. We check
the candidates in order of preference and use the first one that is a real picture.
"""

import io
import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from .fetch import BlockedURL, safe_get

log = logging.getLogger(__name__)

MIN_SIDE = 120          # px: anything smaller is an icon or a tracking pixel
MAX_ASPECT = 5.0        # wider or taller than this is a divider, not a picture
CHECK_BYTES = 98_304    # enough to read the size of a PNG, JPEG, WebP or GIF
MAX_CHECKS = 5          # candidates we're willing to fetch per item
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_CACHE: dict[str, tuple[float, bool | None]] = {}
_CACHE_SECONDS = 3600

# Never a picture of the thing, wherever it appears. Kept narrow: an article about a Pixel phone or a
# badge collection still gets its photo.
_NOT_A_PICTURE = re.compile(r"\.svg(\?|#|$)|/spacer\.|/blank\.(gif|png)|\b1x1\.|/pixel\.(gif|png)|tracking[-_]?pixel|"
                            r"/ads?/|doubleclick\.net|/sprites?[./]|gravatar\.com/avatar/[0-9a-f]{32}\?.*d=blank", re.I)
# README-only noise: status badges, sponsor buttons, contributor walls, video thumbnails.
_README_NOISE = re.compile(r"shields\.io|badge|travis-ci|codecov|coveralls|star-history|contrib\.rocks|visitor|"
                           r"buymeacoffee|ko-fi|opencollective|/workflows/|gh-dark-mode-only|emoji|api\.star-history|"
                           r"github\.com/sponsors|img\.youtube\.com|/donate|repobeats|contributors-img|sonarcloud|snyk\.io|"
                           r"fossa\.com|codeclimate|deepsource|app\.netlify\.com/.*/deploy|badgen\.net|npm\.im|nodei\.co|"
                           r"packagephobia|bundlephobia\.com/api|img\.badgesize|codefactor|circleci\.com|ci\.appveyor", re.I)
# Site-wide share images: used for every page on the site, so a real picture of the article should beat them.
_GENERIC_SHARE = re.compile(r"(default|placeholder|fallback|generic|site[-_]?(image|share|og)|share[-_]?default|"
                            r"og[-_]?default|social[-_]?default|no[-_]?image|logo)[^/]*\.(png|jpe?g|webp|gif)(\?|$)", re.I)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return m.group(1) if m else None


def looks_usable(url: str) -> bool:
    """Cheap check on the URL alone: not an SVG, tracker or ad."""
    return bool(url) and url.startswith(("http://", "https://")) and not _NOT_A_PICTURE.search(url)


def github_raw(url: str) -> str:
    """github.com/o/r/blob/<ref>/path is an HTML page showing the file; its raw address is the picture itself."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/(?:blob|raw)/(.+?)(\?raw=(true|1))?$", url)
    return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else url


async def verify_image(http: httpx.AsyncClient, url: str) -> bool | None:
    """True: a real picture of a usable size. False: dead, not an image, or too small.
    None: couldn't tell (the host refuses server requests or timed out); browsers may still load it."""
    hit = _CACHE.get(url)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    result = await _verify(http, url)
    if len(_CACHE) > 500:
        _CACHE.clear()
    _CACHE[url] = (time.monotonic(), result)
    return result


async def _verify(http: httpx.AsyncClient, url: str) -> bool | None:
    try:
        r = await safe_get(http, url, headers={"User-Agent": BROWSER_UA, "Accept": "image/*,*/*;q=0.5",
                                                "Range": f"bytes=0-{CHECK_BYTES - 1}"}, timeout=8)
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Couldn't check image %s: %s", url, e)
        return None
    if r.status_code in (401, 403, 429) or r.status_code >= 500:
        return None
    if r.status_code not in (200, 206):
        return False
    data = r.content[:CHECK_BYTES]
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "image/svg+xml" or data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:300]:
        return False
    if not ctype.startswith("image/") and data[:4] not in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"RIFF", b"GIF8"):
        return False
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:  # Pillow missing, or a truncated progressive file: the content type will have to do
        return True
    return min(w, h) >= MIN_SIDE and max(w, h) / max(1, min(w, h)) <= MAX_ASPECT


PROBE_BYTES = 1_500_000


async def picture_is_opaque(http: httpx.AsyncClient, url: str) -> bool | None:
    """Whether a cover picture has no transparent areas, so the card can show it whole over a blurred copy of itself.
    A logo with a transparent background looks wrong that way and keeps the plain themed cover. None: couldn't tell."""
    try:
        r = await safe_get(http, url, headers={"User-Agent": BROWSER_UA, "Accept": "image/*,*/*;q=0.5",
                                                "Range": f"bytes=0-{PROBE_BYTES - 1}"}, timeout=8)
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Couldn't probe image %s: %s", url, e)
        return None
    if r.status_code not in (200, 206):
        return None
    try:
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True   # the top of a large picture is enough to see its transparency
        with Image.open(io.BytesIO(r.content[:PROBE_BYTES])) as im:
            if im.format == "JPEG":
                return True
            if im.mode in ("RGBA", "LA", "PA"):
                small = im.convert("RGBA")
                small.thumbnail((64, 64))
                return small.getchannel("A").getextrema()[0] >= 250
            return "transparency" not in im.info   # a palette or colour-key PNG/GIF with a transparent entry
    except Exception as e:   # Pillow missing, or not a picture it can read
        log.info("Couldn't read image %s: %s", url, e)
        return None


async def best_image(http: httpx.AsyncClient, candidates: Iterable[str | None], base: str | None = None,
                     last_resort: str | None = None, verified_only: bool = False) -> str | None:
    """The first candidate that is a real picture. When none could be checked, the first that
    couldn't be ruled out (unless `verified_only`); `last_resort` (e.g. a generated card) is used without a check."""
    seen: list[str] = []
    for c in candidates:
        if not c or not isinstance(c, str):
            continue
        url = urljoin(base, c.strip()) if base else c.strip()
        if looks_usable(url) and url not in seen:
            seen.append(url)
    unknown = None
    for url in seen[:MAX_CHECKS]:
        verdict = await verify_image(http, url)
        if verdict:
            return url
        if verdict is None and unknown is None:
            unknown = url
    return (None if verified_only else unknown) or last_resort


def _best_srcset(value: str | None) -> str | None:
    """The largest picture in a srcset ("a.jpg 480w, b.jpg 1200w")."""
    best, best_w = None, -1
    for part in (value or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        m = re.match(r"(\d+(?:\.\d+)?)[wx]", bits[1]) if len(bits) > 1 else None
        w = float(m.group(1)) if m else 0
        if w > best_w:
            best, best_w = bits[0], w
    return best


def youtube_thumbnails(url: str | None) -> list[str]:
    """The video's own thumbnail, without loading the page (YouTube often answers servers with a consent wall)."""
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})", url or "")
    return [f"https://i.ytimg.com/vi/{m.group(1)}/maxresdefault.jpg", f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg"] if m else []


async def oembed_thumbnail(http: httpx.AsyncClient, page: Any) -> str | None:
    """The thumbnail an oEmbed endpoint advertises on the page (video, audio and social sites publish one)."""
    for l in getattr(page, "links", []):
        if "oembed" in (l.get("type") or "").lower() and "json" in (l.get("type") or "").lower():
            try:
                r = await safe_get(http, urljoin(page.url, l["href"]), headers={"User-Agent": BROWSER_UA}, timeout=8)
                thumb = r.json().get("thumbnail_url") if r.status_code == 200 else None
            except (httpx.HTTPError, BlockedURL, ValueError, AttributeError):
                return None
            return thumb if isinstance(thumb, str) else None
    return None


_LOGO_FILE = re.compile(r"(^|/)(logo|icon|banner|hero|header|social[-_]?preview|og[-_]?image)[^/]*\.(png|jpe?g|webp|gif)$", re.I)


async def package_logos(http: httpx.AsyncClient, name: str, version: str | None) -> list[str]:
    """Logo-like files shipped inside an npm package (jsDelivr lists a package's files), shallowest first."""
    if not version:
        return []
    try:
        r = await http.get(f"https://data.jsdelivr.com/v1/package/npm/{name}@{version}/flat", timeout=8)
        files = [f.get("name", "") for f in r.json().get("files", [])] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, AttributeError):
        return []
    hits = sorted((f for f in files if _LOGO_FILE.search(f) and "node_modules" not in f and "/test" not in f), key=lambda f: (f.count("/"), len(f)))
    return [f"https://cdn.jsdelivr.net/npm/{name}@{version}{f}" for f in hits[:3]]


def origin_icons(url: str) -> list[str]:
    """The site's logo at addresses that work without loading its pages (for sites that block us):
    the touch icon at its conventional address, then Google's copy of its icon at the largest size."""
    parts = urlsplit(url)
    if not parts.netloc:
        return []
    return [f"{parts.scheme}://{parts.netloc}/apple-touch-icon.png",
            f"https://www.google.com/s2/favicons?domain={parts.hostname}&sz=256"]


def _ld_images(node: dict) -> list[str]:
    out: list[str] = []

    def add(v: Any) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for x in v:
                add(x)
        elif isinstance(v, dict):
            add(v.get("url") or v.get("contentUrl"))

    types = {t.lower() for t in (node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]) if isinstance(t, str)}
    if types & {"organization", "newsmediaorganization", "corporation", "website", "person", "brand"}:
        return out   # the publisher's logo or portrait, not a picture of this page
    for key in ("image", "thumbnailUrl", "primaryImageOfPage", "thumbnail"):
        add(node.get(key))
    return out


def _ld_logos(node: dict) -> list[str]:
    """The publisher's logo from structured data (Organization.logo, Article.publisher.logo)."""
    out: list[str] = []
    for holder in (node, node.get("publisher") if isinstance(node.get("publisher"), dict) else None):
        logo = (holder or {}).get("logo")
        if isinstance(logo, list):
            logo = logo[0] if logo else None
        if isinstance(logo, dict):
            logo = logo.get("url") or logo.get("contentUrl")
        if isinstance(logo, str):
            out.append(logo)
    return out


_HEROISH = re.compile(r"\b(hero|featured|feature[-_]?image|lead[-_]?(image|media|art)|header[-_]?image|post[-_]?image|article[-_]?image|"
                      r"main[-_]?image|cover[-_]?image|wp-post-image|entry[-_]?image|story[-_]?image|top[-_]?image|masthead[-_]?image)", re.I)


def header_images(html: str, limit: int = 3) -> list[str]:
    """The article's header picture as the page itself marks it: preloaded for the first paint, fetched with high
    priority, or tagged as the hero / featured / lead image. Sites without a share image usually still do one of these."""
    found: list[str] = []
    head = (html or "")[:200_000]
    for tag in re.findall(r"<link\b[^>]*>", head, re.I):
        if (_attr(tag, "rel") or "").lower() == "preload" and (_attr(tag, "as") or "").lower() == "image":
            src = _best_srcset(_attr(tag, "imagesrcset")) or _attr(tag, "href")
            if src and not re.search(r"logo|icon|sprite|avatar", src, re.I):
                found.append(src)
    for tag in _IMG_TAG.findall(head):
        marked = (_attr(tag, "fetchpriority") or "").lower() == "high" or _HEROISH.search(
            " ".join(filter(None, [_attr(tag, "class"), _attr(tag, "id"), _attr(tag, "data-component")])))
        if not marked:
            continue
        src = (_best_srcset(_attr(tag, "data-srcset") or _attr(tag, "srcset")) or _attr(tag, "data-src")
               or _attr(tag, "data-lazy-src") or _attr(tag, "src"))
        if src and not src.startswith("data:") and not re.search(r"logo|icon|avatar|author", src, re.I):
            found.append(src)
    out: list[str] = []
    for u in found:
        if u not in out:
            out.append(u)
    return out[:limit]


def content_images(html: str, limit: int = 3) -> list[str]:
    """The first few pictures in the page body that look like content, not chrome."""
    found: list[str] = []
    for tag in _IMG_TAG.findall(html or ""):
        src = (_best_srcset(_attr(tag, "data-srcset") or _attr(tag, "srcset")) or _attr(tag, "data-src")
               or _attr(tag, "data-lazy-src") or _attr(tag, "data-original") or _attr(tag, "src"))
        if not src or src.startswith("data:") or not looks_usable(urljoin("https://x/", src)):
            continue
        if re.search(r"logo|icon|avatar|profile|author|thumb-?small|emoji|badge", src, re.I):
            continue
        try:
            if any(int(_attr(tag, d) or 999) < 150 for d in ("width", "height")):
                continue
        except ValueError:
            pass
        found.append(src)
        if len(found) >= limit:
            break
    return found


def page_pictures(page: Any, extra: Iterable[str | None] = ()) -> list[str]:
    """Pictures of the page itself, best first: its share image, structured data, the header picture the page
    marks as such, the lead picture, the first content pictures. Site-wide default share images go last."""
    m = page.meta
    out: list[str | None] = [m.get(k) for k in ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src", "image", "thumbnail")]
    for node in page.ld:
        out += _ld_images(node)
    out += [l["href"] for l in getattr(page, "links", []) if {"image_src", "image"} & set(l["rel"].split())]
    out += header_images(page.html)
    out += list(extra)
    out += content_images(page.html)
    specific = [u for u in out if u and not _GENERIC_SHARE.search(u)]
    out = specific + [u for u in out if u and _GENERIC_SHARE.search(u)]
    return _unique(urljoin(page.url, u) for u in out if u)


def site_logos(page: Any) -> list[str]:
    """The site's logo, largest first: the publisher logo from structured data, the declared touch and app icons
    (by size), then the conventional addresses."""
    out: list[str] = []
    for node in page.ld:
        out += _ld_logos(node)
    out += [page.meta[k] for k in ("msapplication-TileImage", "msapplication-square310x310logo") if page.meta.get(k)]

    def side(l: dict) -> int:
        m = re.match(r"\d+", l.get("sizes") or "")
        return int(m.group(0)) if m else (180 if "apple-touch-icon" in l["rel"] else 0)

    icons = [l for l in getattr(page, "links", []) if "apple-touch-icon" in l["rel"]
             or ("icon" in l["rel"].split() and side(l) >= 120)]
    out += [l["href"] for l in sorted(icons, key=side, reverse=True)]
    out = [urljoin(page.url, u) for u in out if u]
    return _unique([*out, *origin_icons(page.url)])


async def manifest_icons(http: httpx.AsyncClient, page: Any) -> list[str]:
    """The largest icons in the site's web app manifest (PWAs declare 192 and 512 px ones)."""
    link = next((l for l in getattr(page, "links", []) if "manifest" in l["rel"].split()), None)
    if not link:
        return []
    url = urljoin(page.url, link["href"])
    try:
        r = await safe_get(http, url, headers={"User-Agent": BROWSER_UA}, timeout=8)
        icons = r.json().get("icons") or [] if r.status_code == 200 else []
    except (httpx.HTTPError, BlockedURL, ValueError, AttributeError):
        return []

    def side(i: dict) -> int:
        return max((int(x) for x in re.findall(r"(\d+)x\d+", str(i.get("sizes") or ""))), default=0)

    usable = [i for i in icons if isinstance(i, dict) and i.get("src") and side(i) >= 120 and "monochrome" not in str(i.get("purpose") or "")]
    return [urljoin(url, i["src"]) for i in sorted(usable, key=side, reverse=True)[:2]]


def page_image_candidates(page: Any, extra: Iterable[str | None] = ()) -> list[str]:
    """Every picture a web page offers for itself, best first, and last the site's logo."""
    return _unique([*page_pictures(page, extra), *site_logos(page)])


def _unique(urls: Iterable[str]) -> list[str]:
    out: list[str] = []
    for u in urls:
        if u and u not in out:
            out.append(u)
    return out


def _resolve_readme_src(src: str, full_name: str, base: str | None = None) -> str | None:
    src = (src or "").strip()
    if not src or src.startswith("data:"):
        return None
    if src.startswith("//"):
        src = "https:" + src
    elif not src.startswith("http"):
        if base:
            src = urljoin(base if base.endswith("/") else base + "/", src.removeprefix("./"))
        elif full_name:
            src = f"https://raw.githubusercontent.com/{full_name}/HEAD/{src.removeprefix('./').lstrip('/')}"
        else:
            return None
    src = github_raw(src)
    return src if looks_usable(src) and not _README_NOISE.search(src) else None


_README_IMG = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)|<img\b[^>]*?\bsrc=(?:["\']([^"\']+)["\']|([^\s>"\']+))', re.I)
_LOGO_NAME_RE = re.compile(r"(?:^|[_\-/.])(logo|icon|brand|logomark|wordmark|symbol|mascot)(?:[_\-/.]|$)", re.I)
_LOGO_ATTR_RE = re.compile(r"\b(logo|icon|brand|logomark|wordmark|symbol|mascot)\b", re.I)
_MD_IMG_FULL = re.compile(r'!\[(?P<alt>[^\]]*)\]\(\s*<?(?P<src>[^)\s>]+)(?:\s+(?:["\'](?P<title>[^"\']*)["\']|\((?P<title2>[^)]*)\)))?', re.I)
_CENTER_BLOCK = re.compile(r'<(?:p|div|h1)\b[^>]*align=["\']?center["\']?[^>]*>(.*?)</(?:p|div|h1)>', re.I | re.S)


def readme_images(markdown: str, full_name: str, limit: int = 3, base: str | None = None) -> list[str]:
    """The first real pictures near the top of a README (its header or logo), skipping badges and SVGs
    (which the mobile app can't draw). Relative paths are resolved against `base` (e.g. a package's files on a
    CDN) or, by default, the GitHub repository."""
    found: list[str] = []
    for m in _README_IMG.finditer((markdown or "")[:8000]):
        raw = m.group(1) or m.group(2) or m.group(3) or ""
        src = _resolve_readme_src(raw, full_name, base)
        if src and src not in found:
            found.append(src)
        if len(found) >= limit:
            break
    return found


def readme_logos(markdown: str, full_name: str, limit: int = 3, base: str | None = None) -> list[str]:
    """Candidate logos found near the top of a repository README: images whose filename, alt text, or
    class declares them as a logo, icon or brand, or a hero emblem centered at the top."""
    text = (markdown or "")[:10000]
    if not text:
        return []

    # Find images located inside a centered block near the top of the README (<2500 chars)
    centered_srcs: set[str] = set()
    for block in _CENTER_BLOCK.finditer(text[:2500]):
        for im in _README_IMG.finditer(block.group(1)):
            raw = im.group(1) or im.group(2) or im.group(3) or ""
            resolved = _resolve_readme_src(raw, full_name, base)
            if resolved:
                centered_srcs.add(resolved)

    explicit_logos: list[str] = []
    top_centered: list[str] = []

    # 1. HTML <img> tags
    for tag_match in re.finditer(r'<img\b(?P<attrs>[^>]*?)>', text, re.I):
        attrs = tag_match.group("attrs")
        raw = (_best_srcset(_attr(attrs, "data-srcset") or _attr(attrs, "srcset")) or
               _attr(attrs, "src") or _attr(attrs, "data-src") or _attr(attrs, "data-original"))
        if not raw:
            continue
        src = _resolve_readme_src(raw, full_name, base)
        if not src:
            continue
        alt = _attr(attrs, "alt") or ""
        title = _attr(attrs, "title") or ""
        cls = _attr(attrs, "class") or ""
        is_explicit = bool(_LOGO_NAME_RE.search(src) or _LOGO_ATTR_RE.search(f"{alt} {title} {cls}"))
        if is_explicit and src not in explicit_logos:
            explicit_logos.append(src)
        elif src in centered_srcs and src not in top_centered and not re.search(r"screenshot|diagram|demo", f"{src} {alt}", re.I):
            top_centered.append(src)

    # 2. Markdown images ![alt](url)
    for m in _MD_IMG_FULL.finditer(text):
        raw = m.group("src") or ""
        src = _resolve_readme_src(raw, full_name, base)
        if not src:
            continue
        alt = m.group("alt") or ""
        title = m.group("title") or m.group("title2") or ""
        is_explicit = bool(_LOGO_NAME_RE.search(src) or _LOGO_ATTR_RE.search(f"{alt} {title}"))
        if is_explicit and src not in explicit_logos:
            explicit_logos.append(src)
        elif src in centered_srcs and src not in top_centered and not re.search(r"screenshot|diagram|demo", f"{src} {alt}", re.I):
            top_centered.append(src)

    out: list[str] = []
    for s in [*explicit_logos, *top_centered]:
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out

