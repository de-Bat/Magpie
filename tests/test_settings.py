"""Startup never fails; problems are reported; settings can be read and changed from the UI."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from magpie.analyzers import AnalyzerRouter
from magpie.config import Settings
from magpie.main import INTERRUPTED, create_app


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "LOCAL_LLM_URL", "MAGPIE_ANALYZER", "MAGPIE_API_TOKEN", "MAGPIE_ESCALATE_BELOW",
                 "LOCAL_LLM_TIMEOUT", "MAGPIE_CLAUDE_BATCH", "TMDB_API_KEY", "OMDB_API_KEY", "MAGPIE_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAGPIE_OCR", "off")


def app_for(tmp_path, **kw):
    return create_app(Settings.load() if not kw else Settings(**kw), http=httpx.AsyncClient())


def test_invalid_environment_values_fall_back_and_are_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAGPIE_ESCALATE_BELOW", "high")
    monkeypatch.setenv("LOCAL_LLM_TIMEOUT", "soon")
    monkeypatch.setenv("MAGPIE_ANALYZER", "gpt")
    with TestClient(app_for(tmp_path)) as client:
        health = client.get("/api/health").json()
        status = client.get("/api/status").json()
    assert health["ok"] and health["status"] == "error" and health["errors"] == 3
    keys = {p["key"] for p in status["problems"] if p["level"] == "error"}
    assert keys == {"MAGPIE_ESCALATE_BELOW", "LOCAL_LLM_TIMEOUT", "MAGPIE_ANALYZER"}
    assert status["problems"][0]["level"] == "error"  # most serious first


def test_unusable_data_directory_starts_and_answers_503(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with TestClient(create_app(Settings(data_dir=blocker / "data"), http=httpx.AsyncClient())) as client:
        assert client.get("/api/health").json()["status"] == "error"
        status = client.get("/api/status").json()
        assert "Storage unavailable" in status["problems"][0]["message"]
        r = client.get("/api/items")
        assert r.status_code == 503 and "Storage unavailable" in r.json()["detail"]
        assert client.get("/api/settings").status_code == 200
        assert client.get("/").status_code == 200  # the UI still loads and can show the problem


def test_missing_key_is_reported_with_the_setting_to_fix(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, analyzer="claude", anthropic_api_key=None),
                               http=httpx.AsyncClient())) as client:
        problems = client.get("/api/status").json()["problems"]
        settings = client.get("/api/settings").json()
    assert problems[0] == {"level": "error", "key": "ANTHROPIC_API_KEY", "message": problems[0]["message"]}
    entry = next(s for g in settings["groups"] for s in g["settings"] if s["env"] == "ANTHROPIC_API_KEY")
    assert entry["problem"]["level"] == "error" and entry["is_set"] is False


def test_saving_settings_applies_them_live_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAGPIE_CLAUDE_BATCH", "false")
    app = app_for(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["analyzer"] == "ocr"
        r = client.put("/api/settings", json={"changes": {"ANTHROPIC_API_KEY": "sk-ant-secret-1234", "MAGPIE_EFFORT": "low"}})
        assert r.status_code == 200
        body = r.json()
        key = next(s for g in body["groups"] for s in g["settings"] if s["env"] == "ANTHROPIC_API_KEY")
        assert key["is_set"] and key["source"] == "ui" and "sk-ant-secret" not in json.dumps(body)
        assert key["value"].endswith("1234")
        assert client.get("/api/health").json()["analyzer"] == "claude"
        assert isinstance(app.state.pipeline.analyzer, AnalyzerRouter) and app.state.pipeline.analyzer.mode == "claude"

    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved == {"ANTHROPIC_API_KEY": "sk-ant-secret-1234", "MAGPIE_EFFORT": "low"}
    reloaded = Settings.load()
    assert reloaded.anthropic_api_key == "sk-ant-secret-1234" and reloaded.effort == "low"

    # Resetting removes the saved value: back to the environment/default.
    with TestClient(app_for(tmp_path)) as client:
        client.put("/api/settings", json={"changes": {"MAGPIE_EFFORT": None}})
    assert json.loads((tmp_path / "settings.json").read_text()) == {"ANTHROPIC_API_KEY": "sk-ant-secret-1234"}


def test_invalid_settings_are_rejected_per_field(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path), http=httpx.AsyncClient())) as client:
        r = client.put("/api/settings", json={"changes": {"MAGPIE_ESCALATE_BELOW": "150", "MAGPIE_EFFORT": "extreme",
                                                          "NOT_A_SETTING": "1"}})
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert set(errors) == {"MAGPIE_ESCALATE_BELOW", "MAGPIE_EFFORT", "NOT_A_SETTING"}
    assert not (tmp_path / "settings.json").exists()


def test_corrupt_settings_file_is_reported_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text("{not json")
    with TestClient(app_for(tmp_path)) as client:
        problems = client.get("/api/status").json()["problems"]
    assert any("saved settings" in p["message"] for p in problems)


def test_token_protects_settings_and_can_be_changed(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, api_token="old"), http=httpx.AsyncClient())) as client:
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/health").status_code == 200
        auth = {"Authorization": "Bearer old"}
        assert client.put("/api/settings", json={"changes": {"MAGPIE_API_TOKEN": "new"}}, headers=auth).status_code == 200
        assert client.get("/api/items", headers=auth).status_code == 401
        assert client.get("/api/items", headers={"Authorization": "Bearer new"}).status_code == 200


def test_items_interrupted_by_a_restart_are_marked_failed(tmp_path):
    settings = Settings(data_dir=tmp_path, analyzer="ocr")
    with TestClient(create_app(settings, http=httpx.AsyncClient())) as client:
        db = client.app.state.db
        stuck = db.create_item("x.png")["id"]
        queued = db.create_item("y.png")["id"]
        db.add_batch_job(queued, "analyze", {}, {})
    with TestClient(create_app(settings, http=httpx.AsyncClient())) as client:
        assert client.get(f"/api/items/{stuck}").json()["error"] == INTERRUPTED
        assert client.get(f"/api/items/{queued}").json()["status"] == "processing"


def test_unexpected_errors_return_json(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), http=httpx.AsyncClient())

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("kaput")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/boom")
    assert r.status_code == 500 and "kaput" in r.json()["detail"]
