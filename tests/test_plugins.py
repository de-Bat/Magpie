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

    def __init__(self, kind, lookup=None, library=None, key="secret-key", v4=True):
        self.kind, self.lookup, self.library, self.key, self.v4 = kind, lookup or [], library or [], key, v4
        self.posted, self.terms = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != self.key:
            return httpx.Response(401)
        path, k = request.url.path, self.kind
        if path == "/api/v3/system/status":
            return httpx.Response(200, json={"version": "5.0.0"})
        if path == "/api/v3/rootfolder":
            return httpx.Response(200, json=[{"path": "/media/a"}, {"path": "/media/b/"}])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 4, "name": "Any"}, {"id": 7, "name": "HD-1080p"}])
        if path == "/api/v3/languageprofile":
            return httpx.Response(404 if self.v4 else 200, json=[{"id": 1, "name": "English"}])
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
                            "quality_profiles": ["Any", "HD-1080p"]}
        assert client.post("/api/plugins/radarr/test", json={"url": RADARR}, headers=code).status_code == 400  # stored key is wrong
        # the stored key is never sent to a server it wasn't saved for
        r = client.post("/api/plugins/radarr/test", json={"url": "http://evil:7878"}, headers=code)
        assert r.status_code == 400 and "Enter the API key" in r.json()["detail"]
