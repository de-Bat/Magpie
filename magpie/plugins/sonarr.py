"""Sonarr: add a saved TV show to the download list."""
from __future__ import annotations

from ..config import Spec
from . import PluginError, register
from .arr import ArrPlugin, arr_specs

MONITOR = ("all", "future", "missing", "existing", "firstseason", "lastseason", "pilot", "none")  # settings choices are lowercase
API_MONITOR = {"firstseason": "firstSeason", "lastseason": "lastSeason"}


class Sonarr(ArrPlugin):
    id = "sonarr"
    prefix = "sonarr"
    label = "Sonarr"
    action_label = "Add to Sonarr"
    categories = ("tv_show",)
    specs = arr_specs("sonarr", "Sonarr", 8989, (
        Spec("sonarr_monitor", "SONARR_MONITOR", "choice", "all", "Sonarr", "Monitor",
             "Which episodes to download.", MONITOR),
        Spec("sonarr_search", "SONARR_SEARCH", "bool", True, "Sonarr", "Search right away",
             "Start looking for the missing episodes as soon as the show is added."),
    ))

    async def _find(self, item, settings, http) -> dict:
        tmdb, imdb, title, year = self.identify(item)
        terms = ([f"imdb:{imdb}"] if imdb else []) + ([f"tmdb:{tmdb}"] if tmdb else [])
        return await self.lookup(http, settings, "/series/lookup", terms, title, year)

    async def _existing(self, series, settings, http) -> dict | None:
        library = await self.call(http, settings, "GET", "/series") or []
        for s in library:
            if series.get("tvdbId") and s.get("tvdbId") == series["tvdbId"]:
                return s
            if series.get("titleSlug") and s.get("titleSlug") == series["titleSlug"]:
                return s
        return None

    def _link(self, settings, series) -> str:
        return f"{self.base_url(settings)}/series/{series.get('titleSlug')}"

    async def status(self, item, settings, http):
        series = await self._find(item, settings, http)
        if await self._existing(series, settings, http):
            return {"state": "added", "message": "In Sonarr", "url": self._link(settings, series)}
        return {"state": "available"}

    async def run(self, item, settings, http):
        series = await self._find(item, settings, http)
        name = f"{series.get('title')} ({series.get('year')})" if series.get("year") else series.get("title")
        if await self._existing(series, settings, http):
            return {"state": "added", "message": f"{name} is already in Sonarr", "url": self._link(settings, series)}
        search = self.with_search(settings)
        body = {k: v for k, v in series.items() if k != "id"}
        body.update(
            qualityProfileId=await self.quality_profile_id(http, settings),
            rootFolderPath=await self.root_folder(http, settings),
            monitored=True, seasonFolder=True,
            addOptions={"monitor": API_MONITOR.get(self.cfg(settings, "monitor"), self.cfg(settings, "monitor")), "searchForMissingEpisodes": search,
                        "searchForCutoffUnmetEpisodes": False},
        )
        try:  # Sonarr v3 also wants a language profile; v4 has none and answers 404
            languages = await self.call(http, settings, "GET", "/languageprofile")
            if languages:
                body["languageProfileId"] = languages[0]["id"]
        except PluginError:
            pass
        await self.call(http, settings, "POST", "/series", json=body)
        return {"state": "added", "message": f"Added {name} to Sonarr" + (" and started searching" if search else ""),
                "url": self._link(settings, series)}


register(Sonarr())
