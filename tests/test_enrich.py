

def test_readme_images_pick_the_header_and_skip_badges():
    from magpie.images import readme_images
    md = ("[![CI](https://github.com/a/b/workflows/ci/badge.svg)](x)\n![build](https://img.shields.io/x.png)\n"
          '<p align="center"><img src=".github/assets/logo.png" width="200"></p>\n![shot](https://example.com/shot.png)')
    assert readme_images(md, "a/b") == ["https://raw.githubusercontent.com/a/b/HEAD/.github/assets/logo.png", "https://example.com/shot.png"]
    assert readme_images("![x](https://img.shields.io/a.png) ![y](logo.svg)", "a/b") == []


def test_wikipedia_poster_fallback_needs_a_matching_title():
    import asyncio
    import httpx
    from magpie.enrich import _wikipedia_poster

    def client(page_title):
        def handler(request):
            return httpx.Response(200, json={"query": {"pages": {"1": {"title": page_title, "thumbnail": {"source": "https://upload.wikimedia.org/p.jpg"}}}}})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert asyncio.run(_wikipedia_poster("Past Lives", 2023, False, client("Past Lives (film)"))) == "https://upload.wikimedia.org/p.jpg"
    assert asyncio.run(_wikipedia_poster("Past Lives", 2023, False, client("Lives of Others"))) is None


def _png(size):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "teal").save(buf, format="PNG")
    return buf.getvalue()


async def test_best_image_skips_dead_tiny_and_svg_candidates():
    import httpx
    from magpie.images import best_image
    routes = {
        "https://cdn.example/dead.png": httpx.Response(404),
        "https://cdn.example/tiny.png": httpx.Response(200, content=_png((16, 16)), headers={"content-type": "image/png"}),
        "https://cdn.example/banner.png": httpx.Response(200, content=_png((1200, 90)), headers={"content-type": "image/png"}),
        "https://cdn.example/good.png": httpx.Response(200, content=_png((800, 600)), headers={"content-type": "image/png"}),
        "https://cdn.example/blocked.png": httpx.Response(403),
    }
    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        urls = ["https://cdn.example/dead.png", "https://cdn.example/tiny.png", "https://cdn.example/banner.png", "https://cdn.example/good.png"]
        assert await best_image(http, urls) == "https://cdn.example/good.png"
        # nothing checkable: the first one that couldn't be ruled out, else the last resort
        assert await best_image(http, ["https://cdn.example/dead.png", "https://cdn.example/blocked.png"]) == "https://cdn.example/blocked.png"
        assert await best_image(http, ["https://cdn.example/dead.png"], last_resort="https://card") == "https://card"


def test_readme_logos_extracts_logo_candidates():
    from magpie.images import readme_logos
    md = ("[![CI](https://github.com/a/b/workflows/ci/badge.svg)](x)\n"
          '<img src="assets/project-logo.png" alt="MyProject logo" width="128">\n'
          '![Diagram](assets/architecture.png)\n'
          '<p align="center"><img src="mascot.png" width="200"></p>')
    logos = readme_logos(md, "a/b")
    assert "https://raw.githubusercontent.com/a/b/HEAD/assets/project-logo.png" in logos
    # architecture diagram is not a logo, mascot in centered block near top is candidate
    assert "https://raw.githubusercontent.com/a/b/HEAD/assets/architecture.png" not in logos


async def test_github_image_prefers_logo_over_social_preview():
    import httpx
    from magpie.enrich import github_image

    readme = '<p align="center"><img src="assets/logo.png" alt="logo"></p>'
    routes = {
        "https://github.com/myorg/myrepo": httpx.Response(200, text='<html><head><meta property="og:image" content="https://repository-images.githubusercontent.com/123/banner.png"></head></html>', headers={"content-type": "text/html"}),
        "https://repository-images.githubusercontent.com/123/banner.png": httpx.Response(200, content=_png((1280, 640)), headers={"content-type": "image/png"}),
        "https://raw.githubusercontent.com/myorg/myrepo/HEAD/assets/logo.png": httpx.Response(200, content=_png((256, 256)), headers={"content-type": "image/png"}),
    }

    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        repo = {"owner": {"login": "myorg", "type": "Organization", "avatar_url": "https://avatars.githubusercontent.com/u/999"}}
        url, kind = await github_image("myorg/myrepo", {}, http, readme=readme, repo=repo)
        assert url == "https://raw.githubusercontent.com/myorg/myrepo/HEAD/assets/logo.png"
        assert kind == "logo"


async def test_github_image_uses_org_avatar_when_no_readme_logo():
    import httpx
    from magpie.enrich import github_image

    readme = "# MyRepo\nJust text without any logo."
    routes = {
        "https://github.com/myorg/myrepo": httpx.Response(200, text='<html><head><meta property="og:image" content="https://repository-images.githubusercontent.com/123/banner.png"></head></html>', headers={"content-type": "text/html"}),
        "https://avatars.githubusercontent.com/u/999": httpx.Response(200, content=_png((400, 400)), headers={"content-type": "image/png"}),
        "https://repository-images.githubusercontent.com/123/banner.png": httpx.Response(200, content=_png((1280, 640)), headers={"content-type": "image/png"}),
    }

    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        repo = {"owner": {"login": "myorg", "type": "Organization", "avatar_url": "https://avatars.githubusercontent.com/u/999"}}
        url, kind = await github_image("myorg/myrepo", {}, http, readme=readme, repo=repo)
        assert url == "https://avatars.githubusercontent.com/u/999"
        assert kind == "logo"


async def test_github_image_falls_back_to_social_preview_without_logo():
    import httpx
    from magpie.enrich import github_image

    readme = "# MyRepo\nJust text without any logo."
    routes = {
        "https://github.com/john/myrepo": httpx.Response(200, text='<html><head><meta property="og:image" content="https://repository-images.githubusercontent.com/123/banner.png"></head></html>', headers={"content-type": "text/html"}),
        "https://repository-images.githubusercontent.com/123/banner.png": httpx.Response(200, content=_png((1280, 640)), headers={"content-type": "image/png"}),
    }

    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        # User account (not organization) without avatar verification
        repo = {"owner": {"login": "john", "type": "User"}}
        url, kind = await github_image("john/myrepo", {}, http, readme=readme, repo=repo)
        assert url == "https://repository-images.githubusercontent.com/123/banner.png"
        assert kind is None


async def test_enrich_github_sets_logo_kind():
    import httpx
    from magpie.config import Settings
    from magpie.enrich import enrich_github

    settings = Settings()

    readme = '<p align="center"><img src="assets/logo.png" alt="logo"></p>'
    repo_data = {
        "full_name": "cool/app",
        "html_url": "https://github.com/cool/app",
        "stargazers_count": 500,
        "owner": {"login": "cool", "type": "Organization", "avatar_url": "https://avatars.githubusercontent.com/u/123", "html_url": "https://github.com/cool"},
    }
    routes = {
        "https://api.github.com/repos/cool/app": httpx.Response(200, json=repo_data),
        "https://api.github.com/repos/cool/app/readme": httpx.Response(200, text=readme),
        "https://api.github.com/repos/cool/app/releases/latest": httpx.Response(404),
        "https://github.com/cool/app": httpx.Response(200, text='<html><head><meta property="og:image" content="https://repository-images.githubusercontent.com/123/banner.png"></head></html>', headers={"content-type": "text/html"}),
        "https://raw.githubusercontent.com/cool/app/HEAD/assets/logo.png": httpx.Response(200, content=_png((256, 256)), headers={"content-type": "image/png"}),
    }

    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        analysis = {"title": "cool app", "canonical_url": "https://github.com/cool/app", "details": {"github_full_name": "cool/app"}}
        enrichment = await enrich_github(analysis, settings, http)
        assert enrichment is not None
        assert enrichment.image_url == "https://raw.githubusercontent.com/cool/app/HEAD/assets/logo.png"
        assert enrichment.image_kind == "logo"


async def test_github_image_never_uses_a_users_personal_avatar():
    import httpx
    from magpie.enrich import github_image

    readme = "# MyRepo\nJust text without any logo."
    avatar = "https://avatars.githubusercontent.com/u/1"
    routes = {
        # the user's avatar loads fine, but it must not be picked; no social preview or README picture exists
        avatar: httpx.Response(200, content=_png((400, 400)), headers={"content-type": "image/png"}),
    }

    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        repo = {"owner": {"login": "john", "type": "User", "avatar_url": avatar}}
        url, kind = await github_image("john/myrepo", {}, http, readme=readme, repo=repo)
        assert url == "https://opengraph.githubassets.com/1/john/myrepo"
        assert kind is None


def test_clean_keeps_identifying_query_parameters_but_drops_tracking():
    from magpie.findlink import _clean
    assert _clean("https://youtube.com/watch?v=abc123&utm_source=x#t=5") == "https://youtube.com/watch?v=abc123"
    assert _clean("https://shop.example/item.php?id=42&fbclid=zzz") == "https://shop.example/item.php?id=42"
    assert _clean("https://example.com/post?utm_medium=a&gclid=b") == "https://example.com/post"
    assert _clean("https://example.com/post/") == "https://example.com/post/"


