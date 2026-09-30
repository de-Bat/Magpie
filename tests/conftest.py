import pytest


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Tests use made-up hostnames served by httpx.MockTransport: resolve them to a public IP,
    except the ones a test marks as internal."""
    async def resolve(host):
        if "internal" in host or host == "localhost":
            return ["10.0.0.5"]
        return ["93.184.216.34"]
    monkeypatch.setattr("magpie.fetch.resolve_host", resolve)


@pytest.fixture(autouse=True)
def fresh_page_cache():
    from magpie import enrich, images
    for cache in (enrich._PAGE_CACHE, enrich._REFUSED, images._CACHE):
        cache.clear()
    yield
    for cache in (enrich._PAGE_CACHE, enrich._REFUSED, images._CACHE):
        cache.clear()
