"""Per-provider/model limits: what's left, pausing a model that hit its limit, and retrying items later."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from magpie import limits
from magpie.analyzer import RateLimited
from magpie.analyzers import AnalyzerRouter, LocalLLMAnalyzer
from magpie.config import Settings
from test_analyzers import GOOD, INSTAGRAM_POST, FakeBackend, FakeOcr, png


def hosted(tmp_path, provider="groq", model=None, **kw):
    key = {"groq": "groq_api_key", "gemini": "gemini_api_key", "openai": "openai_api_key"}[provider]
    s = Settings(data_dir=tmp_path, hosted_llm=provider, **{key: "k"}, **kw)
    return s


def server(*replies):
    """Mock /chat/completions answering each request with the next (status, headers, body)."""
    calls = []

    def handler(request):
        calls.append(request)
        status, headers, body = replies[min(len(calls), len(replies)) - 1]
        if status == 200:
            return httpx.Response(200, headers=headers, json={"choices": [{"message": {"content": json.dumps(GOOD)}}]})
        return httpx.Response(status, headers=headers, text=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def test_resets_are_read_as_timestamps_or_durations():
    assert limits.duration_seconds("6m0s") == 360 and limits.duration_seconds("20ms") == 0.02
    assert limits.duration_seconds("1h2m3s") == 3723 and limits.duration_seconds("12") == 12
    assert limits.duration_seconds("soon") is None
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert limits.reset_at("2026-09-30T12:01:00Z", now) == "2026-09-30T12:01:00+00:00"   # Anthropic
    assert limits.reset_at("1m30s", now) == "2026-09-30T12:01:30+00:00"                   # OpenAI / Groq
    parsed = limits.parse_limits({"anthropic-ratelimit-input-tokens-remaining": "0", "anthropic-ratelimit-input-tokens-limit": "30000",
                                  "anthropic-ratelimit-input-tokens-reset": "2026-09-30T12:01:00Z"})
    assert parsed["input_tokens"]["limit"] == 30000


async def test_a_model_over_its_limit_is_paused_until_it_is_back(tmp_path):
    http, calls = server((429, {"retry-after": "120", "x-ratelimit-remaining-requests": "0", "x-ratelimit-limit-requests": "1000"}, "Rate limit reached"))
    llm = LocalLLMAnalyzer(hosted(tmp_path), http)
    with pytest.raises(RateLimited) as e:
        await llm.analyze(png(), "image/png")
    assert e.value.provider == "groq" and e.value.reason == "rate limit"
    assert 110 < (e.value.until - datetime.now(timezone.utc)).total_seconds() <= 120
    assert "Groq" in str(e.value) and "retries automatically" in str(e.value)
    # the next request doesn't even reach the provider
    with pytest.raises(RateLimited):
        await llm.analyze(png(), "image/png")
    assert len(calls) == 1
    [row] = limits.report([("groq", llm.model)])
    assert row["current"] and row["blocked_reason"] == "rate limit" and row["requests"]["remaining"] == 0 and row["low"]


async def test_short_pauses_are_waited_out(tmp_path):
    http, calls = server((429, {"retry-after": "3"}, "slow down"), (200, {"x-ratelimit-remaining-requests": "99"}, ""))
    llm = LocalLLMAnalyzer(hosted(tmp_path), http)
    waited = []

    async def sleep(s):
        waited.append(s)
        limits.LIMITS[f"groq:{llm.model}"]["blocked_until"] = None   # time passes
    llm._sleep = sleep
    out = await llm.analyze(png(), "image/png")
    assert out["title"] == GOOD["title"] and len(calls) == 2 and 0 < waited[0] <= 3
    assert "blocked_until" not in limits.LIMITS[f"groq:{llm.model}"]   # a success ends the pause


async def test_gemini_daily_quota_and_openai_credit(tmp_path):
    body = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]}})
    http, _ = server((429, {}, body))
    with pytest.raises(RateLimited) as e:
        await LocalLLMAnalyzer(hosted(tmp_path, "gemini"), http).analyze(png(), "image/png")
    assert e.value.reason == "daily quota"

    http, _ = server((429, {}, '{"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}'))
    with pytest.raises(RateLimited) as e:
        await LocalLLMAnalyzer(hosted(tmp_path, "openai"), http).analyze(png(), "image/png")
    assert e.value.reason == "credit" and "out of credit" in str(e.value)
    assert limits.blocked("openai", "gpt-4.1")   # billing applies to every model of the key


def test_gemini_estimate_from_published_limits(tmp_path):
    made = {"day": 249, "minute": 1}
    limits.count_requests = lambda provider, model, since: made["minute"] if datetime.fromisoformat(since) > datetime.now(timezone.utc) - timedelta(minutes=2) else made["day"]
    [row] = limits.report([("gemini", "gemini-2.5-flash")])
    assert row["estimated"] and row["requests"] == {**row["requests"], "remaining": 1, "limit": 250, "per": "day"} and row["low"]
    assert not limits.blocked("gemini", "gemini-2.5-flash")
    made["day"] = 250
    until, reason = limits.blocked("gemini", "gemini-2.5-flash")
    assert reason == "daily quota" and until > datetime.now(timezone.utc)
    [row] = limits.report([("gemini", "gemini-2.5-flash")])
    assert row["blocked_reason"] == "daily quota"


def test_limits_override_for_paid_tiers(monkeypatch):
    monkeypatch.setenv("MAGPIE_RATE_LIMITS", '{"gemini-2.5-flash": [1000, 10000]}')
    limits.count_requests = lambda *a: 300
    assert not limits.blocked("gemini", "gemini-2.5-flash")


async def test_hybrid_keeps_the_local_answer_when_the_fallback_is_limited(tmp_path):
    s = Settings(data_dir=tmp_path, analyzer="hybrid", escalate_below=70, claude_batch=False)
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    claude = FakeBackend(error=RateLimited("claude", "claude-opus-5", until, "rate limit"), label="claude")
    r = AnalyzerRouter(s, httpx.AsyncClient(), ocr=FakeOcr(INSTAGRAM_POST), claude=claude, local=FakeBackend({**GOOD, "confidence": 40}))
    out = await r.analyze(png(), "image/png")
    assert out["confidence"] == 40 and out["_analyzer"] == ["ocr", "local:fake"]
    # with no local answer the item fails, to be retried when Claude is back
    r = AnalyzerRouter(s, httpx.AsyncClient(), ocr=FakeOcr(INSTAGRAM_POST), claude=claude, local=FakeBackend(error=RateLimited("x", None, until, "rate limit")))
    with pytest.raises(RateLimited):
        await r.analyze(png(), "image/png")


def test_limited_items_are_retried_automatically(tmp_path):
    from fastapi.testclient import TestClient
    from magpie.main import create_app

    class Flaky:
        def __init__(self):
            self.limited = True

        async def analyze(self, image, media_type, note=None, correction=None, interactive=False, **kw):
            if self.limited:
                raise RateLimited("gemini", "gemini-2.5-flash", datetime.now(timezone.utc) + timedelta(minutes=10), "rate limit")
            return {**GOOD, "_runs": []}

    flaky = Flaky()
    app = create_app(Settings(data_dir=tmp_path, api_token=None, enrich=False, hosted_llm="gemini", gemini_api_key="k"), analyzer=flaky)
    with TestClient(app) as client:
        item_id = client.post("/api/items", files={"file": ("s.png", png(), "image/png")}).json()["id"]
        item = client.get(f"/api/items/{item_id}").json()
        assert item["status"] == "error" and item["retry_at"] and "Gemini" in item["error"]
        db = app.state.db
        assert db.due_retries() == [] and db.waiting_for_limits() == 1   # not yet
        limits.mark_limited("gemini", "gemini-2.5-flash", {"retry-after": "600"})
        report = client.get("/api/limits").json()
        assert report["waiting"] == 1 and report["models"][0]["blocked_reason"] == "rate limit" and report["models"][0]["current"]
        # time passes: the item is due, and a new outcome clears the retry
        db.conn.execute("UPDATE items SET retry_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (item_id,))
        db.conn.commit()
        assert db.due_retries() == [item_id]
        flaky.limited = False
        db.update_item(item_id, status="processing")
        assert db.get_item(item_id)["retry_at"] is None


async def test_claude_rate_limit_pauses_that_model(tmp_path):
    import anthropic
    from types import SimpleNamespace
    from magpie.analyzer import ScreenshotAnalyzer

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request, headers={"retry-after": "90", "anthropic-ratelimit-tokens-remaining": "0",
                                                              "anthropic-ratelimit-tokens-limit": "80000"}, text="rate_limit_error")
    sent = []

    async def create(**kw):
        sent.append(kw)
        raise anthropic.RateLimitError("rate limited", response=response, body=None)
    client = SimpleNamespace(messages=SimpleNamespace(create=create), beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    claude = ScreenshotAnalyzer(client=client, model="claude-sonnet-5")
    with pytest.raises(RateLimited) as e:
        await claude.analyze(png(), "image/png")
    assert e.value.provider == "claude" and e.value.model == "claude-sonnet-5" and e.value.runs is not None
    with pytest.raises(RateLimited):
        await claude.analyze(png(), "image/png")
    assert len(sent) == 1   # paused: not sent again
    assert limits.LIMITS["claude:claude-sonnet-5"]["tokens"]["limit"] == 80000
    assert not limits.blocked("claude", "claude-haiku-4-5")   # other models aren't affected


async def test_requests_refused_before_sending_are_not_counted(tmp_path):
    http, calls = server((429, {"retry-after": "300"}, "slow down"))
    llm = LocalLLMAnalyzer(hosted(tmp_path), http)
    with pytest.raises(RateLimited) as first:
        await llm.analyze(png(), "image/png")
    with pytest.raises(RateLimited) as second:
        await llm.analyze(png(), "image/png")
    assert [r["requests"] for r in first.value.runs] == [1] and second.value.runs == [] and len(calls) == 1
