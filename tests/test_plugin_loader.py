"""Installing plugins from outside: a single .py file, or a git repository holding several."""

import json
import subprocess
import sys

import httpx
import pytest

from magpie import plugins
from magpie.config import SPEC_BY_ENV, Settings
from magpie.plugins import loader
from test_plugins import make

HELLO = '''
from magpie.config import Spec
from magpie.plugins import Plugin, register


class Hello(Plugin):
    id = "hello"
    label = "Hello"
    action_label = "Say hello"
    categories = ("movie",)
    specs = (Spec("hello_name", "HELLO_NAME", "str", "world", "Hello", "Name"),)

    def configured(self, settings):
        return True

    async def run(self, item, settings, http):
        return {"state": "added", "message": f"Hello {settings.value('hello_name')} from {item['title']}"}


register(Hello())
'''


def plugin_module(plugin_id, helper=False):
    greeting = "from _helpers import GREETING" if helper else "GREETING = 'hi'"
    return f'''
{greeting}
from magpie.plugins import Plugin, register


class P(Plugin):
    id = "{plugin_id}"
    label = "{plugin_id.title()}"
    action_label = GREETING + " {plugin_id}"
    categories = ("movie",)


register(P())
'''


@pytest.fixture(autouse=True)
def external(monkeypatch):
    monkeypatch.setenv(loader.ALLOW_ENV, "true")
    monkeypatch.delenv("MAGPIE_API_TOKEN", raising=False)
    yield
    for p in plugins.all():
        if p.source != "built-in":
            plugins.unregister(p.id)
    sys.path[:] = [x for x in sys.path if "plugins" not in x or "magpie" in x]


def git_repo(path, files):
    path.mkdir(parents=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    for cmd in (["init", "-b", "main"], ["add", "."], ["commit", "-m", "x"]):
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *cmd], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture
def local_git(monkeypatch):
    """Allow cloning from a local path (the real checks insist on public https URLs)."""
    monkeypatch.setattr(loader, "GIT_PROTOCOLS", "file")
    monkeypatch.setattr(loader, "validate_git_url", lambda url: url)


def setup_headers(client):
    return {"X-Magpie-Setup-Code": client.app.state.runtime.setup_code}


# ---- the gate -------------------------------------------------------------------------------------------


def test_installing_is_off_unless_the_server_environment_allows_it(tmp_path, monkeypatch):
    monkeypatch.delenv(loader.ALLOW_ENV)
    client, _ = make(tmp_path)
    with client:
        r = client.post("/api/plugins/upload", files={"file": ("hello.py", HELLO.encode())}, headers=setup_headers(client))
        assert r.status_code == 403 and loader.ALLOW_ENV in r.json()["detail"]
        assert plugins.get("hello") is None and not (tmp_path / "plugins").exists()
        admin = client.get("/api/settings").json()["plugin_admin"]
        assert admin["allowed"] is False and admin["env"] == loader.ALLOW_ENV and admin["sources"] == []
        assert client.post("/api/plugins/upload", files={"file": ("hello.py", HELLO.encode())}).status_code == 403  # setup code too


def test_installed_plugins_stay_unloaded_while_the_gate_is_off(tmp_path, monkeypatch):
    loader.install_file(tmp_path, "hello.py", HELLO.encode())
    loader.unload_all(tmp_path)
    monkeypatch.delenv(loader.ALLOW_ENV)
    assert loader.load_external(tmp_path) == 0 and plugins.get("hello") is None


# ---- a single file --------------------------------------------------------------------------------------


def test_a_single_file_adds_a_plugin_with_its_own_settings_and_can_be_removed(tmp_path):
    client, item = make(tmp_path)
    with client:
        h = setup_headers(client)
        r = client.post("/api/plugins/upload", files={"file": ("hello.py", HELLO.encode(), "text/x-python")}, headers=h)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert [s["id"] for s in payload["plugin_admin"]["sources"]] == ["file-hello"]
        assert payload["plugin_admin"]["sources"][0]["plugins"] == ["hello"] and payload["plugin_admin"]["sources"][0]["type"] == "file"
        assert {p["id"]: p["source"] for p in payload["plugins"]}["hello"] == "file-hello"
        assert "Hello" in [g["name"] for g in payload["groups"]]  # its settings tab, no restart

        assert client.put("/api/settings", json={"changes": {"HELLO_NAME": "Ada"}}, headers=h).status_code == 200
        r = client.post(f"/api/items/{item}/plugins/hello")
        assert r.status_code == 200 and r.json()["message"] == "Hello Ada from Fight Club"
        assert [p["id"] for p in client.get(f"/api/items/{item}/plugins").json()] == ["hello"]

        assert client.delete("/api/plugins/sources/file-hello", headers=h).status_code == 200
        assert plugins.get("hello") is None and "HELLO_NAME" not in SPEC_BY_ENV
        assert not (tmp_path / "plugins" / "files" / "file-hello").exists()
        assert "HELLO_NAME" not in json.loads((tmp_path / "settings.json").read_text())  # saved values go with it
        assert client.post(f"/api/items/{item}/plugins/hello").status_code == 404
        assert client.get("/api/settings").status_code == 200


def test_installed_plugins_and_their_saved_settings_survive_a_restart(tmp_path):
    client, item = make(tmp_path)
    with client:
        h = setup_headers(client)
        client.post("/api/plugins/upload", files={"file": ("hello.py", HELLO.encode())}, headers=h)
        client.put("/api/settings", json={"changes": {"HELLO_NAME": "Ada"}}, headers=h)
    loader.unload_all(tmp_path)  # the process ends: nothing of it is left in memory
    assert plugins.get("hello") is None and "HELLO_NAME" not in SPEC_BY_ENV

    client, item = make(tmp_path)  # a new process
    with client:
        assert client.post(f"/api/items/{item}/plugins/hello").json()["message"] == "Hello Ada from Fight Club"


def test_bad_files_are_refused_and_leave_nothing_behind(tmp_path):
    broken = "from magpie.plugins import register\nraise RuntimeError('boom')\n"
    for name, body, expect in [("syntax.py", "def (:\n", "isn't valid Python"), ("boom.py", broken, "RuntimeError: boom"),
                               ("empty.py", "X = 1\n", "Found no plugins"), ("dup.py", HELLO.replace('"hello"', '"radarr"'), "already exists"),
                               ("../evil.py", HELLO, "plain name"), ("notes.txt", "x", "plain name"),
                               ("big.py", "#" + "x" * loader.MAX_FILE_BYTES, "larger")]:
        with pytest.raises(loader.LoaderError, match=expect):
            loader.install_file(tmp_path, name, body.encode())
    assert loader.read_sources(tmp_path) == [] and [p.id for p in plugins.all()] == ["radarr", "sonarr"]
    assert not list((tmp_path / "plugins" / "files").glob("*")) if (tmp_path / "plugins" / "files").exists() else True


def test_a_file_can_come_from_a_link_and_is_refreshed_from_it(tmp_path):
    served = {"body": HELLO}

    def route(request: httpx.Request):
        if request.url.host == "raw.githubusercontent.com" and request.url.path == "/me/repo/main/hello.py":
            return httpx.Response(200, content=served["body"].encode())
        if request.url.path.endswith("big.py"):
            return httpx.Response(200, content=b"#" + b"x" * loader.MAX_FILE_BYTES)
        return httpx.Response(404)

    client, _ = make(tmp_path)
    with client:
        client.app.state.pipeline.http = httpx.AsyncClient(transport=httpx.MockTransport(route))
        h = setup_headers(client)
        blob = "https://github.com/me/repo/blob/main/hello.py"   # the page link works too
        r = client.post("/api/plugins/install", json={"url": blob}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["plugin_admin"]["sources"][0]["url"] == "https://raw.githubusercontent.com/me/repo/main/hello.py"

        served["body"] = HELLO.replace("Say hello", "Greet")
        assert client.post("/api/plugins/sources/file-hello/update", headers=h).status_code == 200
        assert plugins.get("hello").action_label == "Greet"

        assert client.post("/api/plugins/install", json={"url": "http://example.com/x.py"}, headers=h).status_code == 400
        assert client.post("/api/plugins/install", json={"url": "https://example.com/missing.py"}, headers=h).status_code == 502
        assert client.post("/api/plugins/install", json={"url": "https://example.com/big.py"}, headers=h).status_code == 400
        assert client.post("/api/plugins/install", json={}, headers=h).status_code == 422
        assert client.post("/api/plugins/install", json={"url": blob}, headers=h).status_code == 409  # already installed


# ---- a git repository with several plugins -------------------------------------------------------------


def test_a_git_repository_installs_all_its_plugins_and_updates(tmp_path, local_git):
    repo = git_repo(tmp_path / "me" / "magpie-plugins", {
        "plugins/alpha.py": plugin_module("alpha", helper=True),
        "plugins/beta.py": plugin_module("beta", helper=True),
        "plugins/_helpers.py": "GREETING = 'hi'\n",
        "plugins/test_alpha.py": "raise SystemExit('tests are not plugins')\n",
        "README.md": "# plugins\n",
    })
    source = loader.install_git(tmp_path / "data", str(repo))
    assert source["id"] == "me-magpie-plugins" and sorted(source["plugins"]) == ["alpha", "beta"]
    assert plugins.get("alpha").source == "me-magpie-plugins" and plugins.get("alpha").action_label == "hi alpha"

    (repo / "plugins" / "gamma.py").write_text(plugin_module("gamma"))
    for cmd in (["add", "."], ["commit", "-m", "gamma"]):
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *cmd], cwd=repo, check=True, capture_output=True)
    assert sorted(loader.update(tmp_path / "data", "me-magpie-plugins")["plugins"]) == ["alpha", "beta", "gamma"]

    loader.remove(tmp_path / "data", "me-magpie-plugins")
    assert all(plugins.get(i) is None for i in ("alpha", "beta", "gamma"))
    assert "_helpers" not in sys.modules and loader.read_sources(tmp_path / "data") == []


def test_modules_at_the_top_of_a_repository_work_too_and_a_branch_can_be_chosen(tmp_path, local_git):
    repo = git_repo(tmp_path / "flat", {"solo.py": plugin_module("solo")})
    subprocess.run(["git", "checkout", "-b", "next"], cwd=repo, check=True, capture_output=True)
    (repo / "solo.py").write_text(plugin_module("solo").replace("class P", "NEXT = True\n\n\nclass P"))
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-am", "n"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    source = loader.install_git(tmp_path / "data", str(repo), ref="next")
    assert source["plugins"] == ["solo"] and source["ref"] == "next"


def test_a_repository_that_half_loads_leaves_nothing_registered(tmp_path, local_git):
    repo = git_repo(tmp_path / "mixed", {"plugins/good.py": plugin_module("good"), "plugins/zbad.py": "raise ValueError('nope')\n"})
    with pytest.raises(loader.LoaderError, match="zbad.py: ValueError: nope"):
        loader.install_git(tmp_path / "data", str(repo))
    assert plugins.get("good") is None and loader.read_sources(tmp_path / "data") == []
    assert not (tmp_path / "data" / "plugins" / "repos" / "mixed").exists()


def test_a_source_that_breaks_later_is_reported_not_fatal(tmp_path):
    loader.install_file(tmp_path, "hello.py", HELLO.encode())
    loader.unload_all(tmp_path)
    (tmp_path / "plugins" / "files" / "file-hello" / "hello.py").write_text("raise RuntimeError('changed')\n")
    client, _ = make(tmp_path)  # the server still starts
    with client:
        sources = client.get("/api/settings").json()["plugin_admin"]["sources"]
        assert sources[0]["plugins"] == [] and "RuntimeError: changed" in sources[0]["error"]
        assert client.delete("/api/plugins/sources/file-hello", headers=setup_headers(client)).status_code == 200


@pytest.mark.parametrize("url", ["http://github.com/a/b", "ssh://git@github.com/a/b", "git@github.com:a/b.git", "ext::sh -c touch /tmp/x",
                                 "file:///etc", "/srv/repo", "https://user:pw@github.com/a/b", "https://github.com/a/b --upload-pack=x",
                                 "-https://x", ""])
def test_only_plain_public_https_git_urls_are_accepted(url):
    with pytest.raises(loader.LoaderError):
        loader.validate_git_url(url)


def test_git_refs_cannot_smuggle_options(tmp_path):
    for ref in ("--upload-pack=touch /tmp/x", "-b", "a b", "x;y"):
        with pytest.raises(loader.LoaderError, match="branch or tag"):
            loader.install_git(tmp_path, "https://github.com/a/b", ref)
    assert loader.validate_git_url("https://github.com/a/b.git") == "https://github.com/a/b.git"
