"""The running version comes from magpie/VERSION (written by tag.sh) and is shown in Settings → About."""

import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import magpie
from magpie.config import Settings
from magpie.main import create_app

STATIC = Path(magpie.__file__).parent / "static"


def test_the_version_file_is_the_single_source_of_truth():
    text = (Path(magpie.__file__).parent / "VERSION").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d+\.\d+\.\d+\n", text), "tag.sh writes one X.Y.Z line"
    assert magpie.__version__ == text.strip()


def test_the_server_reports_its_version(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path), http=httpx.AsyncClient())) as client:
        assert client.get("/api/health").json()["version"] == magpie.__version__
        assert client.get("/api/status").json()["version"] == magpie.__version__
        assert client.get("/api/settings").json()["status"]["version"] == magpie.__version__  # what the About panel shows


def test_about_is_one_panel_inside_settings():
    html, js = (STATIC / "index.html").read_text(encoding="utf-8"), (STATIC / "app.js").read_text(encoding="utf-8")
    assert "about-dialog" not in html and "fonts.googleapis.com" not in html
    assert "View Full About" not in js and 'data-action="about"' not in js and "close-about" not in js
    assert 'section("About", aboutHtml(data))' in js and "data.status?.version" in js


def test_release_notes_are_served_to_the_about_panel_newest_first(tmp_path):
    from magpie import releases
    notes = releases.load()
    assert notes and notes[0]["features"] and all(f["title"] for r in notes for f in r["features"])
    versions = [r["version"] for r in notes if r["version"] != "next"]
    key = lambda v: tuple(int(x) for x in v.split("."))
    assert versions == sorted(set(versions), key=key, reverse=True)        # newest first, none twice
    assert [r["version"] for r in notes].count("next") <= 1 and "next" not in versions
    with TestClient(create_app(Settings(data_dir=tmp_path), http=httpx.AsyncClient())) as client:
        assert client.get("/api/settings").json()["releases"] == notes
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function releasesHtml(" in js and "${releasesHtml(data.releases)}" in js


def test_unreadable_release_notes_are_just_empty(tmp_path):
    from magpie import releases
    bad = tmp_path / "r.json"
    bad.write_text("{not json")
    assert releases.load(bad) == [] and releases.load(tmp_path / "missing.json") == []
    bad.write_text('[{"version": "1.0.0", "features": [{"title": "A"}, {"text": "no title"}, 3]}, {"nope": 1}, 7]')
    assert releases.load(bad) == [{"version": "1.0.0", "date": None, "features": [{"title": "A"}]}]
