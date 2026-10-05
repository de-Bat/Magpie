"""Install and load plugins from outside Magpie: a git repository (any number of plugins) or a single .py file.

This runs the code you install with the server's privileges, so it is off unless the server's environment
sets MAGPIE_ALLOW_PLUGIN_INSTALL=true (deliberately not a UI setting: whoever holds an access token can change
settings, and that must not become code execution on the host).

Layout under <data dir>/plugins/:  sources.json (what is installed) and one folder per source holding its files.
A source's plugin modules are the *.py files in its `plugins/` folder, or else at its root (names starting with
`_` are helpers). A module registers its plugins with `magpie.plugins.register(...)` when it is imported.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import all as all_plugins, loading_from, unregister

log = logging.getLogger("magpie.plugins")

ALLOW_ENV = "MAGPIE_ALLOW_PLUGIN_INSTALL"
GIT_PROTOCOLS = "https"          # git's own transports are limited to this (no ext::, file://, ssh)
GIT_TIMEOUT = 120
MAX_FILE_BYTES = 256 * 1024
_LOCK = threading.RLock()
_errors: dict[str, str] = {}     # source id -> why it didn't load (this process)


class LoaderError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def allowed() -> bool:
    return os.environ.get(ALLOW_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# ---- what is installed ---------------------------------------------------------------------------------


def _root(data_dir: Path) -> Path:
    return Path(data_dir) / "plugins"


def read_sources(data_dir: Path) -> list[dict]:
    try:
        data = json.loads((_root(data_dir) / "sources.json").read_text())
        return [s for s in data if isinstance(s, dict) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(s.get("id", "")))]
    except (OSError, ValueError, TypeError):
        return []


def _write_sources(data_dir: Path, sources: list[dict]) -> None:
    _root(data_dir).mkdir(parents=True, exist_ok=True)
    path = _root(data_dir) / "sources.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sources, indent=2))
    tmp.replace(path)


def describe_sources(data_dir: Path) -> list[dict]:
    plugins = all_plugins()
    return [{**{k: s.get(k) for k in ("id", "type", "url", "ref", "installed")},
             "plugins": [p.id for p in plugins if p.source == s["id"]], "error": _errors.get(s["id"])}
            for s in read_sources(data_dir)]


# ---- loading -------------------------------------------------------------------------------------------


def _base(folder: Path) -> Path:
    return folder / "plugins" if (folder / "plugins").is_dir() else folder


def _modules(folder: Path) -> list[Path]:
    base = _base(folder)
    skip = ("setup.py", "conftest.py")
    return sorted(f for f in base.glob("*.py") if not f.name.startswith("_") and f.name not in skip and not f.name.startswith("test_"))


def _unload(source_id: str, folder: Path) -> list[str]:
    """Forget a source's plugins and modules. Returns the setting names its plugins had."""
    envs = []
    for plugin in [p for p in all_plugins() if p.source == source_id]:
        envs += [s.env for s in plugin.specs]
        unregister(plugin.id)
    resolved = folder.resolve()
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file and Path(file).resolve().is_relative_to(resolved):
            del sys.modules[name]
    sys.path[:] = [p for p in sys.path if p not in (str(folder), str(folder / "plugins"))]
    return envs


def _load(source: dict, data_dir: Path) -> list[str]:
    """Import a source's modules. All or nothing: on any failure its plugins are forgotten and LoaderError raised."""
    folder = _root(data_dir) / source["path"]
    if not folder.is_dir():
        raise LoaderError("Its files are missing; remove it and install it again.")
    sid = source["id"]
    for entry in {str(folder), str(_base(folder))}:   # a repo's modules can import each other and its helpers
        sys.path.insert(0, entry)
    try:
        with loading_from(sid):
            for file in _modules(folder):
                name = f"magpie_ext_{sid.replace('-', '_')}_{file.stem}"
                spec = importlib.util.spec_from_file_location(name, file)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                try:
                    spec.loader.exec_module(module)
                except (Exception, SystemExit) as e:
                    raise LoaderError(f"{file.name}: {type(e).__name__}: {e}") from None
    except LoaderError as e:
        _unload(sid, folder)
        _errors[sid] = str(e)
        raise
    _errors.pop(sid, None)
    return [p.id for p in all_plugins() if p.source == sid]


def load_external(data_dir: Path) -> int:
    """At startup: load what is installed. Never raises; failures are recorded per source."""
    if not allowed():
        return 0
    loaded = 0
    with _LOCK:
        for source in read_sources(data_dir):
            try:
                loaded += len(_load(source, data_dir))
            except LoaderError as e:
                _errors[source["id"]] = str(e)
                log.error("Plugin source %s didn't load: %s", source["id"], e)
    return loaded


def unload_all(data_dir: Path) -> None:
    with _LOCK:
        for source in read_sources(data_dir):
            _unload(source["id"], _root(data_dir) / source["path"])
        _errors.clear()


# ---- installing ----------------------------------------------------------------------------------------


def need_allowed() -> None:
    if not allowed():
        raise LoaderError(f"Installing plugins runs code on the server, so it is off. Set {ALLOW_ENV}=true in the "
                          "server's environment (e.g. .env) and restart to turn it on.", 403)


def validate_git_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or re.search(r"\s", url):
        raise LoaderError("Use a public https:// git URL without a username or password.")
    return url


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "plugin"


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": GIT_PROTOCOLS, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", "-c", "protocol.ext.allow=never", *args], cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise LoaderError("git isn't installed on the server.", 500) from None
    except subprocess.TimeoutExpired:
        raise LoaderError(f"git took longer than {GIT_TIMEOUT} seconds.", 504) from None
    if done.returncode:
        raise LoaderError(f"git failed: {(done.stderr.strip().splitlines() or ['unknown error'])[-1]}", 502)
    return done.stdout.strip()


def _add(data_dir: Path, source: dict) -> dict:
    """Load a freshly written source and record it; on failure remove its files."""
    try:
        _load(source, data_dir)
    except LoaderError:
        shutil.rmtree(_root(data_dir) / source["path"], ignore_errors=True)
        _errors.pop(source["id"], None)
        raise
    ids = [p.id for p in all_plugins() if p.source == source["id"]]
    if not ids:
        _unload(source["id"], _root(data_dir) / source["path"])
        shutil.rmtree(_root(data_dir) / source["path"], ignore_errors=True)
        raise LoaderError("Found no plugins in it: its modules must call magpie.plugins.register(...).")
    _write_sources(data_dir, [*read_sources(data_dir), source])
    return {**source, "plugins": ids}


def install_git(data_dir: Path, url: str, ref: str | None = None) -> dict:
    need_allowed()
    url = validate_git_url(url)
    ref = (ref or "").strip() or None
    if ref and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref):
        raise LoaderError("The branch or tag has characters git doesn't allow.")
    with _LOCK:
        host_path = urlsplit(url.replace("\\", "/")).path.removesuffix(".git").strip("/").split("/")
        sid = _slug("-".join(host_path[-2:]) or urlsplit(url).hostname)
        if any(s["id"] == sid for s in read_sources(data_dir)):
            raise LoaderError(f"{sid} is already installed; update or remove it instead.", 409)
        rel = f"repos/{sid}"
        dest = _root(data_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        try:
            _git("clone", "--depth", "1", *(["--branch", ref] if ref else []), "--", url, str(dest))
        except LoaderError:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return _add(data_dir, {"id": sid, "type": "git", "url": url, "ref": ref, "path": rel, "installed": _now()})


def install_file(data_dir: Path, filename: str, content: bytes, url: str | None = None, replace: bool = False) -> dict:
    need_allowed()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.py", filename or ""):
        raise LoaderError("A plugin file is a .py file with a plain name (letters, digits, - _ .).")
    if len(content) > MAX_FILE_BYTES:
        raise LoaderError(f"That file is larger than {MAX_FILE_BYTES // 1024} KB.")
    try:
        compile(content, filename, "exec")
    except (SyntaxError, ValueError) as e:
        raise LoaderError(f"{filename} isn't valid Python: {e}") from None
    stem = filename[:-3]
    sid = _slug(f"file-{stem}")
    with _LOCK:
        existing = next((s for s in read_sources(data_dir) if s["id"] == sid), None)
        if existing and not replace:
            raise LoaderError(f"{sid} is already installed; update or remove it instead.", 409)
        if existing:
            _drop(data_dir, sid)
        rel = f"files/{sid}"
        folder = _root(data_dir) / rel
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        (folder / f"{stem}.py").write_bytes(content)
        return _add(data_dir, {"id": sid, "type": "file", "url": url, "ref": None, "path": rel, "installed": _now()})


def update(data_dir: Path, source_id: str) -> dict:
    """Pull a git source's latest commit and reload it. (A file from a URL is refreshed by re-installing it.)"""
    need_allowed()
    with _LOCK:
        source = next((s for s in read_sources(data_dir) if s["id"] == source_id), None)
        if source is None:
            raise LoaderError("No such plugin source.", 404)
        if source["type"] != "git":
            raise LoaderError("Only git sources are updated this way.")
        folder = _root(data_dir) / source["path"]
        _git("pull", "--ff-only", cwd=folder)
        _unload(source_id, folder)
        _load(source, data_dir)
        return {**source, "plugins": [p.id for p in all_plugins() if p.source == source_id]}


def _drop(data_dir: Path, source_id: str) -> list[str]:
    sources = read_sources(data_dir)
    source = next((s for s in sources if s["id"] == source_id), None)
    if source is None:
        raise LoaderError("No such plugin source.", 404)
    folder = _root(data_dir) / source["path"]
    envs = _unload(source_id, folder)
    shutil.rmtree(folder, ignore_errors=True)
    _write_sources(data_dir, [s for s in sources if s["id"] != source_id])
    _errors.pop(source_id, None)
    return envs


def setting_names(source_id: str) -> list[str]:
    return [spec.env for p in all_plugins() if p.source == source_id for spec in p.specs]


def remove(data_dir: Path, source_id: str) -> list[str]:
    """Uninstall a source. Returns the names of the settings its plugins had, so their saved values can be cleared."""
    with _LOCK:
        return _drop(data_dir, source_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
