"""HTTP API and web UI."""

import asyncio
import hmac
import logging
import mimetypes
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import CATEGORIES, AnalysisError
from .analyzers import AnalyzerRouter
from .batch import BatchWorker
from .links import URL_TOO_LONG, normalize_url
from .config import HOSTED_LLMS, SPEC_BY_ATTR, SPECS, Settings, mask
from .models import check_key
from .usage import LIMITS
from .db import Database
from .pipeline import Pipeline

log = logging.getLogger("magpie")

STATIC_DIR = Path(__file__).parent / "static"
mimetypes.add_type("application/manifest+json", ".webmanifest")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
API_VERSION = 1
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class Correction(BaseModel):
    """What the user says the screenshot really is. Give facts, a free-text hint, or both."""
    title: str | None = None
    category: str | None = None
    year: int | None = None
    canonical_url: str | None = None
    hint: str | None = None  # e.g. "it's the 2019 remake, not the original" -> Claude looks again


class ItemPatch(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    summary: str | None = None
    category: str | None = None
    note: str | None = None
    canonical_url: str | None = None
    tags: list[str] | None = None


class ModelsRequest(BaseModel):
    provider: str
    url: str | None = None   # local / self-hosted only
    key: str | None = None   # a key typed but not saved yet


class SettingsUpdate(BaseModel):
    # env name -> new value; null or "" removes the value saved from the UI (back to env/default)
    changes: dict[str, Any]


def create_app(
    settings: Settings | None = None,
    analyzer: Any = None,
    http: httpx.AsyncClient | None = None,
    start_batch_worker: bool = True,
) -> FastAPI:
    # Nothing in here may stop the server from starting: problems are recorded and shown in the UI.
    if settings is None:
        try:
            settings = Settings.load()
        except Exception as e:  # defensive: Settings.load() already tolerates bad values
            log.exception("Settings could not be loaded; using defaults")
            settings = Settings()
            settings.load_errors.append(f"Settings could not be loaded: {e}")
    rt = _Runtime(settings)
    rt.open_database()

    def log_setup_code() -> None:
        log.warning("No access token is set. Setup code for changing settings in the web app: %s "
                    "(new on every start; not needed once MAGPIE_API_TOKEN is set)", rt.setup_code)
    db = rt.db  # a proxy: answers 503 with the reason while the database is unavailable
    state: dict = {}

    def build_analyzer(client: httpx.AsyncClient) -> Any:
        if analyzer is not None:
            return analyzer
        try:
            chosen = AnalyzerRouter(settings, client, spent=db.month_cost)
            rt.analyzer_error = None
            return chosen
        except Exception as e:  # misconfiguration: keep serving, report it in the UI and on each item
            log.error("Analyzer not available: %s", e)
            rt.analyzer_error = str(e)
            return _Unavailable(str(e))

    def start_worker(chosen: Any) -> None:
        if rt.worker_task:
            rt.worker_task.cancel()
            rt.worker_task = None
        if (isinstance(chosen, AnalyzerRouter) and chosen.batch and chosen.claude is not None
                and start_batch_worker and rt.db_ok):
            app.state.batch_worker = BatchWorker(db, state["pipeline"], chosen.claude.client, settings.batch_poll_seconds)
            rt.worker_task = asyncio.create_task(app.state.batch_worker.run_forever())

    def reconfigure() -> None:
        """Apply changed settings without a restart."""
        pipeline = state["pipeline"]
        pipeline.analyzer = build_analyzer(pipeline.http)
        start_worker(pipeline.analyzer)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = http or httpx.AsyncClient(timeout=20)
        chosen = build_analyzer(client)
        state["pipeline"] = app.state.pipeline = Pipeline(db, settings, chosen, client)
        try:
            start_worker(chosen)
        except Exception as e:
            log.exception("Batch worker not started")
            rt.startup_errors.append(f"The Claude batch worker could not start: {e}")
        for problem in settings.problems():
            if problem["level"] != "info":
                log.warning("Setup: %s", problem["message"])
        if not settings.api_tokens:
            log_setup_code()
        yield
        if rt.worker_task:
            rt.worker_task.cancel()
        if http is None:
            await client.aclose()

    app = FastAPI(title="Magpie", lifespan=lifespan)
    app.state.db = db
    app.state.runtime = rt

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": f"Internal server error ({type(exc).__name__}: {exc})"}, status_code=500)

    # Endpoints that keep working while the database is unavailable, so the UI can say why.
    no_db_paths = {"/api/health", "/api/status", "/api/settings"}

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        protected = path.startswith("/api/") or path.startswith("/media/")
        if settings.api_tokens and protected and path != "/api/health":
            auth = request.headers.get("authorization", "")
            supplied = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else unquote(request.cookies.get("magpie_token") or request.cookies.get("keeper_token", ""))
            # compare against every token (no early exit) so timing doesn't reveal which one matched
            if not any([hmac.compare_digest(supplied.encode(), t.encode()) for t in settings.api_tokens]):
                return JSONResponse({"detail": "Missing or invalid API token"}, status_code=401)
        if protected and path not in no_db_paths and not rt.db_ok:
            rt.open_database(retry=True)  # the data directory may have been fixed since
            if not rt.db_ok:
                return JSONResponse({"detail": f"Storage unavailable: {rt.db_error}"}, status_code=503)
        return await call_next(request)

    def status_report() -> dict:
        problems = [{"level": "error", "key": None, "message": m} for m in rt.startup_errors]
        if not rt.db_ok:
            problems.append({"level": "error", "key": None, "message": f"Storage unavailable: {rt.db_error}"})
        settings_problems = settings.problems() if analyzer is None else [
            p for p in settings.problems() if p["key"] not in ("ANTHROPIC_API_KEY", "LOCAL_LLM_URL", "MAGPIE_ANALYZER")]
        problems += settings_problems
        if rt.analyzer_error and not any(p["level"] == "error" for p in settings_problems):
            problems.append({"level": "error", "key": None, "message": f"Analyzer not available: {rt.analyzer_error}"})
        levels = {p["level"] for p in problems}
        status = "error" if "error" in levels else "warning" if "warning" in levels else "ok"
        return {"status": status, "problems": problems, "analyzer": settings.resolved_analyzer()}

    @app.get("/api/health")
    def health():
        report = status_report()
        return {
            "ok": True, "api_version": API_VERSION, "auth_required": bool(settings.api_tokens),
            "analyzer": settings.resolved_analyzer(), "status": report["status"],
            "errors": sum(p["level"] == "error" for p in report["problems"]),
            "warnings": sum(p["level"] == "warning" for p in report["problems"]),
        }

    @app.get("/api/status")
    def server_status():
        """Everything that's wrong or missing in the setup, most serious first."""
        report = status_report()
        order = {"error": 0, "warning": 1, "info": 2}
        report["problems"].sort(key=lambda p: order.get(p["level"], 3))
        return report

    def provider_choices() -> list[dict]:
        """What the "AI provider" picker offers: each provider's endpoint and the settings its fields edit."""
        out = [{"id": "claude", "label": "Anthropic (Claude)", "url": None, "model": SPEC_BY_ATTR["model"].default,
                "key_env": "ANTHROPIC_API_KEY", "model_env": "MAGPIE_MODEL"}]
        out += [{"id": pid, "label": label, "url": url, "model": model, "key_env": key_attr.upper(),
                 "model_env": "LOCAL_LLM_MODEL"} for pid, (url, model, key_attr, label) in HOSTED_LLMS.items()]
        out.append({"id": "local", "label": "Local / self-hosted", "url": None, "model": None,
                    "key_env": "LOCAL_LLM_API_KEY", "model_env": "LOCAL_LLM_MODEL"})
        return out

    def settings_payload() -> dict:
        problems = {p["key"]: p for p in settings.problems() if p["key"]}
        groups: dict[str, list] = {}
        for spec in SPECS:
            value = getattr(settings, spec.attr)
            entry = {
                "env": spec.env, "label": spec.label, "help": spec.help, "kind": spec.kind,
                "choices": list(spec.choices), "default": None if spec.kind == "secret" else spec.default,
                "source": settings.source_of(spec), "is_set": value not in (None, ""),
                "value": mask(value) if spec.kind == "secret" else value,
                "problem": problems.get(spec.env),
            }
            if spec.kind != "secret" and spec.env in settings.overrides:
                entry["saved"] = settings.overrides[spec.env]  # what was typed, even if invalid
            groups.setdefault(spec.group, []).append(entry)
        return {
            "groups": [{"name": name, "settings": items} for name, items in groups.items()],
            "data_dir": str(settings.data_dir), "settings_file": str(settings.overrides_path),
            "setup_code_required": not settings.api_tokens,
            "status": server_status(),
            "providers": provider_choices(), "resolved_analyzer": settings.resolved_analyzer(),
        }

    @app.get("/api/settings")
    def get_settings():
        return settings_payload()

    def require_settings_access(request: Request) -> None:
        # Without an access token anyone who can reach the server gets this far, so changing settings
        # also needs the setup code from the server's log. (Not "localhost is fine": DNS rebinding lets
        # a web page send requests that look local.)
        if not settings.api_tokens and not _setup_code_ok(request.headers.get("x-magpie-setup-code"), rt.setup_code):
            raise HTTPException(403, {
                "code": "setup_code_required",
                "message": "This server has no access token yet. Enter the setup code printed in the server's log "
                           "(docker compose logs magpie), or set MAGPIE_API_TOKEN.",
            })

    @app.post("/api/models")
    async def list_models(req: ModelsRequest, request: Request):
        """The models a provider offers, for the settings screen's model picker. Same access rules as changing
        settings, because it makes the server call out with a stored key."""
        require_settings_access(request)
        client = state["pipeline"].http if "pipeline" in state else httpx.AsyncClient(timeout=20)
        try:
            return await check_key(settings, client, req.provider, req.url, req.key)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Couldn't reach the provider: {e!r}")

    @app.put("/api/settings")
    async def put_settings(update: SettingsUpdate, request: Request):
        require_settings_access(request)
        had_token = bool(settings.api_tokens)
        try:
            notices = settings.save_overrides(update.changes)
        except OSError as e:
            raise HTTPException(500, f"Couldn't save settings to {settings.overrides_path}: {e}")
        except ValueError as e:  # SettingsError
            raise HTTPException(422, {"message": "Some values are invalid", "errors": getattr(e, "errors", {"_": str(e)})})
        if "pipeline" in state:
            reconfigure()
        if had_token and not settings.api_tokens:
            log_setup_code()  # the token was just removed: settings changes need the code again
        return {**settings_payload(), "notices": notices}

    @app.get("/api/sync")
    def sync(since: str | None = Query(None, description="server_time returned by the previous sync")):
        """Delta sync for offline clients: items changed since the cursor, plus deletions."""
        return db.changes_since(since)

    def get_or_404(item_id: str) -> dict:
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        return item

    @app.post("/api/items", status_code=202)
    async def upload(
        background: BackgroundTasks,
        file: UploadFile | None = File(None),
        url: str | None = Form(None, description="Capture a link instead of a screenshot"),
        note: str | None = Form(None),
        tags: str | None = Form(None, description="Comma-separated tags to add"),
        id: str | None = Form(None, description="Client-generated id; re-sending the same id is a no-op"),
        created_at: str | None = Form(None, description="When the screenshot was captured (ISO 8601)"),
    ):
        if id is not None:
            if not CLIENT_ID_RE.match(id):
                raise HTTPException(422, "id must be 8-64 characters of [A-Za-z0-9_-]")
            existing = db.get_item(id)
            if existing:
                return existing
            if db.is_deleted(id):
                raise HTTPException(410, "This item was deleted")
        if (file is None) == (not url):
            raise HTTPException(422, "Send either a screenshot (file) or a link (url)")
        if url:
            return capture_url(url, id, note, tags, created_at, background)
        media_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
        if media_type not in IMAGE_TYPES:
            raise HTTPException(415, f"Unsupported image type {media_type!r}; use PNG, JPEG, WebP or GIF")
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Screenshot is larger than 20 MB")
        if not data:
            raise HTTPException(400, "Empty file")
        name = uuid.uuid4().hex + IMAGE_TYPES[media_type]
        (settings.uploads_dir / name).write_bytes(data)
        item = db.create_item(
            name, note=note or None, tags=[t for t in (tags or "").split(",") if t.strip()],
            item_id=id, created_at=_normalize_time(created_at),
        )
        background.add_task(state["pipeline"].process, item["id"])
        return item

    def capture_url(url: str, item_id: str | None, note: str | None, tags: str | None, created_at: str | None,
                    background: BackgroundTasks) -> dict:
        url = url.strip()
        if len(url) > URL_TOO_LONG:
            raise HTTPException(422, "URL is too long")
        normalized = normalize_url(url)
        if not re.match(r"^https?://[^/\s]+\.[^/\s]+", normalized):
            raise HTTPException(422, "That doesn't look like a web link (http or https)")
        existing = db.find_by_source_url(normalized)
        if existing:  # already saved: don't identify (or pay for) it twice
            new_tags = [t for t in (tags or "").split(",") if t.strip()]
            if new_tags:
                db.add_tags(existing["id"], new_tags)
            if note and not existing.get("note"):
                db.update_item(existing["id"], note=note)
            return {**db.get_item(existing["id"]), "duplicate": True}
        item = db.create_item(
            "", note=note or None, tags=[t for t in (tags or "").split(",") if t.strip()],
            item_id=item_id, created_at=_normalize_time(created_at), kind="url", source_url=normalized,
        )
        background.add_task(state["pipeline"].process, item["id"])
        return item

    @app.get("/api/items")
    def list_items(
        q: str | None = None,
        category: str | None = None,
        tag: list[str] = Query(default=[]),
        needs_review: bool = False,
        limit: int = Query(200, le=500),
        offset: int = 0,
    ):
        return db.list_items(q=q, category=category, tags=tag, needs_review=needs_review, limit=limit, offset=offset)

    @app.get("/api/items/{item_id}")
    def get_item(item_id: str):
        return get_or_404(item_id)

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: str, patch: ItemPatch):
        get_or_404(item_id)
        fields = patch.model_dump(exclude_unset=True)
        if "category" in fields and fields["category"] not in CATEGORIES:
            raise HTTPException(422, f"category must be one of {CATEGORIES}")
        tags = fields.pop("tags", None)
        if tags is not None:
            db.set_tags(item_id, tags)
        return db.update_item(item_id, **fields)

    @app.post("/api/items/{item_id}/reanalyze", status_code=202)
    def reanalyze(item_id: str, background: BackgroundTasks):
        get_or_404(item_id)
        item = db.update_item(item_id, status="processing", error=None)
        background.add_task(state["pipeline"].process, item_id, None, "reanalyze")
        return item

    @app.post("/api/items/{item_id}/refresh-metadata")
    async def refresh_metadata(item_id: str):
        """Look the item up again in TMDB/GitHub/Open Library/its page for a poster, cover, ratings and links.
        No model is called, so it costs nothing and never changes the identification."""
        get_or_404(item_id)
        return await state["pipeline"].refresh_metadata(item_id)

    @app.post("/api/items/{item_id}/correct", status_code=202)
    def correct(item_id: str, correction: Correction, background: BackgroundTasks):
        """Fix a wrong identification. The item is re-enriched in the background."""
        get_or_404(item_id)
        fix = {k: (v.strip() if isinstance(v, str) else v) for k, v in correction.model_dump().items()}
        fix = {k: v for k, v in fix.items() if v not in (None, "")}
        if not fix:
            raise HTTPException(422, "Give at least one of title, category, year, canonical_url or hint")
        if "category" in fix and fix["category"] not in CATEGORIES:
            raise HTTPException(422, f"category must be one of {CATEGORIES}")
        if "canonical_url" in fix and not re.match(r"^https?://", fix["canonical_url"]):
            raise HTTPException(422, "canonical_url must start with http:// or https://")
        preview = {k: fix[k] for k in ("title", "category") if k in fix}
        item = db.update_item(item_id, status="processing", error=None, **preview)
        background.add_task(state["pipeline"].correct, item_id, fix)
        return item

    @app.delete("/api/items/{item_id}", status_code=204)
    def delete_item(item_id: str):
        item = get_or_404(item_id)
        db.delete_item(item_id)
        if item["image_file"]:
            (settings.uploads_dir / item["image_file"]).unlink(missing_ok=True)

    @app.get("/api/usage")
    def usage(days: int = Query(30, ge=1, le=366)):
        """Measured cost of identifying screenshots: totals, per screenshot, per analyzer, per day."""
        report = db.usage_report(days)
        report["config"] = {
            "analyzer": settings.resolved_analyzer(), "claude_model": settings.model, "effort": settings.effort,
            "provider": settings.hosted_llm if settings.hosted_llm in HOSTED_LLMS else "claude",
            "provider_model": settings.llm_model if settings.hosted_llm in HOSTED_LLMS else settings.model,
            "claude_batch": settings.claude_batch, "fetch_max_tokens": settings.fetch_max_tokens,
            "escalate_below": settings.escalate_below, "monthly_budget_usd": settings.monthly_budget_usd,
            "month_spent_usd": round(db.month_cost(), 4), "limits": LIMITS,
        }
        return report

    @app.get("/api/tags")
    def tags():
        return db.tag_counts()

    @app.get("/api/categories")
    def categories():
        return {"all": CATEGORIES, "counts": db.category_counts()}

    @app.get("/media/{name}")
    def media(name: str):
        path = (settings.uploads_dir / name).resolve()
        if path.parent != settings.uploads_dir.resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it can control the whole app; never cached so updates ship.
        return FileResponse(
            STATIC_DIR / "sw.js", media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.post("/share-target")
    def share_target_fallback():
        # Normally the service worker handles shares; if it isn't active yet, just open the app.
        return RedirectResponse("/", status_code=303)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _normalize_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "created_at must be ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


INTERRUPTED = "The server stopped before this finished. Re-analyze to try again."
SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I: easy to copy from a log


def _setup_code_ok(supplied: str | None, expected: str) -> bool:
    def normalize(value: str | None) -> bytes:
        return re.sub(r"[^A-Z0-9]", "", (value or "").upper()).encode()
    return bool(supplied) and hmac.compare_digest(normalize(supplied), normalize(expected))


class _Runtime:
    """What the server found at startup, and the database once it could be opened."""

    RETRY_SECONDS = 10

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = _DatabaseProxy(self)
        self.real_db: Database | None = None
        self.db_error: str | None = None
        self.analyzer_error: str | None = None
        self.startup_errors: list[str] = []
        self.worker_task: asyncio.Task | None = None
        # Guards PUT /api/settings while no access token is set; printed in the server log.
        self.setup_code = "-".join("".join(secrets.choice(SETUP_ALPHABET) for _ in range(5)) for _ in range(3))
        self._last_attempt = 0.0

    @property
    def db_ok(self) -> bool:
        return self.real_db is not None

    def open_database(self, retry: bool = False) -> None:
        if self.real_db is not None:
            return
        if retry and time.monotonic() - self._last_attempt < self.RETRY_SECONDS:
            return
        self._last_attempt = time.monotonic()
        try:
            self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
            db = Database(self.settings.db_path)
        except Exception as e:
            self.db_error = (f"can't use the data directory {self.settings.data_dir} ({type(e).__name__}: {e}). "
                             "Check that it exists and that the server may write to it (MAGPIE_DATA_DIR).")
            log.error("Storage unavailable: %s", self.db_error)
            return
        self.real_db, self.db_error = db, None
        try:
            if n := db.fail_interrupted(INTERRUPTED):
                log.warning("%d item(s) were interrupted by a restart and marked failed", n)
        except Exception:
            log.exception("Couldn't check for interrupted items")


class _DatabaseProxy:
    """Stands in for the Database: forwards to it, or answers 503 while it's unavailable."""

    def __init__(self, runtime: _Runtime):
        self._runtime = runtime

    def __getattr__(self, name: str):
        real = self._runtime.real_db
        if real is None:
            raise HTTPException(503, f"Storage unavailable: {self._runtime.db_error}")
        return getattr(real, name)


class _Unavailable:
    def __init__(self, reason: str):
        self.reason = reason

    async def analyze(self, *args, **kwargs):
        raise AnalysisError(f"No analyzer configured: {self.reason}")
