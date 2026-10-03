"""Plugins: settings they contribute, the per-item actions, and the Radarr / Sonarr integrations."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from magpie.config import Settings
from magpie.main import create_app
from test_magpie import FakeAnalyzer, analysis, blank_details, png_bytes

RADARR = "http://radarr:7878"
SONARR = "http://sonarr:8989"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("RADARR_URL", "RADARR_API_KEY", "SONARR_URL", "SONARR_API_KEY", "MAGPIE_API_TOKEN",
                 "TMDB_API_KEY", "OMDB_API_KEY", "ANTHROPIC_API_KEY", "LOCAL_LLM_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAGPIE_OCR", "off")


class FakeArr:
    """A Radarr/Sonarr stand-in: serves the v3 endpoints the plugins use and records what was posted."""

    def __init__(self, kind, lookup=None, library=None, key="secret-key", v4=True, queue=None):
        self.kind, self.lookup, self.library, self.key, self.v4 = kind, lookup or [], library or [], key, v4
        self.queue = queue if queue is not None else []   # download queue entries; an Exception makes the endpoint fail
        self.posted, self.terms = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != self.key:
            return httpx.Response(401)
        path, k = request.url.path, self.kind
        if path == "/api/v3/system/status":
            return httpx.Response(200, json={"version": "5.0.0"})
        if path == "/api/v3/rootfolder":
            return httpx.Response(200, json=[{"path": "/media/a", "freeSpace": 500_000_000_000}, {"path": "/media/b/"}])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 4, "name": "Any"}, {"id": 7, "name": "HD-1080p"}])
        if path == "/api/v3/languageprofile":
            return httpx.Response(404 if self.v4 else 200, json=[{"id": 1, "name": "English"}])
        if path == "/api/v3/queue/details":
            if isinstance(self.queue, Exception):
                return httpx.Response(500)
            return httpx.Response(200, json=self.queue)
        if path == f"/api/v3/{k}/lookup":
            self.terms.append(request.url.params["term"])
            return httpx.Response(200, json=[r for r in self.lookup if self._matches(r, request.url.params["term"])])
        if path == f"/api/v3/{k}" and request.method == "GET":
            tmdb = request.url.params.get("tmdbId")
            return httpx.Response(200, json=[m for m in self.library if not tmdb or str(m.get("tmdbId")) == tmdb])
        if path == f"/api/v3/{k}" and request.method == "POST":
            self.posted.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(404)

    @staticmethod
    def _matches(result, term):
        if term.startswith("tmdb:"):
            return str(result.get("tmdbId")) == term[5:]
        if term.startswith("imdb:"):
            return result.get("imdbId") == term[5:]
        return term.split()[0].casefold() in result["title"].casefold()


FIGHT_CLUB = {"id": 0, "title": "Fight Club", "year": 1999, "tmdbId": 550, "imdbId": "tt0137523", "titleSlug": "550", "images": []}
SEVERANCE = {"id": 0, "title": "Severance", "year": 2022, "tvdbId": 371980, "imdbId": "tt11280740", "titleSlug": "severance", "seasons": []}


def make(tmp_path, *arrs, category="movie", title="Fight Club", year=1999, imdb="tt0137523", **settings):
    """An app whose HTTP traffic goes to the fake servers (by host); returns (client, item id)."""
    hosts = {"radarr": arrs and next((a for a in arrs if a.kind == "movie"), None),
             "sonarr": next((a for a in arrs if a.kind == "series"), None)}

    def route(request: httpx.Request):
        fake = hosts.get(request.url.host)
        return fake(request) if fake else httpx.Response(404)

    result = analysis(category=category, title=title, year=year, canonical_url=None, details=blank_details(imdb_id=imdb))
    s = Settings(data_dir=tmp_path, tmdb_api_key=None, omdb_api_key=None, github_token=None)
    s.apply_overrides({k: v for k, v in settings.items()})
    http = httpx.AsyncClient(transport=httpx.MockTransport(route))
    client = TestClient(create_app(s, analyzer=FakeAnalyzer(result), http=http))
    client.__enter__()
    r = client.post("/api/items", files={"file": ("shot.png", png_bytes(), "image/png")})
    item_id = r.json()["id"]
    assert client.get(f"/api/items/{item_id}").json()["status"] == "ready"
    return client, item_id


ARR_ON = {"RADARR_URL": RADARR, "RADARR_API_KEY": "secret-key", "SONARR_URL": SONARR, "SONARR_API_KEY": "secret-key"}


def test_plugin_settings_get_their_own_tabs_and_keys_stay_masked(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.apply_overrides({"RADARR_URL": RADARR, "RADARR_API_KEY": "abcdef123456"})
    with TestClient(create_app(s, http=httpx.AsyncClient())) as client:
        payload = client.get("/api/settings").json()
    groups = {g["name"]: {x["env"]: x for x in g["settings"]} for g in payload["groups"]}
    assert {"Radarr", "Sonarr"} <= set(groups)
    assert set(groups["Radarr"]) == {"RADARR_URL", "RADARR_API_KEY", "RADARR_ROOT_FOLDER", "RADARR_QUALITY_PROFILE", "RADARR_SEARCH"}
    assert groups["Radarr"]["RADARR_API_KEY"]["value"].endswith("3456") and "abcdef" not in json.dumps(payload)
    assert groups["Radarr"]["RADARR_URL"]["value"] == RADARR


def test_a_half_configured_plugin_is_reported(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.apply_overrides({"SONARR_URL": SONARR})
    keys = {p["key"] for p in s.problems() if p["level"] == "warning"}
    assert "SONARR_API_KEY" in keys and "RADARR_URL" not in keys


def test_changing_the_url_drops_the_saved_key(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.save_overrides({"RADARR_URL": RADARR, "RADARR_API_KEY": "abcdef123456"})
    notices = s.save_overrides({"RADARR_URL": "http://elsewhere:7878"})
    assert s.value("radarr_api_key") is None and notices
    s.save_overrides({"RADARR_URL": "http://elsewhere:7878", "RADARR_API_KEY": "new-key-123456"})
    assert s.save_overrides({"RADARR_URL": "http://elsewhere:7878/"}) == []  # same server: the key stays
    assert s.value("radarr_api_key") == "new-key-123456"


def test_nothing_is_offered_until_a_plugin_is_set_up(tmp_path):
    client, item = make(tmp_path)
    with client:
        assert client.get(f"/api/items/{item}/plugins").json() == []
        r = client.post(f"/api/items/{item}/plugins/radarr")
        assert r.status_code == 409 and "Settings" in r.json()["detail"]
        assert client.post(f"/api/items/{item}/plugins/nope").status_code == 404
        assert {p["id"]: p["configured"] for p in client.get("/api/plugins").json()} == {"radarr": False, "sonarr": False}


def test_a_film_is_offered_to_radarr_only_and_added_with_the_right_options(tmp_path):
    radarr = FakeArr("movie", lookup=[FIGHT_CLUB])
    client, item = make(tmp_path, radarr, **ARR_ON, RADARR_QUALITY_PROFILE="hd-1080p", RADARR_ROOT_FOLDER="/media/b")
    with client:
        offered = client.get(f"/api/items/{item}/plugins").json()
        assert [(p["id"], p["state"]) for p in offered] == [("radarr", "available")]
        assert offered[0]["action_label"] == "Add to Radarr"

        r = client.post(f"/api/items/{item}/plugins/radarr")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "added" and "Fight Club (1999)" in r.json()["message"]
        body = radarr.posted[0]
        assert body["tmdbId"] == 550 and "id" not in body
        assert body["qualityProfileId"] == 7 and body["rootFolderPath"] == "/media/b/"
        assert body["monitored"] is True and body["addOptions"] == {"searchForMovie": True}
        assert radarr.terms[0] == "imdb:tt0137523"  # no TMDB id saved, so it looks up by IMDb id

        radarr.library = [{**FIGHT_CLUB, "id": 12}]  # now it's in Radarr: don't add it twice
        assert client.get(f"/api/items/{item}/plugins").json()[0]["state"] == "added"
        again = client.post(f"/api/items/{item}/plugins/radarr").json()
        assert again["state"] == "added" and "already" in again["message"] and len(radarr.posted) == 1
        assert client.post(f"/api/items/{item}/plugins/sonarr").status_code == 422


def test_without_ids_radarr_matches_by_title_and_year_and_never_guesses(tmp_path):
    other = {**FIGHT_CLUB, "title": "Fight Club 2", "tmdbId": 1}
    radarr = FakeArr("movie", lookup=[other, FIGHT_CLUB])
    client, item = make(tmp_path, radarr, imdb=None, **ARR_ON)
    with client:
        assert client.post(f"/api/items/{item}/plugins/radarr").status_code == 200
        assert radarr.posted[0]["tmdbId"] == 550

    nothing = FakeArr("movie", lookup=[other])
    client, item = make(tmp_path / "b", nothing, imdb=None, **ARR_ON)
    with client:
        assert client.get(f"/api/items/{item}/plugins").json()[0]["state"] == "unavailable"
        r = client.post(f"/api/items/{item}/plugins/radarr")
        assert r.status_code == 404 and "Fight Club" in r.json()["detail"] and not nothing.posted


def test_a_series_is_added_to_sonarr(tmp_path):
    sonarr = FakeArr("series", lookup=[SEVERANCE])
    client, item = make(tmp_path, sonarr, category="tv_show", title="Severance", year=2022, imdb="tt11280740",
                        **ARR_ON, SONARR_MONITOR="firstseason", SONARR_SEARCH="false")
    with client:
        assert [p["id"] for p in client.get(f"/api/items/{item}/plugins").json()] == ["sonarr"]
        r = client.post(f"/api/items/{item}/plugins/sonarr")
        assert r.status_code == 200, r.text
        body = sonarr.posted[0]
        assert body["tvdbId"] == 371980 and body["seasonFolder"] is True and body["qualityProfileId"] == 4
        assert body["rootFolderPath"] == "/media/a"
        assert body["addOptions"] == {"monitor": "firstSeason", "searchForMissingEpisodes": False, "searchForCutoffUnmetEpisodes": False}
        assert "languageProfileId" not in body and r.json()["message"] == "Added Severance (2022) to Sonarr"

        sonarr.library = [{**SEVERANCE, "id": 3}]
        assert client.post(f"/api/items/{item}/plugins/sonarr").json()["state"] == "added"
        assert len(sonarr.posted) == 1


def test_sonarr_v3_gets_a_language_profile(tmp_path):
    sonarr = FakeArr("series", lookup=[SEVERANCE], v4=False)
    client, item = make(tmp_path, sonarr, category="tv_show", title="Severance", year=2022, imdb="tt11280740", **ARR_ON)
    with client:
        assert client.post(f"/api/items/{item}/plugins/sonarr").status_code == 200
        assert sonarr.posted[0]["languageProfileId"] == 1


def test_wrong_settings_give_readable_errors_and_never_leak_the_key(tmp_path):
    radarr = FakeArr("movie", lookup=[FIGHT_CLUB], key="the-real-key")
    client, item = make(tmp_path, radarr, **ARR_ON)  # settings carry a different key
    with client:
        r = client.post(f"/api/items/{item}/plugins/radarr")
        assert r.status_code == 400 and "API key" in r.json()["detail"] and "secret-key" not in r.text
        offered = client.get(f"/api/items/{item}/plugins").json()[0]
        assert offered["state"] == "unavailable" and "API key" in offered["message"]

    client, item = make(tmp_path / "b", radarr, RADARR_URL="ftp://radarr", RADARR_API_KEY="secret-key")
    with client:
        r = client.post(f"/api/items/{item}/plugins/radarr")
        assert r.status_code == 400 and "http" in r.json()["detail"]

    client, item = make(tmp_path / "c", FakeArr("movie", lookup=[FIGHT_CLUB]), **ARR_ON, RADARR_QUALITY_PROFILE="Ultra")
    with client:
        r = client.post(f"/api/items/{item}/plugins/radarr")
        assert r.status_code == 400 and "Ultra" in r.json()["detail"] and "HD-1080p" in r.json()["detail"]


def test_the_connection_test_uses_typed_values_and_guards_the_saved_key(tmp_path):
    radarr = FakeArr("movie", key="typed-key")
    client, _ = make(tmp_path, radarr, **ARR_ON)
    with client:
        code = {"X-Magpie-Setup-Code": client.app.state.runtime.setup_code}
        assert client.post("/api/plugins/radarr/test", json={}).status_code == 403  # same access rules as settings
        r = client.post("/api/plugins/radarr/test", json={"url": RADARR, "key": "typed-key"}, headers=code)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "message": "Connected to Radarr 5.0.0", "root_folders": ["/media/a", "/media/b/"],
                            "free_space": {"/media/a": 500_000_000_000}, "quality_profiles": ["Any", "HD-1080p"],
                            "quality_profile_ids": {"Any": 4, "HD-1080p": 7}}
        assert client.post("/api/plugins/radarr/test", json={"url": RADARR}, headers=code).status_code == 400  # stored key is wrong
        # the stored key is never sent to a server it wasn't saved for
        r = client.post("/api/plugins/radarr/test", json={"url": "http://evil:7878"}, headers=code)
        assert r.status_code == 400 and "Enter the API key" in r.json()["detail"]


# ---- download status after adding: downloaded / downloading (with a percentage) / pending / missing ----------------

from datetime import datetime, timedelta, timezone  # noqa: E402

LONG_AGO = "2025-01-01T00:00:00Z"


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def film(**over):
    return {**FIGHT_CLUB, "id": 12, "hasFile": False, "monitored": True, "isAvailable": True, "added": LONG_AGO, **over}


def radarr_download(tmp_path, entry, queue=None):
    radarr = FakeArr("movie", lookup=[FIGHT_CLUB], library=[entry], queue=queue)
    client, item = make(tmp_path, radarr, **ARR_ON)
    with client:
        offered = client.get(f"/api/items/{item}/plugins").json()[0]
    assert offered["state"] == "added" and offered["message"] == "In Radarr"
    return offered["download"]


def test_radarr_shows_a_finished_download_with_its_quality_and_size(tmp_path):
    d = radarr_download(tmp_path, film(hasFile=True, movieFile={"size": 8_400_000_000, "quality": {"quality": {"name": "Bluray-1080p"}}}))
    assert (d["state"], d["label"], d["detail"]) == ("downloaded", "Downloaded", "Bluray-1080p · 8.4 GB")


def test_radarr_shows_progress_while_downloading(tmp_path):
    d = radarr_download(tmp_path, film(), queue=[{"status": "downloading", "size": 1000, "sizeleft": 580, "timeleft": "00:12:30"}])
    assert (d["state"], d["percent"], d["label"]) == ("downloading", 42, "Downloading 42%") and "min left" in d["eta"]
    d = radarr_download(tmp_path / "b", film(), queue=[{"status": "downloading", "size": 0, "sizeleft": 0}])
    assert d["state"] == "downloading" and d["percent"] == 0   # no size reported yet: no division by zero


def test_radarr_queue_entries_that_are_not_downloading_are_pending_or_failed(tmp_path):
    cases = [("queued", "pending", "Queued"), ("paused", "pending", "Paused"), ("delay", "pending", "Waiting (delay profile)"),
             ("completed", "pending", "Importing"), ("downloadClientUnavailable", "pending", "Download client unavailable"),
             ("failed", "missing", "Download failed")]
    for i, (status, state, label) in enumerate(cases):
        d = radarr_download(tmp_path / str(i), film(), queue=[{"status": status, "size": 10, "sizeleft": 10, "errorMessage": "disk full" if status == "failed" else None}])
        assert (d["state"], d["label"]) == (state, label), status
        assert "percent" not in d
    assert d["detail"] == "disk full"


def test_radarr_not_downloading_is_pending_while_it_searches_or_waits_for_the_release_and_missing_otherwise(tmp_path):
    assert radarr_download(tmp_path / "a", film(added=ago(2)))["label"] == "Searching"
    d = radarr_download(tmp_path / "b", film(isAvailable=False))
    assert (d["state"], d["label"]) == ("pending", "Not released yet")
    d = radarr_download(tmp_path / "c", film())
    assert (d["state"], d["label"], d["detail"]) == ("missing", "Missing", "No release found yet")
    d = radarr_download(tmp_path / "d", film(monitored=False))
    assert (d["state"], d["detail"]) == ("missing", "Not monitored in Radarr")


def test_a_queue_that_cannot_be_read_does_not_break_the_status(tmp_path):
    d = radarr_download(tmp_path, film(), queue=RuntimeError("down"))
    assert d["state"] == "missing"


def series(have, total, **over):
    return {**SEVERANCE, "id": 3, "monitored": True, "added": LONG_AGO, "statistics": {"episodeFileCount": have, "episodeCount": total}, **over}


def sonarr_download(tmp_path, entry, queue=None):
    sonarr = FakeArr("series", lookup=[SEVERANCE], library=[entry], queue=queue)
    client, item = make(tmp_path, sonarr, category="tv_show", title="Severance", year=2022, imdb="tt11280740", **ARR_ON)
    with client:
        offered = client.get(f"/api/items/{item}/plugins").json()[0]
    assert offered["state"] == "added" and offered["message"] == "In Sonarr"
    return offered["download"]


def test_sonarr_summarises_the_episodes(tmp_path):
    d = sonarr_download(tmp_path / "a", series(10, 10))
    assert (d["state"], d["label"], d["have"], d["total"], d["detail"]) == ("downloaded", "Downloaded", 10, 10, "10 episodes on disk")
    d = sonarr_download(tmp_path / "b", series(7, 10))
    assert (d["state"], d["label"], d["have"], d["total"], d["detail"]) == ("missing", "Missing", 7, 10, "7 of 10 episodes on disk")
    d = sonarr_download(tmp_path / "c", series(0, 0))
    assert (d["state"], d["label"]) == ("pending", "Not aired yet")
    assert sonarr_download(tmp_path / "d", series(0, 0, added=ago(1)))["label"] == "Searching"
    assert sonarr_download(tmp_path / "e", series(2, 8, monitored=False))["detail"] == "Not monitored in Sonarr"


def test_sonarr_adds_up_the_episodes_that_are_downloading(tmp_path):
    queue = [{"status": "downloading", "size": 1000, "sizeleft": 500, "timeleft": "00:30:00"},
             {"status": "downloading", "size": 1000, "sizeleft": 0, "timeleft": "00:00:10"},
             {"status": "queued", "size": 1000, "sizeleft": 1000}]
    d = sonarr_download(tmp_path, series(4, 10), queue=queue)
    assert (d["state"], d["percent"], d["label"], d["detail"]) == ("downloading", 75, "Downloading 75%", "2 episodes downloading")
    assert "30 min left" in d["eta"] and (d["have"], d["total"]) == (4, 10)
    d = sonarr_download(tmp_path / "q", series(4, 10), queue=[{"status": "queued", "size": 10, "sizeleft": 10}])
    assert (d["state"], d["label"], d["detail"]) == ("pending", "Queued", "4 of 10 episodes on disk")


def test_adding_reports_that_it_is_searching(tmp_path):
    radarr = FakeArr("movie", lookup=[FIGHT_CLUB])
    sonarr = FakeArr("series", lookup=[SEVERANCE])
    client, item = make(tmp_path, radarr, **ARR_ON)
    with client:
        r = client.post(f"/api/items/{item}/plugins/radarr").json()
        assert r["download"] == {"state": "pending", "label": "Searching", "detail": "Radarr is looking for a release"}
    client, item = make(tmp_path / "s", sonarr, category="tv_show", title="Severance", year=2022, imdb="tt11280740", **ARR_ON)
    with client:
        r = client.post(f"/api/items/{item}/plugins/sonarr").json()
        assert (r["download"]["state"], r["download"]["label"]) == ("pending", "Searching")


def section_of(tmp_path, kind, library, lookup, **card):
    arr = FakeArr(kind, lookup=lookup, library=library)
    client, item = make(tmp_path, arr, **card, **ARR_ON)
    with client:
        return client.get(f"/api/items/{item}/plugins").json()[0]


def test_radarr_section_shows_status_and_when_the_digital_release_is_due(tmp_path):
    dates = {"digitalRelease": "2026-11-02T00:00:00Z", "inCinemas": "2026-08-01T00:00:00Z", "physicalRelease": "0001-01-01T00:00:00Z"}
    got = section_of(tmp_path, "movie", [film(**dates, isAvailable=False)], [FIGHT_CLUB])
    sec = got["section"]
    assert sec["title"] == "Radarr" and sec["download"]["label"] == "Not released yet"
    assert sec["rows"] == [{"label": "In cinemas", "date": "2026-08-01"}, {"label": "Digital release", "date": "2026-11-02"},
                           {"label": "Physical release", "value": "Not announced yet"}]   # a date nobody has set yet is said so


def test_radarr_section_is_there_before_the_film_is_added_with_the_release_dates_from_the_lookup(tmp_path):
    got = section_of(tmp_path, "movie", [], [{**FIGHT_CLUB, "digitalRelease": "2026-11-02T00:00:00Z"}])
    assert got["state"] == "available" and got["section"]["download"] is None
    assert got["section"]["rows"] == [{"label": "Digital release", "date": "2026-11-02"}, {"label": "Physical release", "value": "Not announced yet"}]


def test_sonarr_section_shows_the_next_episode_and_show_status(tmp_path):
    entry = series(3, 10, nextAiring="2026-10-12T01:00:00Z", previousAiring="2026-10-05T01:00:00Z", status="continuing")
    got = section_of(tmp_path, "series", [entry], [SEVERANCE], category="tv_show", title="Severance", year=2022, imdb="tt11280740")
    assert got["section"]["title"] == "Sonarr" and got["section"]["download"]["state"] == "missing"
    assert got["section"]["rows"] == [{"label": "Next episode", "date": "2026-10-12"}, {"label": "Last aired", "date": "2026-10-05"},
                                      {"label": "Show status", "value": "Continuing"}]
