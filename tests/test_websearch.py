"""Finding an article's address: the chosen search API, DuckDuckGo's cooldown, Bing's junk, and the logo picked for a site."""

import base64
import io
import json
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from PIL import Image

from magpie import findlink, images, websearch
from magpie.enrich import Page, page_picture
from magpie.findlink import find_article_url, search_results

TITLE = "Two old GPUs I salvaged are doing more AI work than a brand new card"
REAL = "https://www.xda-developers.com/salvaged-gpus-ai-work/"


def mock_http(routes: dict, seen: list | None = None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response(request) if callable(response) else response
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def article(title: str = TITLE):
    return httpx.Response(200, text=f'<html><head><title>{title}</title><meta property="og:title" content="{title}"></head></html>',
                          headers={"content-type": "text/html"})


def use(**kw):
    websearch.configure(SimpleNamespace(**{"search_provider": "none", "brave_search_api_key": None, "tavily_api_key": None,
                                           "serper_api_key": None, "serpapi_api_key": None, "searxng_url": None, **kw}))


@pytest.mark.parametrize("provider, settings, route, answer", [
    ("brave", {"brave_search_api_key": "k"}, "https://api.search.brave.com/", {"web": {"results": [{"url": REAL, "title": TITLE}]}}),
    ("tavily", {"tavily_api_key": "k"}, "https://api.tavily.com/search", {"results": [{"url": REAL, "title": TITLE}]}),
    ("serper", {"serper_api_key": "k"}, "https://google.serper.dev/search", {"organic": [{"link": REAL, "title": TITLE}]}),
    ("serpapi", {"serpapi_api_key": "k"}, "https://serpapi.com/search.json", {"organic_results": [{"link": REAL, "title": TITLE}]}),
    ("searxng", {"searxng_url": "http://searxng:8080/"}, "http://searxng:8080/search", {"results": [{"url": REAL, "title": TITLE}]}),
])
async def test_the_chosen_search_api_is_asked_first(provider, settings, route, answer):
    use(search_provider=provider, **settings)
    seen: list = []
    async with mock_http({route: httpx.Response(200, json=answer)}, seen) as http:
        hits = await search_results(f'"{TITLE}"', http)
    assert [h.url for h in hits] == [REAL]
    assert len(seen) == 1   # no DuckDuckGo
    if provider == "searxng":
        assert seen[0].url.params["format"] == "json"


async def test_a_provider_without_its_key_is_skipped_for_duckduckgo():
    use(search_provider="brave")
    ddg = f'<a class="result__a" href="//duckduckgo.com/l/?uddg={quote(REAL, safe="")}&amp;rut=x">{TITLE}</a>'
    async with mock_http({"https://html.duckduckgo.com/html/": httpx.Response(200, text=ddg)}) as http:
        assert [h.url for h in await search_results(TITLE, http)] == [REAL]


async def test_a_failing_provider_falls_back_to_duckduckgo():
    use(search_provider="serper", serper_api_key="k")
    ddg = f'<a class="result__a" href="//duckduckgo.com/l/?uddg={quote(REAL, safe="")}&amp;rut=x">{TITLE}</a>'
    trace: list = []
    async with mock_http({"https://google.serper.dev/": httpx.Response(403),
                          "https://html.duckduckgo.com/html/": httpx.Response(200, text=ddg)}) as http:
        assert [h.url for h in await search_results(TITLE, http, trace=trace)] == [REAL]
    assert trace[0]["engines"]["serper"] == "refused (HTTP 403)"


def bing_page(*pairs):
    items = "".join(f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1{base64.urlsafe_b64encode(u.encode()).decode().rstrip("=")}">'
                    f'{t}</a></h2></li>' for u, t in pairs)
    return httpx.Response(200, text=f"<ol>{items}</ol>")


async def test_bings_page_for_bots_is_recognized_and_duckduckgo_rests_after_refusing():
    """Bing answered "5 old GPUs ..." with sport5.co.il; DuckDuckGo's 202 means: stop asking for a while."""
    seen: list = []
    routes = {"https://html.duckduckgo.com/html/": httpx.Response(202),
              "https://www.bing.com/search": bing_page(("https://m.sport5.co.il/", "Sport5"), ("https://en.wikipedia.org/wiki/5", "5 - Wikipedia"))}
    trace: list = []
    async with mock_http(routes, seen) as http:
        assert await find_article_url(TITLE, None, http, "XDA") is None
        assert await search_results(TITLE, http, trace=trace) == []
    assert trace[0]["engines"]["duckduckgo"].startswith("skipped")
    assert trace[0]["engines"]["bing"].startswith("unrelated results only")
    assert trace[0]["blocked"]
    # the first query was refused everywhere: the remaining ones weren't sent
    assert sum("duckduckgo" in str(r.url) for r in seen) == 1


async def test_the_models_own_source_on_the_site_is_kept_when_the_site_walls_us_off():
    """Gemini names its sources by domain; XDA's bot wall drops the connection. The slug alone is a partial fit."""
    def walled(request):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
    async with mock_http({REAL: walled, "https://html.duckduckgo.com/html/": httpx.Response(200, text="")}) as http:
        found = await find_article_url(TITLE, None, http, "XDA", sources=[REAL], source_titles={REAL: "xda-developers.com"})
    assert found == REAL


async def test_a_page_found_by_search_still_needs_a_good_fit_when_walled_off():
    def walled(request):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
    ddg = f'<a class="result__a" href="//duckduckgo.com/l/?uddg={quote(REAL, safe="")}&amp;rut=x">xda-developers.com</a>'
    async with mock_http({REAL: walled, "https://html.duckduckgo.com/html/": httpx.Response(200, text=ddg)}) as http:
        assert await find_article_url(TITLE, None, http, "XDA") is None


def png(size):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "image/png"})


async def test_a_square_icon_beats_the_publishers_wide_wordmark():
    ld = {"@type": "NewsArticle", "publisher": {"@type": "Organization", "logo": {"url": "https://site.example/wordmark.png"}}}
    page = Page(url="https://site.example/post", meta={}, ld=[ld], title="", html="",
                links=[{"rel": "manifest", "href": "/manifest.json"}])
    manifest = {"icons": [{"src": "/icon-512.png", "sizes": "512x512"}]}
    routes = {"https://site.example/wordmark.png": png((600, 120)), "https://site.example/icon-512.png": png((512, 512)),
              "https://site.example/manifest.json": httpx.Response(200, text=json.dumps(manifest))}
    async with mock_http(routes) as http:
        assert await page_picture(http, page) == ("https://site.example/icon-512.png", "logo")


async def test_a_wide_wordmark_alone_is_not_used_as_the_logo():
    ld = {"@type": "NewsArticle", "publisher": {"@type": "Organization", "logo": "https://site.example/wordmark.png"}}
    page = Page(url="https://site.example/post", meta={}, ld=[ld], title="", html="")
    async with mock_http({"https://site.example/wordmark.png": png((600, 120))}) as http:
        picture, kind = await page_picture(http, page)
    assert kind != "logo"


SQUARE_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
              '<circle cx="100" cy="100" r="90" fill="#c33"/></svg>')
WIDE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"><rect width="600" height="100" fill="#333"/></svg>'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_store", tmp_path)
    return tmp_path


def svg(body: str):
    return httpx.Response(200, text=body, headers={"content-type": "image/svg+xml"})


async def test_an_svg_logo_is_drawn_to_a_png_the_server_keeps(store):
    url = "https://project.example/logo.svg"
    async with mock_http({url: svg(SQUARE_SVG)}) as http:
        got = await images.best_image(http, [url], verified_only=True, allow_svg=True)
        again = await images.best_image(http, [url], verified_only=True, allow_svg=True)
    assert got == again and got.startswith("/media/logo-") and got.endswith(".png")
    with Image.open(store / got.removeprefix("/media/")) as im:
        assert im.format == "PNG" and im.size == (512, 512) and im.mode == "RGBA"
    assert await images.picture_is_opaque(None, got) is False   # round, so it has transparent corners


async def test_svg_is_refused_unless_a_logo_is_wanted_or_no_folder_is_set(store, monkeypatch):
    url = "https://project.example/logo.svg"
    async with mock_http({url: svg(SQUARE_SVG)}) as http:
        assert await images.best_image(http, [url], verified_only=True) is None
        monkeypatch.setattr(images, "_store", None)
        assert await images.best_image(http, [url], verified_only=True, allow_svg=True) is None


@pytest.mark.parametrize("body", [
    WIDE_SVG,
    SQUARE_SVG.replace("</svg>", '<image href="file:///etc/passwd" width="10" height="10"/></svg>'),
    SQUARE_SVG.replace("</svg>", '<script>alert(1)</script></svg>'),
    "<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'></svg>",
    "not a picture",
])
async def test_svgs_that_are_wide_unsafe_empty_or_not_svg_are_not_drawn(store, body):
    url = "https://project.example/logo.svg"
    async with mock_http({url: svg(body)}) as http:
        assert await images.best_image(http, [url], verified_only=True, allow_svg=True, max_aspect=2.0) is None
    assert list(store.iterdir()) == []


def test_a_readmes_svg_logo_is_a_candidate_but_its_svg_screenshots_are_not():
    md = """<p align="center"><img src="docs/logo.svg" alt="logo"></p>

![shot](docs/shot.svg)"""
    assert images.readme_logos(md, "o/r") == ["https://raw.githubusercontent.com/o/r/HEAD/docs/logo.svg"]
    assert images.readme_images(md, "o/r") == []


async def test_a_pages_svg_icon_becomes_its_logo(store):
    page = Page(url="https://site.example/post", meta={}, ld=[], title="", html="",
                links=[{"rel": "icon", "href": "/logo.svg", "sizes": ""}])
    async with mock_http({"https://site.example/logo.svg": svg(SQUARE_SVG)}) as http:
        picture, kind = await page_picture(http, page)
    assert kind == "logo" and picture.startswith("/media/logo-")
