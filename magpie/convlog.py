"""Conversation logs: what Magpie sent to each AI model and what came back, for debriefing and inspection.

One file per session, a session being one operation on one item (an analysis, a re-analysis, a correction, a bulk
re-analysis, the result of a Claude batch):

    <data dir>/logs/2026-10-02/153045_analyze_ab12cd34.jsonl

The name is the UTC start time, the operation and the item. Each line is one event, written as it happens (so a crash
leaves what was said up to then):

    session_start   operation, item, what the analyzer was configured to do (never a key)
    ocr             what the OCR pre-pass read
    llm_request     provider, model, mode, the system prompt, the messages (images replaced by their size and hash,
                    or saved next to the log when images are kept), the parameters
    llm_response    status, time, the reply (text, tool call, thinking), tokens, rate-limit headers
    decision        a choice the pipeline made: escalate to the fallback, keep the local answer, link repaired, ...
    batch_queued    the request left for Claude's Message Batches (its result gets its own batch_result session)
    result          what was stored for the item
    session_end     outcome, error, duration, cost

Off by default: logs contain what is in your screenshots. Settings → Logging (MAGPIE_LOG_CONVERSATIONS).
"""

import base64
import contextvars
import hashlib
import json
import logging
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

MAX_FIELD = 60_000          # characters of one string kept in a log line; longer text is cut with a note
MAX_SESSION_BYTES = 8_000_000
KEEP_HEADERS = ("retry-after", "content-type", "x-request-id", "request-id", "anthropic-request-id")

_settings: Any = None
_session: contextvars.ContextVar["Session | None"] = contextvars.ContextVar("magpie_log_session", default=None)
_last_cleanup: float | None = None


def configure(settings: Any) -> None:
    """Remember the settings object (read on every session, so changes made in the app apply at once)."""
    global _settings
    _settings = settings


def enabled() -> bool:
    return bool(_settings is not None and getattr(_settings, "log_conversations", False))


def log_dir() -> Path:
    return Path(_settings.data_dir) / "logs"


# ---- making things safe to write -------------------------------------------------------------------------------


def _cut(text: str) -> str:
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD] + f"… [{len(text) - MAX_FIELD} more characters not kept]"


def jsonable(value: Any, depth: int = 0) -> Any:
    """Anything an SDK or an API returned, as plain JSON, with long strings cut."""
    if depth > 12:
        return "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _cut(value)
    if isinstance(value, bytes):
        return f"[{len(value)} bytes]"
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return jsonable(dump(mode="json", exclude_none=True), depth + 1)
        except Exception:
            pass
    return _cut(repr(value))


def _is_data_uri(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("data:") and ";base64," in value


def _redact_images(value: Any, session: "Session | None") -> Any:
    """Copy of a request with every image replaced by what it was (type, size, hash), or by the saved file's name."""
    if isinstance(value, list):
        return [_redact_images(v, session) for v in value]
    if not isinstance(value, dict):
        return value
    # OpenAI style: {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    url = (value.get("image_url") or {}).get("url") if isinstance(value.get("image_url"), dict) else None
    if value.get("type") == "image_url" and _is_data_uri(url):
        media, _, data = url[5:].partition(";base64,")
        return {"type": "image", **_image_note(session, media, base64.b64decode(data))}
    # Anthropic style: {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
    source = value.get("source")
    if value.get("type") == "image" and isinstance(source, dict) and source.get("type") == "base64":
        return {"type": "image", **_image_note(session, source.get("media_type"), base64.b64decode(source.get("data") or ""))}
    return {k: _redact_images(v, session) for k, v in value.items()}


def _image_note(session: "Session | None", media_type: str | None, data: bytes) -> dict:
    note: dict[str, Any] = {"media_type": media_type, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16]}
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            note["size"] = f"{im.width}x{im.height}"
    except Exception:
        pass
    if session is not None and getattr(_settings, "log_images", False):
        note["file"] = session.save_image(data, media_type)
    return note


# ---- a session --------------------------------------------------------------------------------------------------


class Session:
    def __init__(self, operation: str, item_id: str | None, meta: dict):
        now = datetime.now(timezone.utc)
        self.started = time.monotonic()
        self.operation, self.item_id = operation, item_id
        day = now.strftime("%Y-%m-%d")
        base = f"{now.strftime('%H%M%S')}_{_slug(operation)}_{(item_id or 'none')[:8]}"
        folder = log_dir() / day
        folder.mkdir(parents=True, exist_ok=True)
        name, n = base, 1
        while (folder / f"{name}.jsonl").exists():
            n += 1
            name = f"{base}-{n}"
        self.id = f"{day}/{name}"
        self.path = folder / f"{name}.jsonl"
        self.bytes = 0
        self.images = 0
        self.llm_calls = 0
        self.cost = 0.0
        self.capped = False
        self.add("session_start", operation=operation, item_id=item_id, **meta)

    def add(self, event: str, **data: Any) -> None:
        if self.capped:
            return
        try:
            line = json.dumps({"t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, **jsonable(data)},
                              ensure_ascii=False) + "\n"
            if self.bytes + len(line) > MAX_SESSION_BYTES:
                self.capped = True
                line = json.dumps({"t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": "truncated",
                                   "note": "This session reached its size limit; the rest is not kept."}) + "\n"
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
            self.bytes += len(line)
        except Exception:   # logging never breaks an analysis
            log.exception("Could not write the conversation log")

    def save_image(self, data: bytes, media_type: str | None) -> str:
        self.images += 1
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(media_type or "", "bin")
        name = f"{self.path.stem}.{self.images}.{ext}"
        (self.path.parent / name).write_bytes(data)
        return name


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "op").lower()).strip("-")[:24] or "op"


@contextmanager
def session(operation: str, item_id: str | None = None, **meta: Any) -> Iterator["Session | None"]:
    """Log everything said to models while this block runs. Does nothing when logging is off or a session is
    already open (a re-analysis started inside a bulk job is logged as its own session by its own call)."""
    if not enabled() or _session.get() is not None:
        yield None
        return
    try:
        cleanup()
        s = Session(operation, item_id, meta)
    except Exception:
        log.exception("Could not start a conversation log")
        yield None
        return
    token = _session.set(s)
    outcome: dict[str, Any] = {"outcome": "ok"}
    try:
        yield s
    except BaseException as e:
        outcome = {"outcome": "error", "error": f"{type(e).__name__}: {e}"}
        raise
    finally:
        _session.reset(token)
        s.add("session_end", duration_ms=int((time.monotonic() - s.started) * 1000), llm_calls=s.llm_calls,
              cost_usd=round(s.cost, 5), **{**outcome, **getattr(s, "outcome", {})})


def current() -> "Session | None":
    return _session.get()


def event(kind: str, **data: Any) -> None:
    s = _session.get()
    if s is not None:
        s.add(kind, **data)


def set_outcome(**data: Any) -> None:
    """What the session ends with (overrides the default "ok")."""
    s = _session.get()
    if s is not None:
        s.outcome = {**getattr(s, "outcome", {}), **jsonable(data)}


def log_request(provider: str, model: str | None, mode: str, **payload: Any) -> None:
    s = _session.get()
    if s is None:
        return
    s.llm_calls += 1
    s.add("llm_request", provider=provider, model=model, mode=mode, **{k: _redact_images(jsonable(v), s) for k, v in payload.items()})


def log_response(provider: str, model: str | None, *, headers: Any = None, cost: float | None = None, **payload: Any) -> None:
    s = _session.get()
    if s is None:
        return
    if cost:
        s.cost += cost
    hdrs = {}
    for k, v in dict(headers or {}).items():
        if str(k).lower() in KEEP_HEADERS or "ratelimit" in str(k).lower():
            hdrs[str(k).lower()] = v
    s.add("llm_response", provider=provider, model=model, **({"headers": hdrs} if hdrs else {}), **payload)


# ---- retention --------------------------------------------------------------------------------------------------


def cleanup(force: bool = False) -> int:
    """Delete day folders older than the retention setting (0 = keep everything). At most once an hour."""
    global _last_cleanup
    days = int(getattr(_settings, "log_retention_days", 0) or 0)
    if not days or (not force and _last_cleanup is not None and time.monotonic() - _last_cleanup < 3600):
        return 0
    _last_cleanup = time.monotonic()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    removed = 0
    for folder in sorted(log_dir().glob("????-??-??")) if log_dir().exists() else []:
        if folder.is_dir() and folder.name < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


# ---- reading them back ------------------------------------------------------------------------------------------

_ID = re.compile(r"^\d{4}-\d{2}-\d{2}/[\w-]+$")


def _path(session_id: str) -> Path | None:
    if not _ID.match(session_id or ""):
        return None   # no path tricks: only ids this module makes
    p = log_dir() / f"{session_id}.jsonl"
    return p if p.is_file() else None


def _events(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def summarize(p: Path) -> dict:
    events = _events(p)
    start = next((e for e in events if e["event"] == "session_start"), {})
    end = next((e for e in reversed(events) if e["event"] == "session_end"), {})
    reqs = [e for e in events if e["event"] == "llm_request"]
    result = next((e for e in reversed(events) if e["event"] == "result"), {})
    return {
        "id": f"{p.parent.name}/{p.stem}", "started": start.get("t") or events[0]["t"] if events else None,
        "operation": start.get("operation"), "item_id": start.get("item_id"), "title": result.get("title") or start.get("item_title"),
        "models": sorted({f"{e.get('provider')}:{e.get('model')}" for e in reqs}), "calls": len(reqs),
        "outcome": end.get("outcome") or "running", "error": end.get("error"), "cost_usd": end.get("cost_usd"),
        "duration_ms": end.get("duration_ms"), "bytes": p.stat().st_size,
        "confidence": result.get("confidence"), "category": result.get("category"),
    }


def list_sessions(limit: int = 100, offset: int = 0, operation: str | None = None, item_id: str | None = None,
                  outcome: str | None = None) -> dict:
    if not log_dir().exists():
        return {"sessions": [], "total": 0}
    files = sorted(log_dir().glob("????-??-??/*.jsonl"), key=lambda p: (p.parent.name, p.stem), reverse=True)
    rows = []
    for p in files:
        # the operation and item are in the name: filter before reading anything
        parts = p.stem.split("_", 2)
        if operation and (len(parts) < 2 or parts[1] != _slug(operation)):
            continue
        if item_id and (len(parts) < 3 or not parts[2].startswith(item_id[:8])):
            continue
        rows.append(p)
    total_rows = rows
    if outcome:
        rows = [p for p in rows if summarize(p)["outcome"] == outcome]
        return {"sessions": [summarize(p) for p in rows[offset:offset + limit]], "total": len(rows)}
    return {"sessions": [summarize(p) for p in total_rows[offset:offset + limit]], "total": len(total_rows)}


def read_session(session_id: str) -> dict | None:
    p = _path(session_id)
    if not p:
        return None
    return {**summarize(p), "events": _events(p), "files": sorted(f.name for f in p.parent.glob(f"{p.stem}.*") if f.suffix != ".jsonl")}


def raw_session(session_id: str) -> Path | None:
    return _path(session_id)


def image_file(session_id: str, name: str) -> Path | None:
    p = _path(session_id)
    if not p or not re.fullmatch(re.escape(p.stem) + r"\.\d+\.\w+", name or ""):
        return None
    f = p.parent / name
    return f if f.is_file() else None


def delete_session(session_id: str) -> bool:
    p = _path(session_id)
    if not p:
        return False
    for f in p.parent.glob(f"{p.stem}.*"):
        f.unlink(missing_ok=True)
    try:
        p.parent.rmdir()   # the day folder, once empty
    except OSError:
        pass
    return True


def delete_all() -> int:
    n = len(list(log_dir().glob("????-??-??/*.jsonl"))) if log_dir().exists() else 0
    if log_dir().exists():
        shutil.rmtree(log_dir(), ignore_errors=True)
    return n


def disk_usage() -> dict:
    files = [f for f in log_dir().rglob("*") if f.is_file()] if log_dir().exists() else []
    return {"files": len([f for f in files if f.suffix == ".jsonl"]), "bytes": sum(f.stat().st_size for f in files)}


def render_markdown(session_id: str) -> str | None:
    """A readable transcript: the prompts and replies in order, for pasting into a bug report or reading in an editor."""
    data = read_session(session_id)
    if not data:
        return None
    out = [f"# {data['operation']} · {data['started']}", "",
           f"Item `{data['item_id']}` — {data.get('title') or 'untitled'} · outcome **{data['outcome']}**"
           + (f" · ${data['cost_usd']}" if data.get("cost_usd") else "") + (f" · {data['error']}" if data.get("error") else ""), ""]
    for e in data["events"]:
        kind, t = e["event"], (e.get("t") or "")[11:23]
        body = {k: v for k, v in e.items() if k not in ("t", "event")}
        if kind == "llm_request":
            out += [f"## {t} → {e.get('provider')}:{e.get('model')} ({e.get('mode')})", ""]
            if e.get("system"):
                out += ["**System**", "", "```", str(e["system"]), "```", ""]
            for m in e.get("messages") or []:
                out += [f"**{m.get('role', '?')}**", "", _content_md(m.get("content")), ""]
            rest = {k: v for k, v in body.items() if k not in ("provider", "model", "mode", "system", "messages")}
            if rest:
                out += ["<details><summary>parameters</summary>", "", "```json", json.dumps(rest, ensure_ascii=False, indent=1), "```", "</details>", ""]
        elif kind == "llm_response":
            out += [f"## {t} ← {e.get('provider')}:{e.get('model')}" + (f" · HTTP {e['status']}" if e.get("status") else "")
                    + (f" · {e['duration_ms']} ms" if e.get("duration_ms") else ""), ""]
            if e.get("text") is not None:
                out += ["```", str(e["text"]), "```", ""]
            if e.get("content") is not None:
                out += ["```json", json.dumps(e["content"], ensure_ascii=False, indent=1), "```", ""]
            extra = {k: v for k, v in body.items() if k not in ("provider", "model", "text", "content", "status", "duration_ms")}
            if extra:
                out += ["<details><summary>usage and headers</summary>", "", "```json", json.dumps(extra, ensure_ascii=False, indent=1), "```", "</details>", ""]
        else:
            out += [f"### {t} {kind}", "", "```json", json.dumps(body, ensure_ascii=False, indent=1), "```", ""]
    return "\n".join(out)


def _content_md(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text")))
        elif isinstance(c, dict) and c.get("type") == "image":
            parts.append(f"*[image {c.get('media_type')} {c.get('size') or ''} {c.get('bytes')} bytes" + (f" → {c['file']}" if c.get("file") else "") + "]*")
        else:
            parts.append("```json\n" + json.dumps(c, ensure_ascii=False, indent=1) + "\n```")
    return "\n\n".join(parts)
