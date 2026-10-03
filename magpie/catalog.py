"""Look a movie, TV show or book up by name, so the user can pick the right one before it is added."""
from __future__ import annotations

import os
from typing import Any

import httpx

from .config import Settings
from .enrich import _tmdb_auth

KINDS = {"movie": "Movie", "tv_show": "TV show", "book": "Book"}
LIMIT = 8
TMDB = "https://api.themoviedb.org/3"


def _year(text: str | None) -> int | None:
    try:
        return int((text or "")[:4])
    except ValueError:
        return None


def _shorten(text: str | None, size: int = 160) -> str | None:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


async def _tmdb(kind: str, query: str, settings: Settings, http: httpx.AsyncClient) -> list[dict]:
    tv = kind == "tv_show"
    headers, params = _tmdb_auth(settings.tmdb_api_key)
    r = await http.get(f"{TMDB}/search/{'tv' if tv else 'movie'}", headers=headers, params={**params, "query": query})
    r.raise_for_status()
    out = []
    for d in (r.json().get("results") or [])[:LIMIT]:
        title = d.get("name" if tv else "title")
        if not title or not d.get("id"):
            continue
        year = _year(d.get("first_air_date" if tv else "release_date"))
        out.append({
            "title": title, "year": year, "category": kind,
            "subtitle": " · ".join(str(x) for x in (KINDS[kind], year) if x),
            "overview": _shorten(d.get("overview")),
            "image_url": f"https://image.tmdb.org/t/p/w185{d['poster_path']}" if d.get("poster_path") else None,
            "tmdb": ["tv" if tv else "movie", d["id"]],
        })
    return out


async def _omdb(kind: str, query: str, settings: Settings, http: httpx.AsyncClient) -> list[dict]:
    r = await http.get("https://www.omdbapi.com/", params={
        "s": query, "type": "series" if kind == "tv_show" else "movie", "apikey": settings.omdb_api_key})
    data = r.json() if r.status_code == 200 else {}
    out = []
    for d in (data.get("Search") or [])[:LIMIT]:
        year = _year(d.get("Year"))
        out.append({
            "title": d["Title"], "year": year, "category": kind,
            "subtitle": " · ".join(str(x) for x in (KINDS[kind], year) if x), "overview": None,
            "image_url": d["Poster"] if str(d.get("Poster", "")).startswith("http") else None,
            "imdb_id": d.get("imdbID"),
        })
    return out


async def _books(query: str, http: httpx.AsyncClient) -> list[dict]:
    r = await http.get("https://openlibrary.org/search.json", params={
        "q": query, "limit": LIMIT, "fields": "key,title,author_name,first_publish_year,cover_i"})
    r.raise_for_status()
    out = []
    for d in r.json().get("docs") or []:
        if not d.get("title"):
            continue
        authors = ", ".join((d.get("author_name") or [])[:2])
        year = d.get("first_publish_year")
        out.append({
            "title": d["title"], "year": year, "category": "book", "author": (d.get("author_name") or [None])[0],
            "subtitle": " · ".join(x for x in (authors, str(year) if year else "") if x) or "Book", "overview": None,
            "image_url": f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg" if d.get("cover_i") else None,
        })
    return out


async def search(kind: str, query: str, settings: Settings, http: httpx.AsyncClient) -> dict[str, Any]:
    """{results: [...], note?: why there may be none}. Every result carries what the pipeline needs to identify it exactly."""
    query = query.strip()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    if not query:
        return {"results": []}
    try:
        if kind == "book":
            return {"results": await _books(query, http)}
        if settings.tmdb_api_key:
            return {"results": await _tmdb(kind, query, settings, http)}
        if settings.omdb_api_key:
            return {"results": await _omdb(kind, query, settings, http)}
    except (httpx.HTTPError, ValueError) as e:
        return {"results": [], "note": f"The lookup service didn't answer ({e.__class__.__name__}). You can still add it as typed."}
    return {"results": [], "note": "Set a TMDB key in Settings to search movies and TV shows. You can still add it as typed."}


def entry_analysis(spec: dict) -> dict:
    """The analysis for something the user picked (or typed) themselves: certain, and looked up by its exact id when known."""
    details = {k: spec[k] for k in ("author", "imdb_id") if spec.get(k)}
    analysis = {
        "category": spec["category"], "title": spec["title"], "year": spec.get("year"),
        "source_platform": None, "canonical_url": None, "subtitle": None, "summary": spec.get("overview"),
        "image_url": spec.get("image_url"), "links": [], "related": [], "tags": [], "screenshot_text": None,
        "details": details, "confidence": 100, "confidence_reason": "Added by you.", "alternatives": [],
    }
    if spec.get("tmdb"):
        analysis["_tmdb"] = list(spec["tmdb"])
    return analysis
