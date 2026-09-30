import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from magpie.analyzer import SAVE_TOOL, AnalysisError, ScreenshotAnalyzer, prepare_image
from magpie.config import Settings
from magpie.db import Database, fts_query
from magpie.enrich import Page, enrich_screen, iso_duration, recipe_from_page, run_enrichers
from magpie.main import create_app
from magpie.pipeline import merge


def blank_details(**overrides):
    details = {k: ([] if v.get("type") == "array" else None) for k, v in SAVE_TOOL["input_schema"]["properties"]["details"]["properties"].items()}
    details.update(overrides)
    return details


def analysis(**overrides):
    base = {
        "category": "github_repo",
        "source_platform": "facebook",
        "title": "astral-sh/uv",
        "subtitle": None,
        "year": None,
        "summary": "An extremely fast Python package and project manager written in Rust.",
        "canonical_url": "https://github.com/astral-sh/uv",
        "image_url": None,
        "links": [],
        "tags": ["python", "Package Manager"],
        "screenshot_text": "You have to try uv, it replaced pip for me",
        "confidence": 92,
        "confidence_reason": "Repo name is legible and matches github.com/astral-sh/uv.",
        "alternatives": [],
        "details": blank_details(github_full_name="astral-sh/uv", posted_by="Some Dev"),
    }
    base.update(overrides)
    return base


def png_bytes(size=(40, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, "orange").save(buf, format="PNG")
    return buf.getvalue()


class FakeAnalyzer:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.corrections = []

    async def analyze(self, image, media_type, note=None, correction=None):
        self.calls.append((media_type, note))
        self.corrections.append(correction)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def mock_http(routes: dict):
    def handler(request: httpx.Request):
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response(request) if callable(response) else response
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


GITHUB_REPO = {
    "full_name": "astral-sh/uv", "html_url": "https://github.com/astral-sh/uv",
    "description": "An extremely fast Python package manager.", "stargazers_count": 70000,
    "forks_count": 2000, "open_issues_count": 1500, "language": "Rust", "topics": ["python", "packaging"],
    "license": {"spdx_id": "Apache-2.0"}, "homepage": "https://docs.astral.sh/uv", "pushed_at": "2026-09-01T00:00:00Z",
    "archived": False,
}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, tmdb_api_key=None, omdb_api_key=None, github_token=None)


def make_client(settings, result, routes=None):
    analyzer = FakeAnalyzer(result)
    app = create_app(settings, analyzer=analyzer, http=mock_http(routes or {}))
    return TestClient(app), analyzer


# ---- end-to-end through the API -------------------------------------------------


def test_upload_analyze_enrich_and_retrieve(settings):
    routes = {"https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json=GITHUB_REPO)}
    client, analyzer = make_client(settings, analysis(), routes)
    with client:
        r = client.post("/api/items", files={"file": ("shot.png", png_bytes(), "image/png")},
                        data={"note": "from Dana", "tags": "tools, to-try"})
        assert r.status_code == 202
        item_id = r.json()["id"]

        item = client.get(f"/api/items/{item_id}").json()
        assert item["status"] == "ready", item.get("error")
        assert item["category"] == "github_repo"
        assert item["source_platform"] == "facebook"
        assert item["canonical_url"] == "https://github.com/astral-sh/uv"
        assert item["image_url"] == "https://opengraph.githubassets.com/1/astral-sh/uv"
        assert item["metadata"]["stars"] == 70000
        assert item["metadata"]["programming_language"] == "Rust"
        assert item["metadata"]["posted_by"] == "Some Dev"
        assert item["metadata"]["sources"] == ["claude", "github"]
        assert {"python", "package-manager", "packaging", "rust", "tools", "to-try"} <= set(item["tags"])
        assert analyzer.calls == [("image/png", "from Dana")]

        # retrieval: full text (including text read from the screenshot), category, tags
        assert [i["id"] for i in client.get("/api/items", params={"q": "fast rust"}).json()] == [item_id]
        assert [i["id"] for i in client.get("/api/items", params={"q": "replaced pip"}).json()] == [item_id]
        assert client.get("/api/items", params={"q": "lasagna"}).json() == []
        assert len(client.get("/api/items", params={"category": "github_repo"}).json()) == 1
        assert client.get("/api/items", params={"category": "movie"}).json() == []
        assert len(client.get("/api/items", params=[("tag", "python"), ("tag", "to-try")]).json()) == 1
        assert client.get("/api/items", params=[("tag", "python"), ("tag", "nope")]).json() == []

        cats = client.get("/api/categories").json()
        assert cats["counts"] == [{"category": "github_repo", "count": 1}]
        assert {"tag": "python", "count": 1} in client.get("/api/tags").json()

        # editing
        r = client.patch(f"/api/items/{item_id}", json={"tags": ["cli", "#Must Try"], "note": "later"})
        assert r.json()["tags"] == ["cli", "must-try"]
        assert r.json()["note"] == "later"
        assert client.patch(f"/api/items/{item_id}", json={"category": "bogus"}).status_code == 422

        assert client.get(f"/media/{item['image_file']}").status_code == 200
        assert client.delete(f"/api/items/{item_id}").status_code == 204
        assert client.get(f"/api/items/{item_id}").status_code == 404
        assert client.get("/api/items", params={"q": "rust"}).json() == []


def test_failed_analysis_is_kept_and_can_be_retried(settings):
    client, analyzer = make_client(settings, AnalysisError("model declined"))
    with client:
        item_id = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}).json()["id"]
        item = client.get(f"/api/items/{item_id}").json()
        assert item["status"] == "error" and "declined" in item["error"]
        assert item["to_check"] is True       # a failed analysis needs a look
        assert [i["id"] for i in client.get("/api/items", params={"to_check": True}).json()] == [item_id]

        analyzer.result = analysis()
        client.post(f"/api/items/{item_id}/reanalyze")
        item = client.get(f"/api/items/{item_id}").json()
        assert item["status"] == "ready" and item["to_check"] is False    # solved by the retry


def test_rejects_non_images(settings):
    client, _ = make_client(settings, analysis())
    with client:
        r = client.post("/api/items", files={"file": ("a.txt", b"hello", "text/plain")})
        assert r.status_code == 415


def test_media_path_traversal_blocked(settings):
    client, _ = make_client(settings, analysis())
    with client:
        assert client.get("/media/..%2Fmagpie.db").status_code == 404


# ---- enrichment ------------------------------------------------------------------


async def test_movie_enrichment_uses_tmdb_and_omdb(settings):
    settings.tmdb_api_key = "abc123"
    settings.omdb_api_key = "omdbkey"
    routes = {
        "https://api.themoviedb.org/3/search/movie": httpx.Response(200, json={"results": [{"id": 27205}]}),
        "https://api.themoviedb.org/3/movie/27205": httpx.Response(200, json={
            "id": 27205, "vote_average": 8.37, "genres": [{"name": "Science Fiction"}], "runtime": 148,
            "overview": "A thief who steals corporate secrets...", "poster_path": "/poster.jpg", "release_date": "2010-07-15",
            "external_ids": {"imdb_id": "tt1375666"}, "production_companies": [{"name": "Legendary"}],
            "credits": {"cast": [{"name": "Leonardo DiCaprio"}], "crew": [{"name": "Christopher Nolan", "job": "Director"}]},
            "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "YoHD9XEInc0"}]},
            "watch/providers": {"results": {"US": {"link": "https://tmdb/watch", "flatrate": [{"provider_name": "Netflix"}]}}},
        }),
        "https://www.omdbapi.com/": httpx.Response(200, json={
            "Response": "True", "imdbRating": "8.8", "imdbVotes": "2,500,000", "Rated": "PG-13",
            "Ratings": [{"Source": "Rotten Tomatoes", "Value": "87%"}, {"Source": "Metacritic", "Value": "74/100"}],
        }),
    }
    a = analysis(category="movie", title="Inception", year=2010, canonical_url=None,
                 details=blank_details(), tags=["heist"])
    async with mock_http(routes) as http:
        e = await enrich_screen(a, settings, http)
    assert e.canonical_url == "https://www.imdb.com/title/tt1375666/"
    assert e.image_url == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert e.metadata["imdb_rating"] == "8.8/10"
    assert e.metadata["rotten_tomatoes"] == "87%"
    assert e.metadata["metacritic"] == "74/100"
    assert e.metadata["directors"] == ["Christopher Nolan"]
    assert e.metadata["runtime"] == "2h 28m"
    assert e.metadata["where_to_watch"] == ["Netflix"]
    assert {"label": "Trailer", "url": "https://www.youtube.com/watch?v=YoHD9XEInc0"} in e.links

    fields = merge(a, [e])
    assert fields["canonical_url"] == "https://www.imdb.com/title/tt1375666/"
    assert fields["metadata"]["description"].startswith("A thief")
    assert "science fiction" in fields["tags"]


async def test_movie_without_keys_keeps_claude_imdb_link(settings):
    a = analysis(category="movie", title="Inception", canonical_url="https://www.imdb.com/title/tt1375666/",
                 details=blank_details(imdb_id="tt1375666", imdb_rating="8.8/10"))
    async with mock_http({}) as http:
        enrichments = await run_enrichers(a, settings, http)
    fields = merge(a, enrichments)
    assert fields["canonical_url"] == "https://www.imdb.com/title/tt1375666/"
    assert fields["metadata"]["imdb_rating"] == "8.8/10"


RECIPE_HTML = """<html><head><title>Shakshuka</title>
<meta property="og:image" content="/img/og.jpg">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebPage"},
 {"@type":["Recipe"],"name":"Shakshuka","image":["/img/shakshuka.jpg"],"description":"Eggs poached in tomato sauce.",
  "recipeIngredient":["6 eggs","1  can tomatoes"],"totalTime":"PT35M","prepTime":"PT10M","recipeYield":["4","4 servings"],
  "recipeCuisine":"Middle Eastern","author":{"@type":"Person","name":"Yotam"},
  "aggregateRating":{"ratingValue":"4.84","ratingCount":"212"},
  "recipeInstructions":[{"@type":"HowToSection","itemListElement":[{"@type":"HowToStep","text":"Make the sauce."},{"@type":"HowToStep","text":"Add eggs."}]}]}]}
</script></head><body></body></html>"""


async def test_recipe_enrichment_from_json_ld(settings):
    routes = {"https://example.com/shakshuka": httpx.Response(200, text=RECIPE_HTML, headers={"content-type": "text/html"})}
    a = analysis(category="recipe", title="Shakshuka", canonical_url="https://example.com/shakshuka", details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.source == "schema.org/Recipe"
    assert e.image_url == "https://example.com/img/shakshuka.jpg"
    assert e.metadata["ingredients"] == ["6 eggs", "1 can tomatoes"]
    assert e.metadata["instructions"] == ["Make the sauce.", "Add eggs."]
    assert e.metadata["total_time"] == "35m"
    assert e.metadata["servings"] == "4 servings"
    assert e.metadata["rating"] == "4.8/5"
    assert e.metadata["author"] == "Yotam"


def test_recipe_parser_ignores_pages_without_recipes():
    assert recipe_from_page(Page(url="https://x", meta={}, ld=[{"@type": "Article"}], title="")) is None


async def test_generic_page_uses_opengraph(settings):
    html = '<html><head><meta property="og:title" content="Great read"><meta property="og:image" content="https://cdn/x.png"><meta name="description" content="About things"></head></html>'
    routes = {"https://blog.example/post": httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})}
    a = analysis(category="article", canonical_url="https://blog.example/post", details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.image_url == "https://cdn/x.png"
    assert e.metadata["page_description"] == "About things"


async def test_enricher_failures_are_swallowed(settings):
    def boom(request):
        raise httpx.ConnectError("down")
    a = analysis(category="github_repo")
    async with mock_http({"https://api.github.com": boom}) as http:
        assert await run_enrichers(a, settings, http) == []


# ---- helpers ---------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [("PT1H30M", "1h 30m"), ("PT45M", "45m"), ("P0DT2H", "2h"), (None, None), ("20 mins", "20 mins")])
def test_iso_duration(value, expected):
    assert iso_duration(value) == expected


def test_fts_query_is_injection_safe():
    assert fts_query('the "bear" OR -x*') == '"the"* "bear"* "OR"* "x"*'
    assert fts_query("!!!") is None
    db = Database(":memory:")
    db.create_item("a.png", note='weird "quotes" AND NOT')
    assert len(db.list_items(q='"quotes" AND NOT (')) == 1


def test_prepare_image_downscales_large_screenshots():
    data, media_type = prepare_image(png_bytes((1200, 3000)), "image/png")
    assert media_type == "image/jpeg"
    assert max(Image.open(io.BytesIO(data)).size) == 2000
    small = png_bytes()
    assert prepare_image(small, "image/png") == (small, "image/png")


# ---- analyzer loop against a fake Anthropic client -------------------------------


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return self.responses.pop(0)


def fake_client(responses):
    messages = FakeMessages(responses)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


def block(**kw):
    return SimpleNamespace(**kw)


def usage(inp, out, searches=0, fetches=0, cache_read=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cache_read,
                           cache_creation_input_tokens=0,
                           server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=fetches))


async def test_analyzer_resumes_pause_turn_and_returns_tool_input():
    result = analysis()
    client, messages = fake_client([
        SimpleNamespace(stop_reason="pause_turn", model="claude-opus-5", usage=usage(20_000, 1_000, searches=2, fetches=1),
                        content=[block(type="server_tool_use", id="s1", name="web_search", input={})]),
        SimpleNamespace(stop_reason="tool_use", model="claude-opus-5", usage=usage(30_000, 2_000, searches=1),
                        content=[block(type="tool_use", id="t1", name="save_analysis", input=result)]),
    ])
    out = await ScreenshotAnalyzer(client=client, model="claude-opus-5").analyze(png_bytes(), "image/png", note="hi")
    [run] = out.pop("_runs")
    assert out == result
    assert run["input_tokens"] == 50_000 and run["output_tokens"] == 3_000 and run["requests"] == 2
    assert run["web_searches"] == 3 and run["web_fetches"] == 1 and run["mode"] == "realtime"
    # 50k in x $5/M + 3k out x $25/M + 3 searches x $0.01
    assert run["cost_usd"] == pytest.approx(0.25 + 0.075 + 0.03)
    first, second = messages.requests
    assert first["model"] == "claude-opus-5"
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert {t["name"] for t in first["tools"]} == {"web_search", "web_fetch", "save_analysis"}
    fetch = next(t for t in first["tools"] if t["name"] == "web_fetch")
    assert fetch["max_content_tokens"] == 8000           # cost cap on fetched pages
    assert first["output_config"] == {"effort": "medium"}
    assert "hi" in first["messages"][0]["content"][1]["text"]
    assert second["messages"][-1]["role"] == "assistant"


async def test_analyzer_nudges_once_then_fails():
    client, messages = fake_client([
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="It's a movie.")]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="Still a movie.")]),
    ])
    with pytest.raises(AnalysisError) as err:
        await ScreenshotAnalyzer(client=client, model="claude-sonnet-5").analyze(png_bytes(), "image/png")
    assert "fallbacks" not in messages.requests[0]
    assert err.value.runs and err.value.runs[0]["requests"] == 2  # spend is kept even though it failed
    assert "save_analysis" in messages.requests[1]["messages"][-1]["content"]


async def test_analyzer_reports_refusals():
    client, _ = fake_client([SimpleNamespace(stop_reason="refusal", content=[])])
    with pytest.raises(AnalysisError):
        await ScreenshotAnalyzer(client=client).analyze(png_bytes(), "image/png")


# ---- self-hosting: sync + auth ---------------------------------------------------


def test_offline_client_upload_is_idempotent_and_syncs(settings):
    client, analyzer = make_client(settings, analysis())
    with client:
        start = client.get("/api/sync").json()
        assert start["items"] == [] and start["deleted"] == []
        cursor = start["server_time"]

        form = {"id": "ios-3f2a9c1b", "created_at": "2026-09-20T08:30:00Z", "note": "offline"}
        first = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}, data=form)
        again = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}, data=form)
        assert first.json()["id"] == again.json()["id"] == "ios-3f2a9c1b"
        assert len(analyzer.calls) == 1  # the retry did not trigger a second analysis
        assert first.json()["created_at"].startswith("2026-09-20T08:30:00")

        delta = client.get("/api/sync", params={"since": cursor}).json()
        assert [i["id"] for i in delta["items"]] == ["ios-3f2a9c1b"]
        assert delta["items"][0]["status"] == "ready"
        cursor = delta["server_time"]

        assert client.get("/api/sync", params={"since": cursor}).json()["items"] == []

        # tag edits bump updated_at so other devices pick them up
        client.patch("/api/items/ios-3f2a9c1b", json={"tags": ["x"]})
        delta = client.get("/api/sync", params={"since": cursor}).json()
        assert delta["items"][0]["tags"] == ["x"]
        cursor = delta["server_time"]

        client.delete("/api/items/ios-3f2a9c1b")
        delta = client.get("/api/sync", params={"since": cursor}).json()
        assert delta == {"server_time": delta["server_time"], "items": [], "deleted": ["ios-3f2a9c1b"]}

        # an offline client that still has the upload queued learns it was deleted
        gone = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}, data=form)
        assert gone.status_code == 410


def test_rejects_bad_client_ids(settings):
    client, _ = make_client(settings, analysis())
    with client:
        r = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}, data={"id": "../x"})
        assert r.status_code == 422


def test_api_token_required_when_configured(settings):
    settings.api_token = "s3cret"
    client, _ = make_client(settings, analysis())
    with client:
        assert client.get("/api/health").json()["auth_required"] is True
        assert client.get("/api/items").status_code == 401
        assert client.get("/api/items", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/api/items", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        client.cookies.set("magpie_token", "s3cret")
        assert client.get("/api/sync").status_code == 200
        assert client.get("/").status_code == 200  # the web UI shell itself is public


def test_several_api_tokens_are_each_accepted(settings):
    settings.api_token = "phone-tok, laptop-tok\nthird"
    client, _ = make_client(settings, analysis())
    with client:
        for tok in ("phone-tok", "laptop-tok", "third"):
            assert client.get("/api/items", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        assert client.get("/api/items", headers={"Authorization": "Bearer phone-tok,laptop-tok"}).status_code == 401
        assert client.get("/api/items", headers={"Authorization": "Bearer nope"}).status_code == 401


# ---- confidence & manual correction ---------------------------------------------


def test_confidence_and_alternatives_are_stored_and_low_confidence_is_flagged(settings):
    guess = analysis(
        category="movie", title="Dune", year=1984, canonical_url=None, confidence=45,
        confidence_reason="Only a desert still is visible; could be either adaptation.",
        alternatives=[{"title": "Dune: Part Two", "category": "movie", "year": 2024,
                       "canonical_url": "https://www.imdb.com/title/tt15239678/", "why": "Same visual style"}],
        details=blank_details(), tags=["sci-fi"],
    )
    client, _ = make_client(settings, guess)
    with client:
        item_id = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}).json()["id"]
        item = client.get(f"/api/items/{item_id}").json()
        assert item["confidence"] == 45
        assert item["confidence_reason"].startswith("Only a desert")
        assert item["alternatives"][0]["title"] == "Dune: Part Two"
        assert item["needs_review"] is True and item["corrected"] is False
        assert [i["id"] for i in client.get("/api/items", params={"needs_review": True}).json()] == [item_id]


def test_correct_with_facts_reenriches_without_the_model(settings):
    settings.tmdb_api_key = "k"
    routes = {
        "https://api.themoviedb.org/3/find/tt15239678": httpx.Response(200, json={"movie_results": [{"id": 693134}]}),
        "https://api.themoviedb.org/3/movie/693134": httpx.Response(200, json={
            "id": 693134, "vote_average": 8.2, "genres": [{"name": "Science Fiction"}], "overview": "Paul unites with the Fremen.",
            "poster_path": "/dune2.jpg", "external_ids": {"imdb_id": "tt15239678"}, "credits": {}, "videos": {}, "watch/providers": {},
        }),
    }
    wrong = analysis(category="movie", title="Dune", year=1984, canonical_url="https://www.imdb.com/title/tt0087182/",
                     confidence=45, details=blank_details(imdb_id="tt0087182", posted_by="Film Club"),
                     tags=["lynch", "cult-classic"], summary="David Lynch's 1984 adaptation.")
    client, analyzer = make_client(settings, wrong, routes)
    with client:
        item_id = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}, data={"tags": "watchlist"}).json()["id"]
        r = client.post(f"/api/items/{item_id}/correct", json={
            "title": "Dune: Part Two", "year": 2024, "canonical_url": "https://www.imdb.com/title/tt15239678/"})
        assert r.status_code == 202
        item = client.get(f"/api/items/{item_id}").json()

    assert len(analyzer.calls) == 1  # no second model call
    assert item["status"] == "ready"
    assert item["title"] == "Dune: Part Two"
    assert item["canonical_url"] == "https://www.imdb.com/title/tt15239678/"
    assert item["image_url"] == "https://image.tmdb.org/t/p/w500/dune2.jpg"
    assert item["summary"] == "Paul unites with the Fremen."          # the wrong film's summary is gone
    assert item["metadata"]["year"] == 2024
    assert item["metadata"]["posted_by"] == "Film Club"                # facts about the post survive
    assert "imdb_id" in item["metadata"] and item["metadata"]["imdb_id"] == "tt15239678"
    assert item["confidence"] == 100 and item["corrected"] is True and item["needs_review"] is False
    assert item["alternatives"] == []
    assert "watchlist" in item["tags"]                                  # the user's tag stays
    assert "lynch" not in item["tags"] and "science-fiction" in item["tags"]


def test_correct_with_hint_asks_the_model_again(settings):
    client, analyzer = make_client(settings, analysis(category="movie", title="The Office", confidence=50, details=blank_details()))
    with client:
        item_id = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}).json()["id"]
        analyzer.result = analysis(category="tv_show", title="The Office (US)", confidence=88, details=blank_details(),
                                   canonical_url=None)
        client.post(f"/api/items/{item_id}/correct", json={"hint": "It's the American TV series", "category": "tv_show"})
        item = client.get(f"/api/items/{item_id}").json()

    assert analyzer.corrections[1]["hint"] == "It's the American TV series"
    assert analyzer.corrections[1]["previous_title"] == "The Office"
    assert item["title"] == "The Office (US)" and item["category"] == "tv_show"
    assert item["corrected"] is True
    assert item["confidence"] == 100  # an explicit fact (category) was given


def test_correct_validates_input(settings):
    client, _ = make_client(settings, analysis())
    with client:
        item_id = client.post("/api/items", files={"file": ("s.png", png_bytes(), "image/png")}).json()["id"]
        assert client.post(f"/api/items/{item_id}/correct", json={}).status_code == 422
        assert client.post(f"/api/items/{item_id}/correct", json={"category": "nope"}).status_code == 422
        assert client.post(f"/api/items/{item_id}/correct", json={"canonical_url": "javascript:alert(1)"}).status_code == 422
        assert client.post("/api/items/missing/correct", json={"title": "x"}).status_code == 404


def test_correction_prompt_mentions_previous_and_new_facts():
    from magpie.analyzer import correction_prompt
    text = correction_prompt({"previous_title": "Dune", "previous_category": "movie", "year": 2024, "hint": "the sequel"})
    assert "Dune" in text and "year = 2024" in text and "the sequel" in text


def test_legacy_confidence_labels_map_to_scores():
    from magpie.pipeline import confidence_score
    assert confidence_score("high") == 90 and confidence_score("low") == 40
    assert confidence_score(140) == 100 and confidence_score(-3) == 0 and confidence_score(None) is None


def test_existing_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE items (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        status TEXT NOT NULL, error TEXT, image_file TEXT NOT NULL, note TEXT, category TEXT, source_platform TEXT,
        title TEXT, subtitle TEXT, summary TEXT, canonical_url TEXT, image_url TEXT,
        metadata TEXT NOT NULL DEFAULT '{}', links TEXT NOT NULL DEFAULT '[]', analysis TEXT)""")
    conn.execute("INSERT INTO items (id, created_at, updated_at, status, image_file) VALUES ('old1', 't', 't', 'ready', 'a.png')")
    conn.commit()
    conn.close()
    item = Database(path).get_item("old1")
    assert item["confidence"] is None and item["alternatives"] == [] and item["corrected"] is False


# ---- PWA -------------------------------------------------------------------------


def test_pwa_assets_are_served(settings):
    settings.api_token = "s3cret"  # the app shell must load before the user has entered a token
    client, _ = make_client(settings, analysis())
    with client:
        sw = client.get("/sw.js")
        assert sw.status_code == 200
        assert sw.headers["service-worker-allowed"] == "/"
        assert "no-cache" in sw.headers["cache-control"]
        assert "javascript" in sw.headers["content-type"]
        manifest = client.get("/static/manifest.webmanifest")
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert manifest.json()["display"] == "standalone"
        for icon in manifest.json()["icons"]:
            assert client.get(icon["src"]).status_code == 200
        assert client.get("/static/icons/apple-touch-icon.png").status_code == 200
        index = client.get("/").text
        assert 'rel="manifest"' in index and "apple-mobile-web-app-capable" in index
        assert client.post("/share-target", follow_redirects=False).status_code == 303


# ---- rename from Keeper -----------------------------------------------------------


def test_legacy_keeper_settings_database_and_cookie_still_work(tmp_path, monkeypatch):
    import importlib
    import sqlite3
    import magpie.config as config
    monkeypatch.setenv("KEEPER_EFFORT", "high")
    monkeypatch.delenv("MAGPIE_EFFORT", raising=False)
    importlib.reload(config)
    try:
        assert config.Settings(data_dir=tmp_path).effort == "high"
    finally:
        monkeypatch.delenv("MAGPIE_EFFORT", raising=False)
        importlib.reload(config)

    sqlite3.connect(tmp_path / "keeper.db").close()
    s = Settings(data_dir=tmp_path, api_token="s3cret")
    assert s.db_path.name == "keeper.db"

    client, _ = make_client(s, analysis())
    with client:
        client.cookies.set("keeper_token", "s3cret")
        assert client.get("/api/items").status_code == 200


def test_refresh_metadata_updates_facts_without_calling_the_model(settings):
    stars = {"n": 70000}
    routes = {"https://api.github.com/repos/astral-sh/uv":
              lambda request: httpx.Response(200, json={**GITHUB_REPO, "stargazers_count": stars["n"]})}
    client, analyzer = make_client(settings, analysis(), routes)
    with client:
        item_id = client.post("/api/items", files={"file": ("shot.png", png_bytes(), "image/png")}).json()["id"]
        client.patch(f"/api/items/{item_id}", json={"tags": ["mine"]})
        stars["n"] = 71234
        r = client.post(f"/api/items/{item_id}/refresh-metadata")
        assert r.status_code == 200
        item = r.json()
        assert item["metadata"]["stars"] == 71234 and item["image_url"].startswith("https://")
        assert item["title"] == "astral-sh/uv" and item["status"] == "ready" and "mine" in item["tags"]
        assert len(analyzer.calls) == 1   # only the original analysis
        assert client.post("/api/items/nope/refresh-metadata").status_code == 404


def test_unverified_items_are_flagged_and_filterable(settings):
    routes = {"https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json=GITHUB_REPO)}
    client, analyzer = make_client(settings, analysis(), routes)
    with client:
        good = client.post("/api/items", files={"file": ("a.png", png_bytes(), "image/png")}).json()["id"]
        analyzer.result = analysis(category="other", title="Mystery", canonical_url=None, details=blank_details(), confidence=75)
        odd = client.post("/api/items", files={"file": ("b.png", png_bytes((41, 60)), "image/png")}).json()["id"]
        assert client.get(f"/api/items/{good}").json()["verified"] is True    # confirmed by GitHub
        assert client.get(f"/api/items/{odd}").json()["verified"] is False    # only the model's word, and not sure
        analyzer.result = analysis(category="other", title="Sure thing", canonical_url=None, details=blank_details(), confidence=95)
        sure = client.post("/api/items", files={"file": ("c.png", png_bytes((42, 60)), "image/png")}).json()["id"]
        assert client.get(f"/api/items/{sure}").json()["verified"] is True    # a confident answer counts
        assert [i["id"] for i in client.get("/api/items", params={"unverified": True}).json()] == [odd]
        client.post(f"/api/items/{odd}/correct", json={"title": "Mystery Box"})
        assert client.get(f"/api/items/{odd}").json()["verified"] is True     # corrected by the user


def _png(size):
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", size, "teal").save(buf, format="PNG")
    return buf.getvalue()


async def test_article_uses_the_first_real_page_image(settings):
    import httpx
    html = ('<html><head><meta property="og:image" content="/missing.png"><link rel="image_src" href="/lead.png">'
            '<script type="application/ld+json">{"@type":"Article","image":["/lead.png"]}</script></head><body></body></html>')
    routes = {"https://blog.example/post": httpx.Response(200, text=html, headers={"content-type": "text/html"}),
              "https://blog.example/lead.png": httpx.Response(200, content=_png((900, 500)), headers={"content-type": "image/png"})}
    a = analysis(category="article", canonical_url="https://blog.example/post", details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.image_url == "https://blog.example/lead.png"


async def test_npm_package_gets_facts_and_the_repo_header(settings):
    import httpx
    registry = {"dist-tags": {"latest": "1.2.3"}, "description": "Tiny thing", "keywords": ["cli"], "maintainers": [{"name": "ann"}],
                "time": {"1.2.3": "2026-01-01T00:00:00Z"}, "homepage": "https://github.com/acme/tiny#readme",
                "repository": {"url": "git+https://github.com/acme/tiny.git"}, "versions": {"1.2.3": {"license": "MIT"}}}
    routes = {
        "https://registry.npmjs.org/tiny": httpx.Response(200, json=registry),
        "https://api.npmjs.org/downloads/point/last-week/tiny": httpx.Response(200, json={"downloads": 12345}),
        "https://api.github.com/repos/acme/tiny/readme": httpx.Response(200, text="<p align=center><img src=docs/logo.png></p>"),
        "https://raw.githubusercontent.com/acme/tiny/HEAD/docs/logo.png": httpx.Response(200, content=_png((400, 200)), headers={"content-type": "image/png"}),
    }
    a = analysis(category="app", title="tiny", canonical_url="https://www.npmjs.com/package/tiny", details=blank_details())
    async with mock_http(routes) as http:
        results = await run_enrichers(a, settings, http)
    npm = next(e for e in results if e.source == "npm")
    assert npm.image_url == "https://raw.githubusercontent.com/acme/tiny/HEAD/docs/logo.png"
    assert npm.metadata["version"] == "1.2.3" and npm.metadata["weekly_downloads"] == 12345 and npm.metadata["license"] == "MIT"
    assert {"npm", "Repository"} <= {l["label"] for l in npm.links}


async def test_github_prefers_the_maintainers_social_preview(settings):
    import httpx
    page = '<html><head><meta property="og:image" content="https://repository-images.githubusercontent.com/1/abc"></head></html>'
    routes = {
        "https://api.github.com/repos/astral-sh/uv/readme": httpx.Response(200, text="![logo](docs/logo.png)"),
        "https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json=GITHUB_REPO),
        "https://github.com/astral-sh/uv": httpx.Response(200, text=page, headers={"content-type": "text/html"}),
        "https://repository-images.githubusercontent.com/1/abc": httpx.Response(200, content=_png((1280, 640)), headers={"content-type": "image/png"}),
    }
    async with mock_http(routes) as http:
        [e] = await run_enrichers(analysis(), settings, http)
    assert e.image_url == "https://repository-images.githubusercontent.com/1/abc"


def test_verified_threshold_is_configurable_and_items_can_be_confirmed(settings):
    client, analyzer = make_client(settings, analysis(category="other", title="Maybe", canonical_url=None, details=blank_details(), confidence=75))
    with client:
        item_id = client.post("/api/items", files={"file": ("a.png", png_bytes(), "image/png")}).json()["id"]
        get = lambda: client.get(f"/api/items/{item_id}").json()
        assert get()["verified"] is False                       # 75 < the default 90
        assert client.get("/api/status").json()["verified_confidence"] == 90
        # the user's own threshold applies at once, and can't go below 60
        auth = {"X-Magpie-Setup-Code": client.app.state.runtime.setup_code}
        assert client.put("/api/settings", json={"changes": {"MAGPIE_VERIFIED_CONFIDENCE": "70"}}, headers=auth).status_code == 200
        assert get()["verified"] is True
        assert client.put("/api/settings", json={"changes": {"MAGPIE_VERIFIED_CONFIDENCE": "50"}}, headers=auth).status_code == 422
        assert client.put("/api/settings", json={"changes": {"MAGPIE_VERIFIED_CONFIDENCE": None}}, headers=auth).status_code == 200
        assert get()["verified"] is False
        # "Verify": the user confirms the identification
        assert client.patch(f"/api/items/{item_id}", json={"confirmed": True}).json()["verified"] is True
        assert client.get("/api/items", params={"unverified": True}).json() == []
        # a fresh analysis is a new identification: not confirmed any more
        client.post(f"/api/items/{item_id}/reanalyze")
        assert get()["confirmed"] is False and get()["verified"] is False


async def test_page_picture_falls_back_to_oembed_and_the_site_icon(settings):
    import httpx
    html = ('<html><head><meta property="og:image" content="/dead.png">'
            '<link rel="alternate" type="application/json+oembed" href="https://blog.example/oembed?u=1"></head></html>')
    routes = {"https://blog.example/post": httpx.Response(200, text=html, headers={"content-type": "text/html"}),
              "https://blog.example/oembed": httpx.Response(200, json={"thumbnail_url": "https://blog.example/thumb.png"}),
              "https://blog.example/thumb.png": httpx.Response(200, content=_png((640, 360)), headers={"content-type": "image/png"})}
    a = analysis(category="other", canonical_url="https://blog.example/post", details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.image_url == "https://blog.example/thumb.png"
    # a blocked page still yields the site's touch icon... and a YouTube video its thumbnail
    routes = {"https://www.youtube.com/watch?v=abcdefghijk": httpx.Response(403),
              "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg": httpx.Response(404),
              "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg": httpx.Response(200, content=_png((480, 360)), headers={"content-type": "image/png"})}
    a = analysis(category="video", canonical_url="https://www.youtube.com/watch?v=abcdefghijk", details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.image_url == "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"


def test_content_images_use_the_largest_srcset_and_lazy_sources():
    from magpie.images import content_images
    html = ('<img src="data:image/gif;base64,R0lG" data-src="/lazy.jpg"><img src="/small.jpg" srcset="/a-480.jpg 480w, /a-1200.jpg 1200w">'
            '<img src="/logo.png"><img src="/px.png" width="1" height="1">')
    assert content_images(html) == ["/lazy.jpg", "/a-1200.jpg"]


async def test_npm_monorepo_readme_and_shipped_logo(settings):
    import httpx
    registry = {"dist-tags": {"latest": "2.0.0"}, "readme": "![logo](./assets/logo.png)", "versions": {"2.0.0": {}},
                "repository": {"url": "git+https://github.com/acme/mono.git", "directory": "packages/pkg"}}
    routes = {
        "https://registry.npmjs.org/pkg": httpx.Response(200, json=registry),
        "https://raw.githubusercontent.com/acme/mono/HEAD/packages/pkg/assets/logo.png": httpx.Response(200, content=_png((300, 300)), headers={"content-type": "image/png"}),
    }
    a = analysis(category="app", canonical_url="https://www.npmjs.com/package/pkg", details=blank_details())
    async with mock_http(routes) as http:
        results = await run_enrichers(a, settings, http)
    assert next(e for e in results if e.source == "npm").image_url == "https://raw.githubusercontent.com/acme/mono/HEAD/packages/pkg/assets/logo.png"
    # a package without a repository: relative README images resolve against its files on the CDN, and shipped logos are found
    registry = {"dist-tags": {"latest": "1.0.0"}, "readme": "<img src='docs/hero.png'>", "versions": {"1.0.0": {}}}
    routes = {
        "https://registry.npmjs.org/solo": httpx.Response(200, json=registry),
        "https://data.jsdelivr.com/v1/package/npm/solo@1.0.0/flat": httpx.Response(200, json={"files": [{"name": "/dist/x.js"}, {"name": "/assets/logo.png"}]}),
        "https://cdn.jsdelivr.net/npm/solo@1.0.0/docs/hero.png": httpx.Response(404),
        "https://cdn.jsdelivr.net/npm/solo@1.0.0/assets/logo.png": httpx.Response(200, content=_png((256, 256)), headers={"content-type": "image/png"}),
    }
    a = analysis(category="app", canonical_url="https://www.npmjs.com/package/solo", details=blank_details())
    async with mock_http(routes) as http:
        results = await run_enrichers(a, settings, http)
    assert next(e for e in results if e.source == "npm").image_url == "https://cdn.jsdelivr.net/npm/solo@1.0.0/assets/logo.png"


async def test_hugging_face_model_gets_facts_and_a_card_picture(settings):
    import httpx
    from magpie.links import classify
    model = {"pipeline_tag": "text-generation", "library_name": "transformers", "downloads": 1234, "likes": 56,
             "tags": ["transformers", "safetensors", "license:apache-2.0", "region:us", "llama"], "lastModified": "2026-01-01T00:00:00Z",
             "cardData": {"license": "apache-2.0"}, "safetensors": {"total": 7_200_000_000}}
    routes = {
        "https://huggingface.co/api/models/acme/tiny-7b": httpx.Response(200, json=model),
        "https://huggingface.co/acme/tiny-7b/raw/main/README.md": httpx.Response(200, text="---\nlicense: apache-2.0\n---\n![banner](banner.png)"),
        "https://huggingface.co/acme/tiny-7b/resolve/main/banner.png": httpx.Response(200, content=_png((1200, 400)), headers={"content-type": "image/png"}),
    }
    a = analysis(category="app", canonical_url="https://huggingface.co/acme/tiny-7b", details=blank_details())
    async with mock_http(routes) as http:
        results = await run_enrichers(a, settings, http)
    hf = next(e for e in results if e.source == "huggingface")
    assert hf.image_url == "https://huggingface.co/acme/tiny-7b/resolve/main/banner.png"
    assert hf.metadata["task"] == "text-generation" and hf.metadata["license"] == "apache-2.0" and hf.metadata["parameters"] == "7.2B"
    assert "transformers" in hf.tags and not any(":" in t for t in hf.tags)
    # links to models, datasets and spaces are recognized without a model call
    assert classify("https://huggingface.co/acme/tiny-7b/tree/main", None)["title"] == "acme/tiny-7b"
    assert classify("https://huggingface.co/datasets/acme/words", None)["canonical_url"] == "https://huggingface.co/datasets/acme/words"
    assert classify("https://huggingface.co/spaces/acme/demo", None)["category"] == "app"
    assert classify("https://huggingface.co/docs/hub", None) is None


def _img(size=(800, 500)):
    return httpx.Response(200, content=_png(size), headers={"content-type": "image/png"})


def test_readme_blob_links_become_raw_and_badges_are_skipped():
    from magpie.images import readme_images
    md = ("[![npm](https://badgen.net/npm/v/x)](x) ![Build](https://circleci.com/gh/a/b.png)\n"
          "![logo](https://github.com/acme/tool/blob/main/media/logo.png?raw=true)")
    assert readme_images(md, "acme/tool") == ["https://raw.githubusercontent.com/acme/tool/main/media/logo.png"]


async def test_article_photo_beats_the_sites_default_share_image(settings):
    # the page's og:image is the site-wide default; the article's own photo (named "pixel-9-review") is the better cover
    html = ('<html><head><meta property="og:image" content="https://news.example/static/og-default.png"></head><body>'
            '<img src="https://news.example/img/pixel-9-review.jpg" width="1200" height="800"></body></html>')
    routes = {"https://news.example/pixel-9": httpx.Response(200, text=html, headers={"content-type": "text/html"}),
              "https://news.example/static/og-default.png": _img(), "https://news.example/img/pixel-9-review.jpg": _img()}
    a = analysis(category="article", canonical_url="https://news.example/pixel-9", details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.image_url == "https://news.example/img/pixel-9-review.jpg"


async def test_article_that_refuses_servers_uses_the_internet_archive_copy(settings):
    archived = '<html><head><meta property="og:image" content="https://cdn.blog.example/hero.jpg"></head></html>'
    routes = {"https://blog.example/post": httpx.Response(403),
              "https://archive.org/wayback/available": httpx.Response(200, json={"archived_snapshots": {"closest": {"available": True, "timestamp": "20260101000000"}}}),
              "https://web.archive.org/web/20260101000000id_/https://blog.example/post": httpx.Response(200, text=archived, headers={"content-type": "text/html"}),
              "https://cdn.blog.example/hero.jpg": _img()}
    a = analysis(category="article", canonical_url="https://blog.example/post", details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.image_url == "https://cdn.blog.example/hero.jpg"
    # a page that is merely missing (404) is not looked up elsewhere
    seen = []
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await run_enrichers(analysis(category="article", canonical_url="https://gone.example/x", details=blank_details()), settings, http)
    assert "archive.org" not in seen


async def test_npm_package_without_pictures_uses_the_maintainers_logo(settings):
    registry = {"dist-tags": {"latest": "7.0.0"}, "versions": {"7.0.0": {}}, "readme": "no pictures here",
                "repository": {"url": "git+https://github.com/babel/babel.git", "directory": "packages/babel-core"}}
    routes = {"https://registry.npmjs.org/@babel%2Fcore": httpx.Response(200, json=registry),
              "https://api.github.com/repos/babel/babel/readme": httpx.Response(200, text="![](https://img.shields.io/x.png)"),
              "https://avatars.githubusercontent.com/babel": _img((460, 460))}
    a = analysis(category="app", canonical_url="https://www.npmjs.com/package/@babel/core", details=blank_details())
    async with mock_http(routes) as http:
        results = await run_enrichers(a, settings, http)
    assert next(e for e in results if e.source == "npm").image_url == "https://avatars.githubusercontent.com/babel?s=460"


async def test_hugging_face_falls_back_to_the_owners_logo(settings):
    routes = {"https://huggingface.co/api/models/acme/bare": httpx.Response(200, json={"tags": []}),
              "https://huggingface.co/api/organizations/acme/avatar": httpx.Response(200, json={"avatarUrl": "https://cdn-avatars.huggingface.co/acme.png"}),
              "https://cdn-avatars.huggingface.co/acme.png": _img((300, 300))}
    a = analysis(category="app", canonical_url="https://huggingface.co/acme/bare", details=blank_details())
    async with mock_http(routes) as http:
        results = await run_enrichers(a, settings, http)
    assert next(e for e in results if e.source == "huggingface").image_url == "https://cdn-avatars.huggingface.co/acme.png"


async def test_book_cover_beats_the_shared_pages_picture_and_itunes_finds_album_art(settings):
    page = '<html><head><meta property="og:image" content="https://shop.example/banner.jpg"></head></html>'
    routes = {"https://shop.example/book": httpx.Response(200, text=page, headers={"content-type": "text/html"}),
              "https://shop.example/banner.jpg": _img((1200, 630)),
              "https://openlibrary.org/search.json": httpx.Response(200, json={"docs": [{"key": "/works/1", "title": "The Overstory", "cover_i": 42}]}),
              "https://covers.openlibrary.org/b/id/42-L.jpg": _img((400, 600))}
    a = analysis(category="book", title="The Overstory", canonical_url="https://shop.example/book", details=blank_details())
    async with mock_http(routes) as http:
        fields = merge(a, await run_enrichers(a, settings, http))
    assert fields["image_url"] == "https://covers.openlibrary.org/b/id/42-L.jpg"
    art = "https://is1-ssl.mzstatic.com/image/thumb/Music/ab/cd/100x100bb.jpg"
    routes = {"https://itunes.apple.com/search": httpx.Response(200, json={"results": [{"collectionName": "Selected Ambient Works 85-92", "artistName": "Aphex Twin", "artworkUrl100": art, "trackCount": 13}]}),
              "https://is1-ssl.mzstatic.com/image/thumb/Music/ab/cd/600x600bb.jpg": _img((600, 600))}
    a = analysis(category="music", title="Selected Ambient Works 85-92", canonical_url=None, details=blank_details())
    async with mock_http(routes) as http:
        [e] = await run_enrichers(a, settings, http)
    assert e.image_url.endswith("600x600bb.jpg") and e.metadata["tracks"] == 13


def test_a_dead_image_link_from_the_model_is_dropped(settings):
    client, _ = make_client(settings, analysis(category="other", canonical_url=None, details=blank_details(),
                                                image_url="https://made-up.example/poster.jpg"),
                            {"https://made-up.example/poster.jpg": httpx.Response(404)})
    with client:
        item_id = client.post("/api/items", files={"file": ("a.png", png_bytes(), "image/png")}).json()["id"]
        assert client.get(f"/api/items/{item_id}").json()["image_url"] is None


TITLE = "Two old GPUs I salvaged are doing more AI work than a brand new $2000 card, and I won't be upgrading anytime soon"


def _ddg(*urls):
    from urllib.parse import quote
    body = "".join(f'<a class="result__a" href="//duckduckgo.com/l/?uddg={quote(u, safe="")}&amp;rut=x">r</a>' for u in urls)
    return httpx.Response(200, text=f"<html><body>{body}</body></html>", headers={"content-type": "text/html"})


def _article_page(title):
    return httpx.Response(200, text=f'<html><head><title>{title} | XDA</title><meta property="og:title" content="{title}"></head></html>',
                          headers={"content-type": "text/html"})


async def test_invented_article_link_is_replaced_by_the_real_one(settings):
    from magpie.findlink import repair_link
    routes = {"https://www.xda-developers.com/two-old-gpus": httpx.Response(404),
              "https://html.duckduckgo.com/html/": _ddg("https://other.example/unrelated", "https://www.xda-developers.com/salvaged-gpus-beat-new-card/"),
              "https://other.example/unrelated": _article_page("A completely different story"),
              "https://www.xda-developers.com/salvaged-gpus-beat-new-card/": _article_page(TITLE)}
    a = analysis(category="article", title=TITLE, canonical_url="https://www.xda-developers.com/two-old-gpus", details=blank_details())
    async with mock_http(routes) as http:
        fixed = await repair_link(a, http)
    assert fixed["canonical_url"] == "https://www.xda-developers.com/salvaged-gpus-beat-new-card/"


async def test_no_link_is_better_than_a_dead_one(settings):
    from magpie.findlink import repair_link
    routes = {"https://www.xda-developers.com/gone": httpx.Response(404), "https://html.duckduckgo.com/html/": _ddg()}
    a = analysis(category="article", title=TITLE, canonical_url="https://www.xda-developers.com/gone", details=blank_details())
    async with mock_http(routes) as http:
        fixed = await repair_link(a, http)
    assert fixed["canonical_url"] is None and "doesn't work" in fixed["confidence_reason"]


async def test_working_blocked_and_shared_links_are_left_alone(settings):
    from magpie.findlink import repair_link
    asked = []
    def handler(request):
        asked.append(str(request.url))
        if request.url.host == "good.example":
            return _article_page(TITLE)
        return httpx.Response(403)   # a bot wall: we can't tell whether the link is right
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for url, extra in [("https://good.example/a", {}), ("https://walled.example/a", {}), ("https://shared.example/a", {"_analyzer": ["link"]})]:
            a = analysis(category="article", title=TITLE, canonical_url=url, details=blank_details(), **extra)
            assert (await repair_link(a, http))["canonical_url"] == url
    assert not any("duckduckgo" in u for u in asked)   # never searched


ARTICLE_WITH_LINKS = '''<html><head><title>uv replaced pip for me</title><meta property="og:title" content="uv replaced pip for me"></head><body>
<a href="/about">About us</a> <a href="https://www.xda-developers.com/other-post">More posts</a>
<a href="https://twitter.com/share?u=1">Tweet</a> <a href="https://www.facebook.com/sharer.php?u=1">Share</a>
<p>I've been using <a href="https://github.com/astral-sh/uv">uv</a>, from <a href="https://astral.sh">Astral</a>, and
<a href="https://docs.astral.sh/uv/guides/">the guide</a>. Also <a href="https://pypi.org/project/ruff/">ruff</a> and this
<a href="https://arxiv.org/pdf/2307.09288.pdf">paper</a>. <a href="https://apps.apple.com/us/app/some-app/id123?ign-mpt=uo%3D4">Some App</a></p>
</body></html>'''


def test_outbound_links_become_related_items():
    from magpie.enrich import Page
    from magpie.related import outbound_related
    page = Page(url="https://www.xda-developers.com/uv-replaced-pip/", meta={}, ld=[], title="", html=ARTICLE_WITH_LINKS)
    got = [(r["kind"], r["label"], r["url"]) for r in outbound_related(page)]
    assert got == [
        ("repo", "astral-sh/uv", "https://github.com/astral-sh/uv"),
        ("package", "ruff", "https://pypi.org/project/ruff/"),
        ("app", "Some App", "https://apps.apple.com/us/app/some-app/id123"),
        ("paper", "arXiv 2307.09288", "https://arxiv.org/abs/2307.09288"),
        ("docs", "the guide", "https://docs.astral.sh/uv/guides/"),
        ("site", "Astral", "https://astral.sh"),   # a named homepage; the share buttons and same-site links are not listed
    ]


async def test_article_item_lists_what_it_is_about_and_drops_the_dead_and_its_own_links(settings):
    routes = {
        "https://www.xda-developers.com/uv-replaced-pip/": httpx.Response(200, text=ARTICLE_WITH_LINKS, headers={"content-type": "text/html"}),
        "https://astral.sh": httpx.Response(200, text="<html><title>Astral</title></html>", headers={"content-type": "text/html"}),
        "https://made-up.example/": httpx.Response(404),
    }
    model = [{"kind": "company", "label": "Astral (the company)", "url": "https://astral.sh", "why": "makes uv"},
             {"kind": "site", "label": "Invented", "url": "https://made-up.example/", "why": ""},
             {"kind": "site", "label": "The article itself", "url": "https://www.xda-developers.com/uv-replaced-pip/", "why": ""}]
    client, _ = make_client(settings, analysis(category="article", title="uv replaced pip for me", confidence=95,
                                               canonical_url="https://www.xda-developers.com/uv-replaced-pip/",
                                               details=blank_details(), related=model), routes)
    with client:
        item_id = client.post("/api/items", files={"file": ("a.png", png_bytes(), "image/png")}).json()["id"]
        item = client.get(f"/api/items/{item_id}").json()
    related = {(r["kind"], r["url"]) for r in item["related"]}
    assert ("repo", "https://github.com/astral-sh/uv") in related              # from the article's own links
    assert ("company", "https://astral.sh") in related                          # from the model, and it opens
    assert not any("made-up.example" in u for _, u in related)                  # a made-up address is dropped
    assert not any(u.rstrip("/") == "https://www.xda-developers.com/uv-replaced-pip" for _, u in related)   # not the item itself
    assert len([1 for _, u in related if "astral.sh" in u and "docs" not in u]) == 1   # no duplicates (model + page)
    assert len(item["related"]) <= 8


async def test_github_npm_and_hugging_face_add_related_links(settings):
    repo = {**GITHUB_REPO, "owner": {"login": "astral-sh", "type": "Organization", "html_url": "https://github.com/astral-sh"},
            "parent": {"full_name": "someone/uv", "html_url": "https://github.com/someone/uv"}}
    async with mock_http({"https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json=repo)}) as http:
        [e] = await run_enrichers(analysis(), settings, http)
    assert {(r["kind"], r["url"]) for r in e.related} == {("company", "https://github.com/astral-sh"), ("repo", "https://github.com/someone/uv")}
    model = {"tags": ["arxiv:2307.09288", "license:mit"], "cardData": {"base_model": "meta-llama/Llama-2-7b"}}
    routes = {"https://huggingface.co/api/models/acme/tuned": httpx.Response(200, json=model)}
    a = analysis(category="app", canonical_url="https://huggingface.co/acme/tuned", details=blank_details())
    async with mock_http(routes) as http:
        results = await run_enrichers(a, settings, http)
    hf = next(e for e in results if e.source == "huggingface")
    assert {r["url"] for r in hf.related} == {"https://arxiv.org/abs/2307.09288", "https://huggingface.co/meta-llama/Llama-2-7b"}


def test_refresh_metadata_adds_related_links_without_losing_the_old_ones(settings):
    routes = {"https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json={**GITHUB_REPO,
              "owner": {"login": "astral-sh", "type": "Organization", "html_url": "https://github.com/astral-sh"}})}
    client, _ = make_client(settings, analysis(related=[{"kind": "docs", "label": "Docs", "url": "https://docs.astral.sh/uv/", "why": ""}]), routes)
    with client:
        item_id = client.post("/api/items", files={"file": ("a.png", png_bytes(), "image/png")}).json()["id"]
        before = {r["url"] for r in client.get(f"/api/items/{item_id}").json()["related"]}
        after = {r["url"] for r in client.post(f"/api/items/{item_id}/refresh-metadata").json()["related"]}
    assert "https://github.com/astral-sh" in before | after and before <= after
