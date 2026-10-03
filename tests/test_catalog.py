import httpx

from magpie.config import Settings

from test_magpie import make_client, settings  # noqa: F401  (the fixture)

TMDB_SEARCH = {"results": [
    {"id": 603, "title": "The Matrix", "release_date": "1999-03-30", "overview": "A hacker learns the truth.", "poster_path": "/m.jpg"},
    {"id": 604, "title": "The Matrix Reloaded", "release_date": "2003-05-15", "overview": "", "poster_path": None},
]}
TMDB_MOVIE = {"id": 603, "title": "The Matrix", "release_date": "1999-03-30", "overview": "A hacker learns the truth.",
              "poster_path": "/m.jpg", "genres": [{"name": "Action"}], "vote_average": 8.2, "runtime": 136}
OPEN_LIBRARY = {"docs": [
    {"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"], "first_publish_year": 1965, "cover_i": 7},
    {"key": "/works/OL2W", "title": "Dune Messiah", "author_name": ["Frank Herbert"], "first_publish_year": 1969},
]}


def keyed(tmp_path):
    return Settings(data_dir=tmp_path, tmdb_api_key="k" * 32, omdb_api_key=None, github_token=None)


def test_movie_search_lists_candidates_with_ids(tmp_path):
    routes = {"https://api.themoviedb.org/3/search/movie": httpx.Response(200, json=TMDB_SEARCH)}
    client, _ = make_client(keyed(tmp_path), {}, routes)
    with client:
        r = client.get("/api/catalog/search", params={"kind": "movie", "q": "matrix"}).json()
    assert [x["title"] for x in r["results"]] == ["The Matrix", "The Matrix Reloaded"]
    first = r["results"][0]
    assert first["year"] == 1999 and first["tmdb"] == ["movie", 603] and first["image_url"].endswith("/w185/m.jpg")
    assert first["subtitle"] == "Movie · 1999"


def test_book_search_needs_no_key(settings):
    routes = {"https://openlibrary.org/search.json": httpx.Response(200, json=OPEN_LIBRARY)}
    client, _ = make_client(settings, {}, routes)
    with client:
        r = client.get("/api/catalog/search", params={"kind": "book", "q": "dune"}).json()
    assert [x["title"] for x in r["results"]] == ["Dune", "Dune Messiah"]
    assert r["results"][0]["author"] == "Frank Herbert" and r["results"][0]["image_url"].endswith("/7-M.jpg")


def test_movie_search_without_a_key_says_so(settings):
    client, _ = make_client(settings, {})
    with client:
        r = client.get("/api/catalog/search", params={"kind": "movie", "q": "matrix"}).json()
        assert client.get("/api/catalog/search", params={"kind": "song", "q": "x"}).status_code == 422
    assert r["results"] == [] and "TMDB" in r["note"]


def test_a_lookup_failure_does_not_block_adding_it_as_typed(tmp_path):
    client, _ = make_client(keyed(tmp_path), {}, {"https://api.themoviedb.org/": httpx.Response(500)})
    with client:
        r = client.get("/api/catalog/search", params={"kind": "tv_show", "q": "x"}).json()
    assert r["results"] == [] and "as typed" in r["note"]


def test_adding_a_picked_movie_looks_up_that_exact_one(tmp_path):
    routes = {
        "https://api.themoviedb.org/3/movie/603": httpx.Response(200, json=TMDB_MOVIE),
        "https://api.themoviedb.org/3/search/movie": httpx.Response(200, json={"results": [{"id": 1}]}),  # must not be used
    }
    client, analyzer = make_client(keyed(tmp_path), {}, routes)
    with client:
        r = client.post("/api/items/entry", json={"id": "entry-0001", "category": "movie", "title": "The Matrix", "year": 1999,
                                                   "tmdb": ["movie", 603], "tags": ["scifi"]})
        assert r.status_code == 202 and r.json()["kind"] == "entry"
        item = client.get("/api/items/entry-0001").json()
        again = client.post("/api/items/entry", json={"id": "entry-0001", "category": "movie", "title": "x"}).json()
    assert item["status"] == "ready", item.get("error")
    assert item["title"] == "The Matrix" and item["category"] == "movie" and item["metadata"]["tmdb_id"] == 603
    assert "scifi" in item["tags"] and again["id"] == "entry-0001"
    assert analyzer.calls == []   # no model, no screenshot


def test_adding_a_book_by_title_and_author(settings):
    routes = {"https://openlibrary.org/search.json": httpx.Response(200, json=OPEN_LIBRARY),
              "https://covers.openlibrary.org/": httpx.Response(404)}
    client, analyzer = make_client(settings, {}, routes)
    with client:
        r = client.post("/api/items/entry", json={"category": "book", "title": "Dune", "author": "Frank Herbert"})
        item = client.get(f"/api/items/{r.json()['id']}").json()
        redo = client.post(f"/api/items/{item['id']}/reanalyze")
        item2 = client.get(f"/api/items/{item['id']}").json()
    assert item["status"] == "ready" and item["metadata"]["author"] == "Frank Herbert"
    assert redo.status_code == 202 and item2["status"] == "ready" and item2["title"] == "Dune"
    assert analyzer.calls == []


def test_entry_validation(settings):
    client, _ = make_client(settings, {})
    with client:
        assert client.post("/api/items/entry", json={"category": "recipe", "title": "x"}).status_code == 422
        assert client.post("/api/items/entry", json={"category": "book", "title": "  "}).status_code == 422
        assert client.post("/api/items/entry", json={"category": "book", "title": "x", "id": "no"}).status_code == 422
