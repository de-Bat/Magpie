"""Radarr: add a saved film to the download list."""
from __future__ import annotations

from ..config import Spec
from . import register
from .arr import ArrPlugin, arr_specs, date_row, just_added, section, summarize_queue


class Radarr(ArrPlugin):
    id = "radarr"
    prefix = "radarr"
    label = "Radarr"
    action_label = "Add to Radarr"
    categories = ("movie",)
    specs = arr_specs("radarr", "Radarr", 7878, (
        Spec("radarr_search", "RADARR_SEARCH", "bool", True, "Radarr", "Search right away",
             "Start looking for a release as soon as the film is added."),
    ))

    async def _find(self, item, settings, http) -> dict:
        tmdb, imdb, title, year = self.identify(item)
        terms = ([f"tmdb:{tmdb}"] if tmdb else []) + ([f"imdb:{imdb}"] if imdb else [])
        return await self.lookup(http, settings, "/movie/lookup", terms, title, year)

    async def _existing(self, movie, settings, http) -> dict | None:
        if not movie.get("tmdbId"):
            return None
        found = await self.call(http, settings, "GET", "/movie", params={"tmdbId": movie["tmdbId"]})
        return found[0] if found else None

    def _link(self, settings, movie) -> str:
        return f"{self.base_url(settings)}/movie/{movie.get('titleSlug') or movie.get('tmdbId')}"

    async def _download(self, movie: dict, settings, http) -> dict:
        """Downloaded, downloading (with a percentage), pending or missing."""
        if movie.get("hasFile"):
            file = movie.get("movieFile") or {}
            quality = ((file.get("quality") or {}).get("quality") or {}).get("name")
            size = file.get("size") or movie.get("sizeOnDisk")
            return {"state": "downloaded", "label": "Downloaded",
                    "detail": " · ".join(x for x in (quality, f"{size / 1e9:.1f} GB" if size else None) if x) or None}
        queued = summarize_queue(await self.queue_of(http, settings, movieId=movie.get("id")))
        if queued:
            return queued
        if not movie.get("monitored"):
            return {"state": "missing", "label": "Missing", "detail": "Not monitored in Radarr"}
        if just_added(movie):
            return {"state": "pending", "label": "Searching", "detail": "Radarr is looking for a release"}
        if movie.get("isAvailable") is False:
            return {"state": "pending", "label": "Not released yet", "detail": "Radarr will search when it is available"}
        return {"state": "missing", "label": "Missing", "detail": "No release found yet"}

    def _section(self, movie: dict, download: dict | None) -> dict:
        return section("Radarr", download, date_row("Digital release", movie.get("digitalRelease")),
                       date_row("In cinemas", movie.get("inCinemas")), date_row("Physical release", movie.get("physicalRelease")))

    async def status(self, item, settings, http):
        movie = await self._find(item, settings, http)
        existing = await self._existing(movie, settings, http)
        if existing:
            download = await self._download(existing, settings, http)
            return {"state": "added", "message": "In Radarr", "url": self._link(settings, movie), "download": download,
                    "section": self._section({**movie, **{k: v for k, v in existing.items() if v}}, download)}
        return {"state": "available", "section": self._section(movie, None)}

    async def run(self, item, settings, http):
        movie = await self._find(item, settings, http)
        name = f"{movie.get('title')} ({movie.get('year')})" if movie.get("year") else movie.get("title")
        if await self._existing(movie, settings, http):
            return {**await self.status(item, settings, http), "message": f"{name} is already in Radarr"}
        search = self.with_search(settings)
        body = {k: v for k, v in movie.items() if k != "id"}
        body.update(
            qualityProfileId=await self.quality_profile_id(http, settings),
            rootFolderPath=await self.root_folder(http, settings),
            monitored=True, minimumAvailability="released",
            addOptions={"searchForMovie": search},
        )
        created = await self.call(http, settings, "POST", "/movie", json=body) or {}
        download = None
        if created.get("id"):   # just added: with a search running nothing has been looked for yet, so it isn't "missing"
            download = ({"state": "pending", "label": "Searching", "detail": "Radarr is looking for a release"} if search
                        else await self._download(created, settings, http))
        return {"state": "added", "message": f"Added {name} to Radarr" + (" and started searching" if search else ""),
                "url": self._link(settings, movie), "download": download, "section": self._section(movie, download)}


register(Radarr())
