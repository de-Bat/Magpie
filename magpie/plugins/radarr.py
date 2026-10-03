"""Radarr: add a saved film to the download list."""
from __future__ import annotations

from ..config import Spec
from . import register
from .arr import ArrPlugin, arr_specs


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

    async def status(self, item, settings, http):
        movie = await self._find(item, settings, http)
        if await self._existing(movie, settings, http):
            return {"state": "added", "message": "In Radarr", "url": self._link(settings, movie)}
        return {"state": "available"}

    async def run(self, item, settings, http):
        movie = await self._find(item, settings, http)
        name = f"{movie.get('title')} ({movie.get('year')})" if movie.get("year") else movie.get("title")
        if await self._existing(movie, settings, http):
            return {"state": "added", "message": f"{name} is already in Radarr", "url": self._link(settings, movie)}
        search = self.with_search(settings)
        body = {k: v for k, v in movie.items() if k != "id"}
        body.update(
            qualityProfileId=await self.quality_profile_id(http, settings),
            rootFolderPath=await self.root_folder(http, settings),
            monitored=True, minimumAvailability="released",
            addOptions={"searchForMovie": search},
        )
        await self.call(http, settings, "POST", "/movie", json=body)
        return {"state": "added", "message": f"Added {name} to Radarr" + (" and started searching" if search else ""),
                "url": self._link(settings, movie)}


register(Radarr())
