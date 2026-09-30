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
    """The site's touch icon at its conventional address, for pages that block us or don't declare one."""
    parts = urlsplit(url)
    return [f"{parts.scheme}://{parts.netloc}/apple-touch-icon.png"] if parts.netloc else []


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

    for key in ("image", "thumbnailUrl", "primaryImageOfPage", "thumbnail", "logo"):
        add(node.get(key))
    return out


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


def page_image_candidates(page: Any, extra: Iterable[str | None] = ()) -> list[str]:
    """Every picture a web page offers for itself, best first: its own share image, structured data,
    the lead picture, the first content pictures, and last the site's icon."""
    m = page.meta
    out: list[str | None] = [m.get(k) for k in ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src", "image", "thumbnail")]
    for node in page.ld:
        out += _ld_images(node)
    out += [l["href"] for l in getattr(page, "links", []) if {"image_src", "image"} & set(l["rel"].split())]
    out += list(extra)
    out += content_images(page.html)
    # A site-wide default share image (default-og.png, the site logo, ...) goes behind the article's own pictures.
    specific = [u for u in out if u and not _GENERIC_SHARE.search(u)]
    out = specific + [u for u in out if u and _GENERIC_SHARE.search(u)]
    def side(l: dict) -> int:
        m = re.match(r"\d+", l.get("sizes") or "")
        return int(m.group(0)) if m else 0

    icons = sorted((l for l in getattr(page, "links", []) if "apple-touch-icon" in l["rel"]), key=side, reverse=True)
    out += [l["href"] for l in icons]
    return [urljoin(page.url, u) for u in out if u]


_README_IMG = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)|<img\b[^>]*?\bsrc=(?:["\']([^"\']+)["\']|([^\s>"\']+))', re.I)


def readme_images(markdown: str, full_name: str, limit: int = 3, base: str | None = None) -> list[str]:
    """The first real pictures near the top of a README (its header or logo), skipping badges and SVGs
    (which the mobile app can't draw). Relative paths are resolved against `base` (e.g. a package's files on a
    CDN) or, by default, the GitHub repository."""
    found: list[str] = []
    for m in _README_IMG.finditer((markdown or "")[:8000]):
        src = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http"):
            if base:
                src = urljoin(base if base.endswith("/") else base + "/", src.removeprefix("./"))
            elif full_name:
                src = f"https://raw.githubusercontent.com/{full_name}/HEAD/{src.removeprefix('./').lstrip('/')}"
            else:
                continue  # a relative path we can't resolve
        src = github_raw(src)
        if looks_usable(src) and not _README_NOISE.search(src) and src not in found:
            found.append(src)
        if len(found) >= limit:
            break
    return found
