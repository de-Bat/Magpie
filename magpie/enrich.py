"""Type-specific metadata enrichment from public sources.

Each enricher gets the analysis produced by Claude and returns an `Enrichment` patch.
They are best-effort: a missing API key or a failed request just means less metadata.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from . import readability
from .config import Settings
from .fetch import MAX_BYTES, BlockedURL, safe_get
from .related import outbound_related, readme_related, same_site, text_related
from .images import (best_image, manifest_icons, oembed_thumbnail, origin_icons, package_logos, page_image_candidates,
                     page_pictures, readme_images, site_logos, verify_image, youtube_thumbnails)

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TMDB_IMG = "https://image.tmdb.org/t/p/w500"


@dataclass
class Enrichment:
    metadata: dict[str, Any] = field(default_factory=dict)
    canonical_url: str | None = None
    image_url: str | None = None
    summary: str | None = None
    subtitle: str | None = None
    links: list[dict] = field(default_factory=list)
    related: list[dict] = field(default_factory=list)   # worth-a-look links: [{kind, label, url}], see related.py
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    # Name of the thing the source matched; used to double-check non-Claude identifications.
    matched_title: str | None = None
    image_kind: str | None = None   # "logo" when the picture is the site's logo rather than one of the thing itself


# ---------------------------------------------------------------------------
# GitHub


GITHUB_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.I)


def github_full_name(analysis: dict) -> str | None:
    details = analysis.get("details") or {}
    candidates = [details.get("github_full_name"), analysis.get("canonical_url"), analysis.get("title")]
    candidates += [l.get("url") for l in analysis.get("links") or []]
    for c in candidates:
        if not c:
            continue
        m = GITHUB_RE.search(c)
        if m:
            return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
        if re.fullmatch(r"[\w.-]+/[\w.-]+", c.strip()):
            return c.strip()
    # a github.com address written in the screenshot itself
    for text in (analysis.get("screenshot_text"), analysis.get("_ocr_text")):
        m = GITHUB_RE.search(text or "")
        if m and m.group(1).lower() not in ("features", "topics", "sponsors", "orgs", "login", "about", "marketplace"):
            return f"{m.group(1)}/{m.group(2).removesuffix('.git').rstrip('.,;:)')}"
    return None


def _repo_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _repo_names(analysis: dict, guess: str | None) -> list[str]:
    """Names to search GitHub for: the repo part of a wrong owner/repo, and the title (without an owner prefix
    or a tagline after a colon or dash)."""
    out: list[str] = []
    title = (analysis.get("title") or "").strip()
    for c in (guess.split("/")[-1] if guess else None, title.split("/")[-1] if "/" in title else None,
              re.split(r"\s[-–—:|]\s|:\s", title)[0] if title else None):
        c = (c or "").strip().strip(".")
        if 2 <= len(c) <= 100 and len(c.split()) <= 4 and c not in out:
            out.append(c)
    return out


async def find_repo(analysis: dict, guess: str | None, headers: dict, http: httpx.AsyncClient) -> str | None:
    """The repository a capture is about when its owner/repo isn't known or doesn't exist (a model often gets the
    name right and the owner wrong): GitHub's repository search by name. Only an exact name match is taken, the
    named owner's first, else the most starred."""
    owner = guess.split("/")[0] if guess and "/" in guess else None
    for name in _repo_names(analysis, guess):
        wanted = _repo_key(name)
        queries = ([f"{name} in:name user:{owner}"] if owner else []) + [f"{name} in:name"]
        for q in queries:
            try:
                r = await http.get("https://api.github.com/search/repositories", headers=headers,
                                   params={"q": q, "per_page": 10}, timeout=10)
                items = r.json().get("items") or [] if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError, AttributeError):
                items = []
            exact = [i for i in items if isinstance(i, dict) and _repo_key(i.get("name")) == wanted]
            if exact:
                exact.sort(key=lambda i: ((i.get("owner") or {}).get("login", "").lower() == (owner or "").lower(),
                                          not i.get("fork"), i.get("stargazers_count") or 0), reverse=True)
                return exact[0]["full_name"]
    return None


async def enrich_github(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    guess = github_full_name(analysis)
    if not guess and analysis.get("category") != "github_repo":
        return None
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    r = await http.get(f"https://api.github.com/repos/{guess}", headers=headers) if guess else None
    found_by_search = False
    if r is None or r.status_code == 404:
        found = await find_repo(analysis, guess, headers, http)
        if found:
            r, found_by_search = await http.get(f"https://api.github.com/repos/{found}", headers=headers), True
    if r is None or r.status_code != 200:
        log.info("GitHub lookup for %s failed: %s", guess or analysis.get("title"), r.status_code if r is not None else "no repo name")
        return None
    repo = r.json()
    full_name = repo["full_name"]
    try:
        rr = await http.get(f"https://api.github.com/repos/{full_name}/readme", headers={**headers, "Accept": "application/vnd.github.raw+json"})
        readme = rr.text if rr.status_code == 200 else ""
    except httpx.HTTPError:
        readme = ""
    hero = await github_image(full_name, headers, http, readme)
    release = await _latest_release(full_name, headers, http)
    meta = {
        "github_full_name": full_name,
        "stars": repo.get("stargazers_count"),
        "forks": repo.get("forks_count"),
        "watchers": repo.get("subscribers_count"),
        "open_issues": repo.get("open_issues_count"),
        "programming_language": repo.get("language"),
        "topics": repo.get("topics") or [],
        "license": (repo.get("license") or {}).get("spdx_id"),
        "latest_release": (release or {}).get("tag_name"),
        "released": ((release or {}).get("published_at") or "")[:10] or None,
        "created": (repo.get("created_at") or "")[:10] or None,
        "last_push": repo.get("pushed_at"),
        "archived": repo.get("archived"),
        "homepage": repo.get("homepage") or None,
    }
    if found_by_search:
        meta["found_by"] = "GitHub search" + (f" (the capture named {guess})" if guess else "")
    links = [{"label": "Homepage", "url": repo["homepage"]}] if repo.get("homepage") else []
    if release and release.get("html_url"):
        links.append({"label": f"Release {release.get('tag_name') or ''}".strip(), "url": release["html_url"]})
    tags = list((repo.get("topics") or [])[:6])
    if repo.get("language"):
        tags.append(repo["language"])
    related = []
    if (repo.get("owner") or {}).get("type") == "Organization":
        related.append({"kind": "company", "label": f"{repo['owner']['login']} on GitHub", "url": repo["owner"]["html_url"]})
    if isinstance(repo.get("parent"), dict) and repo["parent"].get("html_url"):
        related.append({"kind": "repo", "label": f"{repo['parent']['full_name']} (forked from)", "url": repo["parent"]["html_url"]})
    related += readme_related(readme, full_name, exclude=[repo.get("homepage")])   # its package, docs, paper, demo, community
    return Enrichment(
        metadata=meta, related=related,
        canonical_url=repo["html_url"],
        image_url=hero,
        subtitle=repo.get("description"),
        links=links,
        tags=tags,
        source="github",
        matched_title=full_name,
    )


async def _latest_release(full_name: str, headers: dict, http: httpx.AsyncClient) -> dict | None:
    try:
        r = await http.get(f"https://api.github.com/repos/{full_name}/releases/latest", headers=headers, timeout=8)
        return r.json() if r.status_code == 200 and isinstance(r.json(), dict) else None
    except (httpx.HTTPError, ValueError):
        return None


async def github_image(full_name: str, headers: dict, http: httpx.AsyncClient, readme: str | None = None) -> str:
    """The best picture for a repository: the maintainers' own social preview if they uploaded one, else the
    header or logo from the README, else GitHub's generated card (always available)."""
    candidates: list[str] = []
    page = await fetch_page(f"https://github.com/{full_name}", http)
    og = (page.meta.get("og:image") or "") if page else ""
    if "repository-images.githubusercontent.com" in og:  # uploaded by the maintainers; the generated cards are opengraph.githubassets.com
        candidates.append(og)
    if readme is None:
        try:
            r = await http.get(f"https://api.github.com/repos/{full_name}/readme", headers={**headers, "Accept": "application/vnd.github.raw+json"})
            readme = r.text if r.status_code == 200 else ""
        except httpx.HTTPError:
            readme = ""
    candidates += readme_images(readme, full_name)
    return await best_image(http, candidates, last_resort=f"https://opengraph.githubassets.com/1/{full_name}")


# ---------------------------------------------------------------------------
# npm packages


NPM_RE = re.compile(r"npmjs\.com/package/((?:@[\w.~-]+/)?[\w.~-]+)", re.I)


def npm_package(analysis: dict) -> str | None:
    for c in [analysis.get("canonical_url"), *[l.get("url") for l in analysis.get("links") or []]]:
        m = NPM_RE.search(c or "")
        if m:
            return m.group(1)
    return None


async def enrich_npm(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    """An npm package: facts from the registry and a picture from its repository, README or homepage."""
    name = npm_package(analysis)
    if not name:
        return None
    r = await http.get(f"https://registry.npmjs.org/{name.replace('/', '%2F')}")
    if r.status_code != 200:
        return None
    d = r.json()
    latest = (d.get("dist-tags") or {}).get("latest")
    v = (d.get("versions") or {}).get(latest) or {}
    repo = d.get("repository") or v.get("repository") or {}
    repo_url = repo.get("url") if isinstance(repo, dict) else repo
    gh = GITHUB_RE.search(repo_url or "")
    full_name = f"{gh.group(1)}/{gh.group(2).removesuffix('.git')}" if gh else None
    homepage = d.get("homepage") or v.get("homepage")
    downloads = None
    try:
        dl = await http.get(f"https://api.npmjs.org/downloads/point/last-week/{name}")
        downloads = dl.json().get("downloads") if dl.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        pass
    meta = {
        "package_name": name, "version": latest, "license": v.get("license") or d.get("license"),
        "weekly_downloads": downloads, "last_publish": (d.get("time") or {}).get(latest),
        "maintainers": [m.get("name") for m in d.get("maintainers") or [] if m.get("name")][:4],
        "github_full_name": full_name, "homepage": homepage if isinstance(homepage, str) else None,
    }
    # Picture: the repo's own preview or header, else the README on npm, else the homepage's share image.
    candidates: list[str] = []
    generic_card = None
    if full_name:
        image = await github_image(full_name, {"Accept": "application/vnd.github+json"}, http)
        if "opengraph.githubassets.com" in image:
            generic_card = image
        else:
            candidates.append(image)
    if isinstance(d.get("readme"), str):
        # The package's README refers to files relative to the package: in a monorepo that is its folder in the
        # repository, otherwise the package's own files (served by jsDelivr).
        directory = repo.get("directory") if isinstance(repo, dict) else None
        base = (f"https://raw.githubusercontent.com/{full_name}/HEAD/{directory.strip('/')}/" if full_name and directory
                else f"https://cdn.jsdelivr.net/npm/{name}@{latest}/" if latest else None)
        candidates += readme_images(d["readme"], full_name or "", base=base)
    candidates += await package_logos(http, name, latest)   # logo.png, banner.png, ... shipped in the package
    if isinstance(homepage, str) and homepage.startswith("http") and "github.com" not in homepage:
        page = await fetch_page(homepage, http)
        if page:
            candidates += page_image_candidates(page)
    if full_name:
        # the GitHub owner's avatar is usually the project's or company's logo (@babel/*, @vercel/*, ...)
        candidates.append(f"https://avatars.githubusercontent.com/{full_name.split('/')[0]}?s=460")
    image = await best_image(http, candidates, last_resort=generic_card)
    links = [{"label": "npm", "url": f"https://www.npmjs.com/package/{name}"}]
    if full_name:
        links.append({"label": "Repository", "url": f"https://github.com/{full_name}"})
    if isinstance(homepage, str) and homepage.startswith("http") and (not full_name or full_name.lower() not in homepage.lower()):
        links.append({"label": "Homepage", "url": homepage})
    related = []
    if full_name:
        related.append({"kind": "repo", "label": full_name, "url": f"https://github.com/{full_name}"})
    if isinstance(homepage, str) and homepage.startswith("http") and (not full_name or full_name.lower() not in homepage.lower()):
        related.append({"kind": "site", "label": "Homepage", "url": homepage})
    return Enrichment(
        related=related, metadata=meta, canonical_url=f"https://www.npmjs.com/package/{name}", image_url=image,
        subtitle=d.get("description"), links=links, tags=[str(k).lower() for k in (d.get("keywords") or [])[:5]],
        source="npm", matched_title=name,
    )


# ---------------------------------------------------------------------------
# Hugging Face models, datasets and spaces


HF_RESERVED = {"docs", "blog", "papers", "collections", "models", "tasks", "pricing", "join", "login", "settings",
               "organizations", "api", "chat", "learn", "enterprise", "posts", "new", "welcome", "huggingface"}
HF_KINDS = {"models": "model", "datasets": "dataset", "spaces": "space"}


def hf_repo(analysis: dict) -> tuple[str, str] | None:
    """(kind, id) of a Hugging Face model, dataset or space named by the item's links."""
    for c in [analysis.get("canonical_url"), *[l.get("url") for l in analysis.get("links") or []]]:
        parts = urlsplit(c or "")
        if not (parts.hostname or "").lower().endswith("huggingface.co"):
            continue
        seg = [s for s in parts.path.split("/") if s]
        if len(seg) >= 2 and seg[0] in ("datasets", "spaces"):
            return seg[0], "/".join(seg[1:3]) if len(seg) >= 3 else seg[1]
        if len(seg) >= 2 and seg[0].lower() not in HF_RESERVED:
            return "models", "/".join(seg[:2])
    return None


async def hf_owner_logo(owner: str, http: httpx.AsyncClient) -> list[str]:
    """The logo of the organization (or user) that published it, from Hugging Face's avatar API."""
    for kind in ("organizations", "users"):
        try:
            r = await http.get(f"https://huggingface.co/api/{kind}/{owner}/avatar")
            url = r.json().get("avatarUrl") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            continue
        if isinstance(url, str) and url:
            return [urljoin("https://huggingface.co/", url)]
    return []


async def enrich_huggingface(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    """A Hugging Face model, dataset or space: facts from its API and a picture from its card."""
    ref = hf_repo(analysis)
    if not ref:
        return None
    kind, rid = ref
    r = await http.get(f"https://huggingface.co/api/{kind}/{rid}")
    if r.status_code != 200:
        return None
    d = r.json()
    card = d.get("cardData") if isinstance(d.get("cardData"), dict) else {}
    tags = [t for t in d.get("tags") or [] if isinstance(t, str)]
    license_ = card.get("license") or next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)
    params = (d.get("safetensors") or {}).get("total") if isinstance(d.get("safetensors"), dict) else None
    meta = {
        "huggingface_id": rid, "huggingface_kind": HF_KINDS[kind], "task": d.get("pipeline_tag"), "library": d.get("library_name"),
        "license": license_ if isinstance(license_, str) else None, "downloads": d.get("downloads"), "likes": d.get("likes"),
        "last_modified": d.get("lastModified"), "sdk": d.get("sdk"), "gated": True if d.get("gated") else None,
        "base_model": card.get("base_model") if isinstance(card.get("base_model"), str) else None,
        "parameters": f"{params / 1e9:.1f}B" if isinstance(params, (int, float)) and params >= 1e8 else None,
    }
    page_url = f"https://huggingface.co/{'' if kind == 'models' else kind + '/'}{rid}"
    resolve = f"{page_url}/resolve/main/"
    candidates: list[str] = []
    thumb = card.get("thumbnail")   # spaces set one in their card: a URL or a path in the repo
    if isinstance(thumb, str) and thumb:
        candidates.append(thumb if thumb.startswith("http") else resolve + thumb.lstrip("/"))
    try:
        rr = await http.get(f"{page_url}/raw/main/README.md")
        readme = re.sub(r"\A---\n.*?\n---\n", "", rr.text, flags=re.S) if rr.status_code == 200 else ""
    except httpx.HTTPError:
        readme = ""
    candidates += readme_images(readme, "", base=resolve)
    page = await fetch_page(page_url, http)
    if page:
        candidates += page_image_candidates(page)   # includes Hugging Face's generated social card
    image = await best_image(http, candidates, verified_only=True) or await best_image(
        http, await hf_owner_logo(rid.split("/")[0], http)) or await best_image(http, candidates)
    skip = ("region:", "license:", "endpoints_compatible", "autotrain_compatible", "text-generation-inference")
    related = [{"kind": "paper", "label": f"arXiv {t.split(':', 1)[1]}", "url": f"https://arxiv.org/abs/{t.split(':', 1)[1]}"}
               for t in tags if t.startswith("arxiv:")][:2]
    if meta["base_model"] and "/" in meta["base_model"]:
        related.append({"kind": "repo", "label": f"{meta['base_model']} (base model)", "url": f"https://huggingface.co/{meta['base_model']}"})
    return Enrichment(
        related=related,
        metadata=meta, canonical_url=page_url, image_url=image, subtitle=" · ".join(x for x in (d.get("pipeline_tag"), d.get("library_name")) if x) or None,
        links=[{"label": "Hugging Face", "url": page_url}],
        tags=[t for t in ([d.get("pipeline_tag")] + tags) if t and ":" not in t and not t.startswith(skip)][:5],
        source="huggingface", matched_title=rid,
    )


# ---------------------------------------------------------------------------
# Movies & TV: TMDB (+ OMDb for IMDb / RT / Metacritic scores)


def _tmdb_auth(key: str) -> tuple[dict, dict]:
    # v4 read tokens are JWTs; v3 keys are short hex strings.
    if key.startswith("eyJ"):
        return {"Authorization": f"Bearer {key}"}, {}
    return {}, {"api_key": key}


async def enrich_screen(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    is_tv = analysis.get("category") == "tv_show"
    details = analysis.get("details") or {}
    imdb_id = details.get("imdb_id")
    if not imdb_id:
        m = re.search(r"imdb\.com/title/(tt\d+)", analysis.get("canonical_url") or "")
        imdb_id = m.group(1) if m else None
    out = Enrichment(source="tmdb")

    if settings.tmdb_api_key:
        headers, params = _tmdb_auth(settings.tmdb_api_key)
        base = "https://api.themoviedb.org/3"
        kind = "tv" if is_tv else "movie"
        tmdb_id = None
        if analysis.get("_tmdb"):  # a shared themoviedb.org link
            kind, tmdb_id = analysis["_tmdb"]
        if imdb_id and not tmdb_id:
            r = await http.get(f"{base}/find/{imdb_id}", headers=headers, params={**params, "external_source": "imdb_id"})
            if r.status_code == 200:
                data = r.json()
                order = ("tv", "movie") if is_tv else ("movie", "tv")
                for k in order:
                    if data.get(f"{k}_results"):
                        tmdb_id, kind = data[f"{k}_results"][0]["id"], k
                        break
        if not tmdb_id and analysis.get("title"):
            q = {**params, "query": analysis["title"]}
            if analysis.get("year"):
                q["first_air_date_year" if is_tv else "year"] = analysis["year"]
            r = await http.get(f"{base}/search/{kind}", headers=headers, params=q)
            if r.status_code == 200 and r.json().get("results"):
                tmdb_id = r.json()["results"][0]["id"]
        if tmdb_id:
            r = await http.get(
                f"{base}/{kind}/{tmdb_id}", headers=headers,
                params={**params, "append_to_response": "credits,external_ids,videos,watch/providers"},
            )
            if r.status_code == 200:
                _apply_tmdb(out, r.json(), kind, os.environ.get("MAGPIE_REGION", "US"))
                imdb_id = out.metadata.get("imdb_id") or imdb_id

    if settings.omdb_api_key and imdb_id:
        r = await http.get("https://www.omdbapi.com/", params={"i": imdb_id, "apikey": settings.omdb_api_key})
        if r.status_code == 200 and r.json().get("Response") == "True":
            _apply_omdb(out, r.json())
            out.source = "tmdb+omdb" if out.metadata.get("tmdb_id") else "omdb"

    if not out.image_url and analysis.get("title"):
        out.image_url = await _wikipedia_poster(analysis["title"], analysis.get("year"), is_tv, http)

    if imdb_id:
        out.metadata["imdb_id"] = imdb_id
        out.canonical_url = f"https://www.imdb.com/title/{imdb_id}/"
    return out if (out.metadata or out.canonical_url or out.image_url) else None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


async def wikipedia_image(title: str, hint: str, http: httpx.AsyncClient) -> str | None:
    """The lead picture of the Wikipedia article about `title` (no API key needed), only when the top search
    hit is really about it. `hint` narrows the search, e.g. "film", "album", "Barcelona"."""
    query = f"{title} {hint}".strip()
    try:
        r = await http.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrlimit": 1,
            "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 600})
        if r.status_code != 200:
            return None
        pages = list(((r.json().get("query") or {}).get("pages") or {}).values())
    except (httpx.HTTPError, ValueError):
        return None
    if not pages or not _norm(title) or _norm(title) not in _norm(pages[0].get("title", "")):
        return None  # the top hit isn't about this title
    return (pages[0].get("thumbnail") or {}).get("source")


async def _wikipedia_poster(title: str, year: Any, is_tv: bool, http: httpx.AsyncClient) -> str | None:
    """The poster from the film's / show's Wikipedia infobox, when TMDB and OMDb gave none."""
    return await wikipedia_image(title, f"{year or ''} {'TV series' if is_tv else 'film'}".strip(), http)


def _apply_tmdb(out: Enrichment, d: dict, kind: str, region: str) -> None:
    credits = d.get("credits") or {}
    meta = out.metadata
    meta["tmdb_id"] = d["id"]
    out.matched_title = d.get("title") or d.get("name")
    meta["tmdb_rating"] = f"{d['vote_average']:.1f}/10" if d.get("vote_average") else None
    meta["genres"] = [g["name"] for g in d.get("genres") or []]
    meta["cast"] = [c["name"] for c in (credits.get("cast") or [])[:6]]
    meta["tagline"] = d.get("tagline") or None
    meta["imdb_id"] = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
    if kind == "movie":
        meta["directors"] = [c["name"] for c in credits.get("crew") or [] if c.get("job") == "Director"]
        meta["release_date"] = d.get("release_date")
        if d.get("runtime"):
            meta["runtime"] = f"{d['runtime'] // 60}h {d['runtime'] % 60}m"
        studios = d.get("production_companies") or []
        meta["network_or_studio"] = studios[0]["name"] if studios else None
    else:
        meta["creators"] = [c["name"] for c in d.get("created_by") or []]
        meta["first_air_date"] = d.get("first_air_date")
        meta["seasons"] = str(d["number_of_seasons"]) if d.get("number_of_seasons") else None
        meta["episodes"] = d.get("number_of_episodes")
        meta["status"] = d.get("status")
        nets = d.get("networks") or []
        meta["network_or_studio"] = nets[0]["name"] if nets else None
        if d.get("episode_run_time"):
            meta["runtime"] = f"{d['episode_run_time'][0]}m per episode"
    providers = ((d.get("watch/providers") or {}).get("results") or {}).get(region) or {}
    if providers.get("flatrate"):
        meta["where_to_watch"] = [p["provider_name"] for p in providers["flatrate"]]
    if providers.get("link"):
        out.links.append({"label": f"Where to watch ({region})", "url": providers["link"]})
    for v in (d.get("videos") or {}).get("results") or []:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            out.links.append({"label": "Trailer", "url": f"https://www.youtube.com/watch?v={v['key']}"})
            break
    out.links.append({"label": "TMDB", "url": f"https://www.themoviedb.org/{kind}/{d['id']}"})
    if d.get("poster_path"):
        out.image_url = TMDB_IMG + d["poster_path"]
    if d.get("overview"):
        out.summary = d["overview"]
    out.tags.extend(g.lower() for g in meta["genres"])


def _apply_omdb(out: Enrichment, d: dict) -> None:
    meta = out.metadata
    if d.get("imdbRating") not in (None, "N/A"):
        meta["imdb_rating"] = f"{d['imdbRating']}/10"
        if d.get("imdbVotes") not in (None, "N/A"):
            meta["imdb_votes"] = d["imdbVotes"]
    for r in d.get("Ratings") or []:
        if r.get("Source") == "Rotten Tomatoes":
            meta["rotten_tomatoes"] = r["Value"]
        elif r.get("Source") == "Metacritic":
            meta["metacritic"] = r["Value"]
    for key, name in (("Rated", "rated"), ("Awards", "awards")):
        if d.get(key) not in (None, "N/A"):
            meta[name] = d[key]
    if not out.image_url and d.get("Poster") not in (None, "N/A"):
        out.image_url = d["Poster"]
    if not meta.get("genres") and d.get("Genre") not in (None, "N/A"):
        meta["genres"] = [g.strip() for g in d["Genre"].split(",")]


# ---------------------------------------------------------------------------
# Books: Open Library (no key needed)


async def enrich_book(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    if not analysis.get("title"):
        return None
    params = {"title": analysis["title"], "limit": 1, "fields": "key,title,author_name,first_publish_year,cover_i,isbn,number_of_pages_median,subject,ratings_average"}
    author = (analysis.get("details") or {}).get("author")
    if author:
        params["author"] = author
    r = await http.get("https://openlibrary.org/search.json", params=params)
    docs = r.json().get("docs") if r.status_code == 200 else None
    if not docs:
        cover = await book_cover(analysis["title"], author, [], http)
        return Enrichment(image_url=cover, source="googlebooks") if cover else None
    doc = docs[0]
    meta = {
        "author": ", ".join(doc.get("author_name") or []) or None,
        "first_published": doc.get("first_publish_year"),
        "pages": doc.get("number_of_pages_median"),
        "isbn": (doc.get("isbn") or [None])[0],
        "openlibrary_rating": f"{doc['ratings_average']:.1f}/5" if doc.get("ratings_average") else None,
    }
    first = [f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"] if doc.get("cover_i") else []
    return Enrichment(
        metadata=meta,
        image_url=await best_image(http, first) if first else await book_cover(doc.get("title") or analysis["title"], author,
                                                                               (doc.get("isbn") or [])[:3], http),
        links=[{"label": "Open Library", "url": f"https://openlibrary.org{doc['key']}"}],
        tags=[s.lower() for s in (doc.get("subject") or [])[:4]],
        source="openlibrary",
        matched_title=doc.get("title"),
    )


async def book_cover(title: str, author: str | None, isbns: list[str], http: httpx.AsyncClient) -> str | None:
    """A cover when Open Library's search has none: Open Library by ISBN, then Google Books (no key needed)."""
    candidates = [f"https://covers.openlibrary.org/b/isbn/{i}-L.jpg?default=false" for i in isbns]
    try:
        q = f'intitle:"{title}"' + (f' inauthor:"{author}"' if author else "")
        r = await http.get("https://www.googleapis.com/books/v1/volumes", params={"q": q, "maxResults": 3, "printType": "books"})
        for v in (r.json().get("items") or []) if r.status_code == 200 else []:
            info = v.get("volumeInfo") or {}
            links = info.get("imageLinks") or {}
            if _norm(title) and _norm(title) in _norm(info.get("title", "")) and links.get("thumbnail"):
                # zoom=0 asks for the largest version; https because the app may be served over https
                candidates.append(links["thumbnail"].replace("http://", "https://").replace("&edge=curl", "").replace("zoom=1", "zoom=0"))
                candidates.append(links["thumbnail"].replace("http://", "https://").replace("&edge=curl", ""))
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return await best_image(http, candidates, verified_only=True)


# ---------------------------------------------------------------------------
# Music, podcasts and apps: the iTunes Search API (no key needed)

ITUNES_ENTITY = {"music": ("album", "music"), "podcast": ("podcast", "podcast"), "app": ("software", "software")}


async def enrich_itunes(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    """Album, podcast or app artwork (600 px) and a few facts, matched by title (and artist, when known)."""
    title, category = analysis.get("title"), analysis.get("category")
    if not title or category not in ITUNES_ENTITY or npm_package(analysis) or hf_repo(analysis):
        return None
    entity, media = ITUNES_ENTITY[category]
    details = analysis.get("details") or {}
    artist = details.get("artist") or details.get("author") or details.get("creator")
    term = f"{title} {artist}" if artist and category != "app" else title
    try:
        r = await http.get("https://itunes.apple.com/search", params={"term": term, "entity": entity, "media": media, "limit": 5})
        results = r.json().get("results") or [] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    name_key = {"album": "collectionName", "podcast": "collectionName", "software": "trackName"}[entity]
    hit = next((x for x in results if _norm(title) and (_norm(title) in _norm(x.get(name_key, "")) or _norm(x.get(name_key, "")) in _norm(title))), None)
    if not hit:
        return None
    art = hit.get("artworkUrl512") or hit.get("artworkUrl100") or hit.get("artworkUrl600")
    image = await best_image(http, [re.sub(r"/\d+x\d+(bb)?\.(jpg|png)$", r"/600x600bb.\2", art or ""), art])
    meta = {"artist": hit.get("artistName") or hit.get("sellerName"), "genre": hit.get("primaryGenreName"),
            "released": (hit.get("releaseDate") or "")[:10] or None}
    if entity == "album":
        meta["tracks"] = hit.get("trackCount")
    elif entity == "podcast":
        meta["episodes"] = hit.get("trackCount")
    else:
        meta.update(price=hit.get("formattedPrice"), app_rating=f"{hit['averageUserRating']:.1f}/5" if hit.get("averageUserRating") else None,
                    version=hit.get("version"))
    url = hit.get("collectionViewUrl") or hit.get("trackViewUrl")
    return Enrichment(metadata=meta, image_url=image, links=[{"label": "Apple", "url": url}] if url else [],
                      source="itunes", matched_title=hit.get(name_key))


# ---------------------------------------------------------------------------
# Last resort for things with a Wikipedia article (places, events, products, ...)

WIKI_HINTS = {"place": "", "event": "", "product": "", "course": "", "music": "album", "podcast": "podcast",
              "app": "software", "book": "novel"}


async def enrich_wikipedia(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    title = analysis.get("title")
    if (not title or analysis.get("category") not in WIKI_HINTS or len(_norm(title)) < 4
            or re.search(r"https?://|\w\.\w+/|^[\w.-]+\.[a-z]{2,}$", title) or npm_package(analysis) or hf_repo(analysis)):
        return None   # nothing to look up, or a URL / package name rather than the name of a thing
    image = await wikipedia_image(title, WIKI_HINTS[analysis["category"]], http)
    image = image and await best_image(http, [image], verified_only=True)
    return Enrichment(image_url=image, source="wikipedia") if image else None


# ---------------------------------------------------------------------------
# Web pages: schema.org Recipe JSON-LD and OpenGraph


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.links: list[dict] = []
        self.ld_json: list[str] = []
        self.title = ""
        self._in_ld = False
        self._in_title = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and a.get("content") and key.lower() not in self.meta:
                self.meta[key.lower()] = a["content"]
        elif tag == "link" and a.get("href") and (a.get("rel") or a.get("itemprop")):
            self.links.append({"rel": (a.get("rel") or a.get("itemprop", "")).lower(), "href": a["href"],
                               "sizes": a.get("sizes", ""), "type": a.get("type", "")})
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.ld_json.append("".join(self._buf))
            self._in_ld = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._in_title:
            self.title += data


TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|igshid|igsh|mc_cid|mc_eid|ref_src|ref_url|_hsenc|_hsmi|"
                             r"cmpid|ocid|mbid|smid|sr_share|share_id|guccounter|guce_referrer\w*|__twitter_impression|_ga|_gl|"
                             r"trk|trkcampaign|spm|at_medium|at_campaign|wt_mc|oly_enc_id|vero_id|mkt_tok)$", re.I)
# Parameters that are only tracking on specific sites (elsewhere they can matter, e.g. ?s= search).
SITE_TRACKING = {"x.com": {"s", "t"}, "twitter.com": {"s", "t"}, "youtube.com": {"si", "feature", "pp"},
                 "open.spotify.com": {"si"}, "instagram.com": {"img_index"}}


def strip_tracking(url: str | None) -> str | None:
    """The same address without campaign and click-tracking parameters or a fragment."""
    if not url or not url.startswith("http"):
        return url
    parts = urlsplit(url)
    site = SITE_TRACKING.get((parts.hostname or "").lower().removeprefix("www."), set())
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k) and k not in site]
    query = urlencode(kept) if len(kept) != len(parse_qsl(parts.query, keep_blank_values=True)) else parts.query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def canonical_link(page: "Page") -> str:
    """The article's own address: the page's declared canonical link (which also leads from an AMP, mobile or
    syndicated copy back to the original), else og:url, else where the fetch ended up; without tracking parameters.
    A declared address that is just the site's home page (a common template mistake) is ignored."""
    here = urlsplit(page.url)
    declared = [l["href"] for l in page.links if "canonical" in l["rel"].split()] + [page.meta.get("og:url")]
    for href in declared:
        if not href:
            continue
        url = urljoin(page.url, href.strip())
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        if parts.path.strip("/") == "" and here.path.strip("/") != "":
            continue
        return strip_tracking(url)
    return strip_tracking(page.url)


@dataclass
class Page:
    url: str
    meta: dict[str, str]
    ld: list[dict]
    title: str
    html: str = ""
    links: list = field(default_factory=list)   # <link rel=... href=...> tags (image_src, icons)


_PAGE_CACHE: dict[str, tuple[float, Page | None]] = {}
PAGE_CACHE_SECONDS = 120


async def fetch_page(url: str, http: httpx.AsyncClient) -> Page | None:
    hit = _PAGE_CACHE.get(url)
    if hit and time.monotonic() - hit[0] < PAGE_CACHE_SECONDS:
        return hit[1]
    page = await _fetch_page(url, http)
    if len(_PAGE_CACHE) > 200:
        _PAGE_CACHE.clear()
    _PAGE_CACHE[url] = (time.monotonic(), page)
    return page


# Pages that answered but turned us away (bot walls, rate limits): only these may be looked up elsewhere.
# Private or blocked addresses never are, so no internal hostname leaves the server.
_REFUSED: dict[str, float] = {}
_GONE: dict[str, float] = {}   # answered 404/410, or the name doesn't exist: the link is dead, not just unreachable right now
REFUSING_STATUSES = {401, 403, 406, 429, 451, 500, 502, 503, 520, 521, 522, 523, 524, 525, 526}


def link_is_gone(url: str) -> bool:
    return time.monotonic() - _GONE.get(url, -1e9) < PAGE_CACHE_SECONDS


def page_refused(url: str) -> bool:
    return time.monotonic() - _REFUSED.get(url, -1e9) < PAGE_CACHE_SECONDS


async def _fetch_page(url: str, http: httpx.AsyncClient) -> Page | None:
    try:
        r = await safe_get(http, url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
                                               "Accept-Language": "en-US,en;q=0.9"})
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Fetching %s failed: %s", url, e)
        if isinstance(e, BlockedURL) and "Can't resolve" in str(e):
            _GONE[url] = time.monotonic()   # no such host
        return None
    if r.status_code in (404, 410):
        if len(_GONE) > 500:
            _GONE.clear()
        _GONE[url] = time.monotonic()
    if r.status_code in REFUSING_STATUSES:
        if len(_REFUSED) > 500:
            _REFUSED.clear()
        _REFUSED[url] = time.monotonic()
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return None
    html = r.text[:MAX_BYTES]
    p = _PageParser()
    p.feed(html)
    ld: list[dict] = []
    for raw in p.ld_json:
        try:
            ld.extend(_walk_ld(json.loads(raw)))
        except json.JSONDecodeError:
            continue
    return Page(url=str(r.url), meta=p.meta, ld=ld, title=p.title.strip(), html=html, links=p.links)


def _walk_ld(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [x for n in node for x in _walk_ld(n)]
    if isinstance(node, dict):
        out = [node]
        if "@graph" in node:
            out += _walk_ld(node["@graph"])
        return out
    return []


def _ld_types(node: dict) -> list[str]:
    t = node.get("@type")
    return [x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def _ld_image(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _ld_image(value[0])
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl")
    return None


def _ld_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        names = [n for n in (_ld_name(v) for v in value) if n]
        return ", ".join(names) or None
    if isinstance(value, dict):
        return value.get("name")
    return None


def iso_duration(value: str | None) -> str | None:
    """PT1H30M -> '1h 30m'."""
    if not value or not isinstance(value, str):
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", value.strip())
    if not m:
        return value
    d, h, mins = (int(x) if x else 0 for x in m.groups())
    h += d * 24
    parts = [f"{h}h" if h else "", f"{mins}m" if mins else ""]
    return " ".join(p for p in parts if p) or None


def _instructions(value: Any) -> list[str]:
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"\n+", value) if s.strip()]
    steps: list[str] = []
    for v in value if isinstance(value, list) else [value]:
        if isinstance(v, str):
            steps.append(v.strip())
        elif isinstance(v, dict):
            if "itemListElement" in v:
                steps.extend(_instructions(v["itemListElement"]))
            elif v.get("text"):
                steps.append(re.sub(r"\s+", " ", v["text"]).strip())
    return steps


def recipe_from_page(page: Page) -> Enrichment | None:
    recipe = next((n for n in page.ld if "recipe" in _ld_types(n)), None)
    if not recipe:
        return None
    rating = recipe.get("aggregateRating") or {}
    nutrition = recipe.get("nutrition") or {}
    yield_ = recipe.get("recipeYield")
    if isinstance(yield_, list):
        yield_ = next((str(y) for y in yield_ if not str(y).isdigit()), str(yield_[0]) if yield_ else None)
    meta = {
        "ingredients": [re.sub(r"\s+", " ", i).strip() for i in recipe.get("recipeIngredient") or [] if isinstance(i, str)],
        "instructions": _instructions(recipe.get("recipeInstructions")),
        "prep_time": iso_duration(recipe.get("prepTime")),
        "cook_time": iso_duration(recipe.get("cookTime")),
        "total_time": iso_duration(recipe.get("totalTime")),
        "servings": str(yield_) if yield_ else None,
        "cuisine": _ld_name(recipe.get("recipeCuisine")),
        "course": _ld_name(recipe.get("recipeCategory")),
        "author": _ld_name(recipe.get("author")),
        "calories": nutrition.get("calories") if isinstance(nutrition, dict) else None,
        "rating": f"{float(rating['ratingValue']):.1f}/5" if rating.get("ratingValue") else None,
        "rating_count": rating.get("ratingCount") or rating.get("reviewCount"),
    }
    tags = [t.strip().lower() for t in re.split(r",", recipe.get("keywords") or "") if t.strip()][:5] if isinstance(recipe.get("keywords"), str) else []
    return Enrichment(
        metadata=meta,
        canonical_url=page.url,
        image_url=_absolute(page.url, _ld_image(recipe.get("image"))),
        summary=recipe.get("description") or None,
        tags=tags,
        source="schema.org/Recipe",
        matched_title=recipe.get("name"),
    )


def opengraph_from_page(page: Page) -> Enrichment:
    m = page.meta
    meta = {
        "site_name": m.get("og:site_name"),
        "page_title": m.get("og:title") or m.get("twitter:title") or page.title or None,
        "author": m.get("author") or m.get("article:author"),
        "published_date": m.get("article:published_time"),
    }
    article = next((n for n in page.ld if {"article", "newsarticle", "blogposting"} & set(_ld_types(n))), None)
    if article:
        meta["author"] = meta["author"] or _ld_name(article.get("author"))
        meta["published_date"] = meta["published_date"] or article.get("datePublished")
    # og:description goes into metadata rather than overriding Claude's summary.
    desc = m.get("og:description") or m.get("description")
    if desc:
        meta["page_description"] = desc
    return Enrichment(
        metadata=meta,
        canonical_url=canonical_link(page),
        image_url=_absolute(page.url, m.get("og:image") or m.get("twitter:image")),
        source="opengraph",
    )


def _absolute(base: str, url: str | None) -> str | None:
    return urljoin(base, url) if url else None


async def enrich_web(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    url = analysis.get("canonical_url")
    if not url or not url.startswith("http"):
        return None
    page = await fetch_page(url, http)
    if not page:
        # Blocked or behind a consent wall: a YouTube video still has a thumbnail we can address directly,
        # many articles have a copy in the Internet Archive with the same share image, and failing that
        # the site's logo is at a known address.
        thumb = await best_image(http, youtube_thumbnails(url)) or (await archived_picture(url, http) if page_refused(url) else None)
        if thumb:
            return Enrichment(image_url=thumb, source="archive", canonical_url=strip_tracking(url))
        logo = await best_image(http, origin_icons(url), verified_only=True) if page_refused(url) else None
        return Enrichment(image_url=logo, image_kind="logo", canonical_url=strip_tracking(url)) if logo else None
    if analysis.get("category") == "recipe":
        found = recipe_from_page(page)
        if found:
            found.image_url = await best_image(http, page_image_candidates(page, [found.image_url]), page.url) or found.image_url
            return found
    e = with_readability(opengraph_from_page(page), page)
    e.image_url, e.image_kind = await page_picture(http, page, e.image_url)
    e.related = outbound_related(page)   # the repository, app, paper or company the article is about
    # ... and the ones it only names in its text ("github.com/owner/repo", "example.dev")
    e.related += [r for r in text_related(e.metadata.get("article_text") or "", limit=4)
                  if not (r["kind"] == "site" and same_site(r["url"], page.url))]
    return e


async def archived_picture(url: str, http: httpx.AsyncClient) -> str | None:
    """The share image of the Internet Archive's latest copy of a page that won't answer us. The original image
    address comes first (the page may block servers while its image CDN doesn't), then the archived file."""
    try:
        r = await http.get("https://archive.org/wayback/available", params={"url": url}, timeout=10)
        snap = ((r.json().get("archived_snapshots") or {}).get("closest") or {}) if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    ts = snap.get("timestamp")
    if not snap.get("available") or not ts:
        return None
    page = await fetch_page(f"https://web.archive.org/web/{ts}id_/{url}", http)   # id_: the page as it was, unrewritten
    if not page:
        return None
    originals = [urljoin(url, u) for u in page_image_candidates(page)[:4]]
    originals = [re.sub(r"^https?://web\.archive\.org/web/\d+(?:id_|im_)?/", "", u) for u in originals]
    return await best_image(http, [*originals, *(f"https://web.archive.org/web/{ts}im_/{u}" for u in originals)], verified_only=True)


async def page_picture(http: httpx.AsyncClient, page: Page, lead: str | None = None) -> tuple[str | None, str | None]:
    """The best picture a web page offers, and whether it is only the site's logo ("logo"). In order:
    1. a picture of the page, checked: share image, structured data, the header picture, lead and content pictures;
    2. the oEmbed thumbnail and the video's own thumbnail;
    3. the page's own share image even if its host wouldn't let us check it (browsers usually get it);
    4. the site's logo: publisher logo, app manifest and touch icons, the icon at its usual addresses;
    5. any picture that merely couldn't be ruled out."""
    pictures = page_pictures(page, [lead])
    found = await best_image(http, pictures, page.url, verified_only=True)
    if found:
        return found, None
    extra = [await oembed_thumbnail(http, page), *youtube_thumbnails(page.url)]
    found = await best_image(http, extra, page.url, verified_only=True)
    if found:
        return found, None
    for share in pictures[:2]:
        if await verify_image(http, share) is None:
            return share, None
    logos = [*site_logos(page)[:3], *await manifest_icons(http, page), *site_logos(page)[3:]]
    found = await best_image(http, logos, page.url, verified_only=True)
    if found:
        return found, "logo"
    return (await best_image(http, pictures, page.url) or lead), None


def with_readability(e: Enrichment, page: Page) -> Enrichment:
    """Add the page's main content (reader view): excerpt, byline, reading time, full text."""
    article = readability.extract(page.html, page.url)
    if not article:
        return e
    m = e.metadata
    m["author"] = m.get("author") or article.author
    m["site_name"] = m.get("site_name") or article.site_name
    m["published_date"] = m.get("published_date") or article.published_date
    m["page_title"] = m.get("page_title") or article.title
    if article.excerpt:
        m["excerpt"] = article.excerpt
    if article.word_count:
        m["word_count"] = article.word_count
        m["reading_time"] = f"{article.reading_minutes} min read"
        m["article_text"] = article.text  # hidden in the UI; makes the article searchable
    if article.language:
        m["language"] = article.language
    e.image_url = e.image_url or article.image
    e.source = "opengraph+readability"
    return e


# ---------------------------------------------------------------------------


# The picture of the LAST enricher that found one wins, so the most specific source runs last
# (a book's cover beats the picture on the page you shared).
ENRICHERS = {
    "github_repo": [enrich_github],
    "movie": [enrich_screen],
    "tv_show": [enrich_screen],
    "book": [enrich_web, enrich_book],
    "recipe": [enrich_web],
    "music": [enrich_web, enrich_itunes],
    "podcast": [enrich_web, enrich_itunes],
    "app": [enrich_web, enrich_itunes],
}
DEFAULT_ENRICHERS = [enrich_web]
# Sources recognized from the item's links, whatever its category: (does it name this source?, enricher)
URL_ENRICHERS = [(npm_package, enrich_npm), (hf_repo, enrich_huggingface)]


async def run_enrichers(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> list[Enrichment]:
    results = []
    if not settings.enrich:
        return results
    fns = list(ENRICHERS.get(analysis.get("category"), DEFAULT_ENRICHERS))
    for detect, fn in URL_ENRICHERS:
        if detect(analysis) and fn not in fns:
            fns.append(fn)  # after the page enricher, so the source's own picture wins
    for fn in fns:
        try:
            e = await fn(analysis, settings, http)
        except Exception:  # enrichment is best-effort
            log.exception("Enricher %s failed", fn.__name__)
            continue
        if e:
            results.append(e)
    if not any(e.image_url for e in results):   # nothing had a picture: try the Wikipedia article about it
        try:
            if e := await enrich_wikipedia(analysis, settings, http):
                results.append(e)
        except Exception:
            log.exception("Enricher enrich_wikipedia failed")
    return results
