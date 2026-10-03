"""Shared code for the *arr download managers (Radarr, Sonarr): same API v3, same auth, same add flow."""
from __future__ import annotations

import re

import httpx

from ..config import Spec, _origin
from . import Plugin, PluginError


def arr_specs(prefix: str, label: str, port: int, extra: tuple[Spec, ...] = ()) -> tuple[Spec, ...]:
    env = prefix.upper()
    return (
        Spec(f"{prefix}_url", f"{env}_URL", "str", None, label, "Server URL",
             f"Where {label} runs, e.g. http://{prefix}:{port}"),
        Spec(f"{prefix}_api_key", f"{env}_API_KEY", "secret", None, label, "API key",
             f"{label} → Settings → General → Security → API Key."),
        Spec(f"{prefix}_root_folder", f"{env}_ROOT_FOLDER", "str", None, label, "Root folder",
             "Where new items are stored. Blank = the first root folder configured in " + label + "."),
        Spec(f"{prefix}_quality_profile", f"{env}_QUALITY_PROFILE", "str", None, label, "Quality profile",
             "Name or id. Blank = the first profile."),
        *extra,
    )


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def year_of(value) -> int | None:
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", str(value or ""))
    return int(m.group(1)) if m else None


def pick_match(results: list[dict], title: str, year: int | None) -> dict | None:
    """From a title search, only accept a result whose title is the same; prefer the right year."""
    same = [r for r in results if norm(r.get("title")) == norm(title)]
    if year:
        for r in same:
            if r.get("year") == year:
                return r
    return same[0] if same else None


class ArrPlugin(Plugin):
    prefix = ""   # settings prefix: radarr -> radarr_url, radarr_api_key, ...
    noun = ""     # "movie" / "series"

    def __init__(self):
        self.url_key = (f"{self.prefix.upper()}_URL", f"{self.prefix.upper()}_API_KEY")

    # ---- settings -------------------------------------------------------------------------------

    def cfg(self, settings, name: str):
        return settings.value(f"{self.prefix}_{name}")

    def base_url(self, settings, url: str | None = None) -> str:
        url = (url or self.cfg(settings, "url") or "").strip().rstrip("/")
        if not re.match(r"^https?://[^/\s]+", url):
            raise PluginError(f"{self.label}'s server URL must start with http:// or https://", 400)
        return url

    def configured(self, settings) -> bool:
        return bool(self.cfg(settings, "url") and self.cfg(settings, "api_key"))

    def problems(self, settings):
        has_url, has_key = bool(self.cfg(settings, "url")), bool(self.cfg(settings, "api_key"))
        if has_url != has_key:
            env = f"{self.prefix.upper()}_{'API_KEY' if has_url else 'URL'}"
            missing = "API key" if has_url else "server URL"
            return [("warning", f"{self.label} needs both a server URL and an API key; the {missing} is missing.", env)]
        return []

    # ---- HTTP -----------------------------------------------------------------------------------

    async def call(self, http, settings, method: str, path: str, *, url: str | None = None,
                   key: str | None = None, params: dict | None = None, json=None):
        base = self.base_url(settings, url)
        key = key if key is not None else self.cfg(settings, "api_key")
        try:
            r = await http.request(method, f"{base}/api/v3{path}", params=params, json=json,
                                   headers={"X-Api-Key": key or ""}, timeout=20)
        except httpx.HTTPError as e:
            raise PluginError(f"Couldn't reach {self.label} at {base}: {e!r}", 502) from None
        if r.status_code in (401, 403):
            raise PluginError(f"{self.label} rejected the API key.", 400)
        if r.status_code >= 400:
            raise PluginError(f"{self.label} answered {r.status_code}: {_detail(r)}", 502)
        return r.json() if r.content else None

    async def test(self, settings, http, url=None, key=None):
        if url and key is None and _origin(url) != _origin(self.cfg(settings, "url")):
            raise PluginError("Enter the API key to test a different server.", 400)
        status = await self.call(http, settings, "GET", "/system/status", url=url, key=key)
        folders = await self.call(http, settings, "GET", "/rootfolder", url=url, key=key) or []
        profiles = await self.call(http, settings, "GET", "/qualityprofile", url=url, key=key) or []
        version = (status or {}).get("version", "")
        return {"ok": True, "message": f"Connected to {self.label} {version}".strip(),
                "root_folders": [f.get("path") for f in folders],
                "quality_profiles": [p.get("name") for p in profiles]}

    # ---- what to add --------------------------------------------------------------------------

    def identify(self, item: dict) -> tuple[int | None, str | None, str, int | None]:
        m = item.get("metadata") or {}
        try:
            tmdb = int(m.get("tmdb_id")) if m.get("tmdb_id") else None
        except (TypeError, ValueError):
            tmdb = None
        imdb = str(m.get("imdb_id") or "").strip()
        imdb = imdb if re.fullmatch(r"tt\d+", imdb) else None
        return tmdb, imdb, (item.get("title") or "").strip(), year_of(m.get("year"))

    async def root_folder(self, http, settings) -> str:
        folders = await self.call(http, settings, "GET", "/rootfolder") or []
        paths = [f.get("path") for f in folders if f.get("path")]
        if not paths:
            raise PluginError(f"{self.label} has no root folder set up yet.", 400)
        wanted = (self.cfg(settings, "root_folder") or "").strip()
        if not wanted:
            return paths[0]
        for p in paths:
            if p.rstrip("/\\") == wanted.rstrip("/\\"):
                return p
        raise PluginError(f"Root folder {wanted!r} isn't one of {self.label}'s: {', '.join(paths)}", 400)

    async def quality_profile_id(self, http, settings) -> int:
        profiles = await self.call(http, settings, "GET", "/qualityprofile") or []
        if not profiles:
            raise PluginError(f"{self.label} has no quality profile set up yet.", 400)
        wanted = str(self.cfg(settings, "quality_profile") or "").strip()
        if not wanted:
            return profiles[0]["id"]
        for p in profiles:
            if str(p.get("id")) == wanted or norm(p.get("name")) == norm(wanted):
                return p["id"]
        raise PluginError(f"Quality profile {wanted!r} isn't one of {self.label}'s: "
                          f"{', '.join(p.get('name', '') for p in profiles)}", 400)

    async def lookup(self, http, settings, path: str, terms: list[str], title: str, year: int | None) -> dict:
        """The first hit for an id term, else a title search that must match the title."""
        for term in terms:
            found = await self.call(http, settings, "GET", path, params={"term": term})
            if found:
                return found[0]
        if title:
            found = await self.call(http, settings, "GET", path, params={"term": f"{title} {year}" if year else title})
            match = pick_match(found or [], title, year)
            if match:
                return match
        raise PluginError(f"Couldn't find “{title or 'this item'}” in {self.label}'s lookup.", 404)

    def with_search(self, settings) -> bool:
        return bool(self.cfg(settings, "search"))


def _detail(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:200] or r.reason_phrase
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if isinstance(data, dict):
        return str(data.get("errorMessage") or data.get("message") or data)[:200]
    return str(data)[:200]

