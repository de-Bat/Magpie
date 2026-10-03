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
    text = (Path(magpie.__file__).parent / "VERSION").read_text()
    assert re.fullmatch(r"\d+\.\d+\.\d+\n", text), "tag.sh writes one X.Y.Z line"
    assert magpie.__version__ == text.strip()


def test_the_server_reports_its_version(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path), http=httpx.AsyncClient())) as client:
        assert client.get("/api/health").json()["version"] == magpie.__version__
        assert client.get("/api/status").json()["version"] == magpie.__version__
        assert client.get("/api/settings").json()["status"]["version"] == magpie.__version__  # what the About panel shows


def test_about_is_one_panel_inside_settings():
    html, js = (STATIC / "index.html").read_text(), (STATIC / "app.js").read_text()
    assert "about-dialog" not in html and "fonts.googleapis.com" not in html
    assert "View Full About" not in js and 'data-action="about"' not in js and "close-about" not in js
    assert 'section("About", aboutHtml(data))' in js and "data.status?.version" in js
