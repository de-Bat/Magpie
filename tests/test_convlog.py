"""Conversation logs: one file per operation, what was sent and what came back, never a key."""

import base64
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from magpie import convlog
from magpie.config import Settings
from magpie.main import create_app
from test_analyzers import GOOD, FakeOcr, INSTAGRAM_POST, png
from magpie.analyzers import AnalyzerRouter


AUTH = {"Authorization": "Bearer tok"}


@pytest.fixture(autouse=True)
def reset_convlog():
    convlog._last_cleanup = None
    yield
    convlog.configure(None)


def hosted_app(tmp_path, token="tok", **kw):
    """An app whose hosted provider (Gemini) answers from a mock."""
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, headers={"x-ratelimit-remaining-requests": "99", "set-cookie": "secret=1"},
                              json={"choices": [{"message": {"content": json.dumps(GOOD)}}], "usage": {"prompt_tokens": 900, "completion_tokens": 120}})

    s = Settings(data_dir=tmp_path, api_token=token, enrich=False, hosted_llm="gemini", gemini_web_search=False, gemini_api_key="SECRET-KEY-123",
                 analyzer="local", ocr_engine="off", **kw)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = AnalyzerRouter(s, http, ocr=FakeOcr(INSTAGRAM_POST))
    return create_app(s, analyzer=router, http=http, start_batch_worker=False), s, sent


def upload(client, name="s.png"):
    return client.post("/api/items", files={"file": (name, png(), "image/png")}).json()["id"]


def test_nothing_is_written_while_logging_is_off(tmp_path):
    app, s, _ = hosted_app(tmp_path)
    with TestClient(app, headers=AUTH) as client:
        upload(client)
    assert not (tmp_path / "logs").exists()


def test_each_operation_gets_its_own_timestamped_file(tmp_path):
    app, s, sent = hosted_app(tmp_path, log_conversations=True)
    with TestClient(app, headers=AUTH) as client:
        item_id = upload(client)
        client.post(f"/api/items/{item_id}/reanalyze")
        listing = client.get("/api/logs").json()
        assert listing["enabled"] and listing["total"] == 2
        ops = sorted(x["operation"] for x in listing["sessions"])
        assert ops == ["analyze", "reanalyze"]
        first = next(x for x in listing["sessions"] if x["operation"] == "analyze")
        day, name = first["id"].split("/")
        assert len(day) == 10 and name.split("_")[1] == "analyze" and name.endswith(item_id[:8])   # HHMMSS_operation_item
        assert first["outcome"] == "ok" and first["calls"] == 1 and first["models"] == ["gemini:gemini-2.5-flash"] and first["title"]
        # filtered by operation
        assert client.get("/api/logs", params={"operation": "reanalyze"}).json()["total"] == 1
        assert client.get("/api/logs", params={"item": item_id}).json()["total"] == 2

        data = client.get(f"/api/logs/{first['id']}").json()
        kinds = [e["event"] for e in data["events"]]
        assert kinds[0] == "session_start" and kinds[-1] == "session_end"
        assert {"ocr", "llm_request", "llm_response", "analysis", "enrichment", "result"} <= set(kinds)
        req = next(e for e in data["events"] if e["event"] == "llm_request")
        assert req["provider"] == "gemini" and req["mode"] == "hosted" and "no web access" in req["messages"][0]["content"]
        user = req["messages"][1]["content"]
        image = next(c for c in user if c["type"] == "image")
        assert image["bytes"] > 0 and image["sha256"] and "base64" not in json.dumps(req)   # the picture isn't dumped into the log
        resp = next(e for e in data["events"] if e["event"] == "llm_response")
        assert resp["status"] == 200 and GOOD["title"] in resp["text"] and resp["headers"] == {"x-ratelimit-remaining-requests": "99", "content-type": "application/json"}
        end = data["events"][-1]
        assert end["llm_calls"] == 1 and end["duration_ms"] >= 0

    # a file per session, on disk, one JSON object per line
    files = list((tmp_path / "logs").glob("*/*.jsonl"))
    assert len(files) == 2
    assert all(json.loads(line) for f in files for line in f.read_text().splitlines())


def test_keys_never_reach_the_logs_and_images_are_kept_only_when_asked(tmp_path):
    app, s, sent = hosted_app(tmp_path, log_conversations=True)
    with TestClient(app, headers=AUTH) as client:
        upload(client)
    blob = "".join(f.read_text() for f in (tmp_path / "logs").rglob("*.jsonl"))
    assert "SECRET-KEY-123" not in blob and "secret=1" not in blob and "authorization" not in blob.lower()
    assert not list((tmp_path / "logs").rglob("*.png"))

    app, s, _ = hosted_app(tmp_path / "keep", log_conversations=True, log_images=True)
    with TestClient(app, headers=AUTH) as client:
        item_id = upload(client)
        sid = client.get("/api/logs").json()["sessions"][0]["id"]
        data = client.get(f"/api/logs/{sid}").json()
        req = next(e for e in data["events"] if e["event"] == "llm_request")
        name = next(c for c in req["messages"][1]["content"] if c["type"] == "image")["file"]
        assert client.get(f"/api/logs/{sid}/image/{name}").content[:4] == b"\x89PNG"


def test_a_failed_call_is_logged_with_its_error(tmp_path):
    s = Settings(data_dir=tmp_path, api_token="tok", enrich=False, hosted_llm="gemini", gemini_web_search=False, gemini_api_key="k", analyzer="local",
                 ocr_engine="off", log_conversations=True)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    app = create_app(s, analyzer=AnalyzerRouter(s, http, ocr=FakeOcr(INSTAGRAM_POST)), http=http, start_batch_worker=False)
    with TestClient(app, headers=AUTH) as client:
        item_id = upload(client)
        [row] = client.get("/api/logs").json()["sessions"]
        assert row["outcome"] == "error" and "500" in row["error"]
        events = client.get(f"/api/logs/{row['id']}").json()["events"]
        assert [e for e in events if e["event"] == "llm_response" and e.get("status") == 500 and e["text"] == "boom"]


def test_reading_exporting_deleting_and_no_path_tricks(tmp_path):
    app, s, _ = hosted_app(tmp_path, log_conversations=True)
    with TestClient(app, headers=AUTH) as client:
        upload(client)
        sid = client.get("/api/logs").json()["sessions"][0]["id"]
        md = client.get(f"/api/logs/{sid}", params={"format": "md"})
        assert md.headers["content-type"].startswith("text/markdown") and "→ gemini:" in md.text and "**system**" in md.text.lower()
        raw = client.get(f"/api/logs/{sid}", params={"format": "jsonl"})
        assert raw.headers["content-disposition"].startswith("attachment") and raw.text.count("\n") >= 5
        assert client.get("/api/logs/2026-01-01/..%2F..%2Fsettings").status_code == 404
        assert client.get("/api/logs/nope/nope").status_code == 404
        assert client.delete(f"/api/logs/{sid}").json() == {"ok": True}
        assert client.get("/api/logs").json()["total"] == 0
        upload(client)
        assert client.delete("/api/logs").json()["deleted"] == 1


def test_old_logs_are_deleted_by_the_retention_setting(tmp_path):
    app, s, _ = hosted_app(tmp_path, log_conversations=True, log_retention_days=7)
    old = tmp_path / "logs" / "2020-01-01"
    old.mkdir(parents=True)
    (old / "000000_analyze_deadbeef.jsonl").write_text("{}\n")
    with TestClient(app, headers=AUTH) as client:
        upload(client)   # starting a session tidies up
        assert not old.exists() and client.get("/api/logs").json()["total"] == 1


def test_logs_need_settings_access_without_a_token(tmp_path):
    app, s, _ = hosted_app(tmp_path, token=None, log_conversations=True)
    with TestClient(app) as client:
        upload(client)
        assert client.get("/api/logs").status_code == 403   # setup code required, like settings
        code = app.state.runtime.setup_code
        assert client.get("/api/logs", headers={"X-Magpie-Setup-Code": code}).status_code == 200


async def test_claude_conversations_and_batches_are_logged(tmp_path):
    from magpie.analyzer import ScreenshotAnalyzer
    s = Settings(data_dir=tmp_path, log_conversations=True)
    convlog.configure(s)
    message = SimpleNamespace(model="claude-opus-5", stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                              content=[SimpleNamespace(type="text", text="thinking aloud", model_dump=lambda **kw: {"type": "text", "text": "thinking aloud"})])

    async def create(**kw):
        return message
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    claude = ScreenshotAnalyzer(client=client, model="claude-sonnet-4-6")
    with convlog.session("analyze", "abc12345") as sess:
        params = claude.build_params(png(), "image/png", None, None, "")
        await claude._create(params)
        convlog.event("batch_queued", model="claude-opus-5")
    lines = [json.loads(l) for l in sess.path.read_text().splitlines()]
    req = next(e for e in lines if e["event"] == "llm_request")
    assert req["provider"] == "claude" and req["system"].startswith("You catalogue screenshots") and req["parameters"]["tools"]
    assert "base64" not in json.dumps(req)
    resp = next(e for e in lines if e["event"] == "llm_response")
    assert resp["content"][0]["text"] == "thinking aloud" and resp["usage"] if "usage" in resp else True


def test_debug_mode_records_everything_and_tells_the_client(tmp_path):
    app, s, _ = hosted_app(tmp_path, debug=True)   # no logging settings: debug implies them
    with TestClient(app, headers=AUTH) as client:
        assert client.get("/api/status").json()["debug"] is True and client.get("/api/health").json()["debug"] is True
        item_id = upload(client)
        listing = client.get("/api/logs").json()
        assert listing["enabled"] and listing["debug"] and listing["images"]
        [row] = listing["sessions"]
        # the input capture, the model that was used and the card that came out are all in the history
        assert row["input"]["kind"] == "screenshot" and row["input"]["file"].endswith(".input.png")
        assert row["used"] == ["gemini:gemini-2.5-flash"] or row["used"][-1].startswith("gemini")
        assert row["models"] == ["gemini:gemini-2.5-flash"]
        assert row["card"]["title"] == GOOD["title"] and row["card"]["category"]
        detail = client.get(f"/api/logs/{row['id']}").json()
        card = detail["card"]
        assert card["id"] == item_id and "analysis" not in card and card["status"] == "ready"
        # the screenshot as it was sent in, still there once the item is gone
        assert client.get(f"/api/logs/{row['id']}/image/{row['input']['file']}").content[:4] == b"\x89PNG"
        client.delete(f"/api/items/{item_id}")
        assert client.get(f"/api/logs/{row['id']}/image/{row['input']['file']}").status_code == 200


def test_debug_mode_off_by_default(tmp_path):
    app, s, _ = hosted_app(tmp_path)
    with TestClient(app, headers=AUTH) as client:
        assert client.get("/api/status").json()["debug"] is False
        upload(client)
        assert client.get("/api/logs").json()["total"] == 0


def test_a_shared_link_is_logged_with_its_url(tmp_path):
    page = '<html><head><title>Hello</title></head><body><p>' + "word " * 200 + '</p></body></html>'

    def handler(request):
        if request.url.host == "blog.example":
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({**GOOD, "category": "article", "title": "Hello"})}}]})
    s = Settings(data_dir=tmp_path, api_token="tok", enrich=False, hosted_llm="gemini", gemini_web_search=False, gemini_api_key="k", analyzer="local", ocr_engine="off", debug=True)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(s, analyzer=AnalyzerRouter(s, http, ocr=FakeOcr(INSTAGRAM_POST)), http=http, start_batch_worker=False)
    with TestClient(app, headers=AUTH) as client:
        client.post("/api/items", data={"url": "https://blog.example/post"})
        rows = client.get("/api/logs").json()["sessions"]
        link = next(r for r in rows if (r["input"] or {}).get("source_url"))
        assert link["input"]["kind"] == "url" and link["input"]["source_url"] == "https://blog.example/post" and "file" not in link["input"]


def test_a_large_screenshot_does_not_break_the_log(tmp_path):
    # a real screenshot's base64 is far over the field limit: it must be redacted before any truncation
    big = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(os.urandom(300_000)).decode()}}
    url = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(os.urandom(300_000)).decode()}}
    s = Settings(data_dir=tmp_path, log_conversations=True)
    convlog.configure(s)
    with convlog.session("analyze", "abc12345"):
        convlog.log_request("claude", "m", "realtime", messages=[{"role": "user", "content": [big, url, {"type": "text", "text": "héllo ✓"}]}])
    [row] = convlog.list_sessions()["sessions"]
    msgs = convlog.read_session(row["id"])["events"][1]["messages"][0]["content"]
    assert msgs[0]["bytes"] == 300_000 and msgs[1]["bytes"] == 300_000 and msgs[2]["text"] == "héllo ✓"
