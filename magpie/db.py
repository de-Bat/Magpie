"""SQLite storage: items, tags, and a full-text index for retrieval."""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,          -- processing | ready | error
    error           TEXT,
    image_file      TEXT NOT NULL,
    image_hash      TEXT,                   -- SHA256 hash of the screenshot, for duplicate detection
    note            TEXT,
    category        TEXT,                   -- movie | tv_show | github_repo | recipe | ...
    source_platform TEXT,                   -- facebook | instagram | web | ...
    title           TEXT,
    subtitle        TEXT,
    summary         TEXT,
    canonical_url   TEXT,
    image_url       TEXT,                   -- poster / cover / preview image
    metadata        TEXT NOT NULL DEFAULT '{}',
    links           TEXT NOT NULL DEFAULT '[]',
    analysis        TEXT,                   -- raw model output, kept for debugging / re-enrichment
    confidence      INTEGER,                -- 0-100, how sure the model is; 100 once the user corrects it
    confidence_reason TEXT,
    alternatives    TEXT NOT NULL DEFAULT '[]',  -- other things it might be: [{title, category, year, canonical_url, why}]
    corrected       INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL DEFAULT 'screenshot',  -- screenshot | url
    source_url      TEXT                    -- the link that was shared (kind = url), normalized
);
CREATE INDEX IF NOT EXISTS items_category ON items(category);
CREATE INDEX IF NOT EXISTS items_created ON items(created_at);

CREATE TABLE IF NOT EXISTS tags (
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

-- Perceptual hashes of screenshots, for photo cleanup (cleanup.py). Computed on first use.
CREATE TABLE IF NOT EXISTS fingerprints (
    image_file TEXT PRIMARY KEY,    -- file name in the uploads directory
    bits       TEXT NOT NULL,       -- 64 hex characters
    aspect     REAL NOT NULL,       -- width / height
    thumb      BLOB NOT NULL        -- 54x96 grayscale pixels
);

-- Deleted item ids, so offline clients learn about deletions when they sync.
CREATE TABLE IF NOT EXISTS tombstones (
    id         TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tombstones_deleted ON tombstones(deleted_at);

-- What each analysis actually consumed and cost. Kept when items are deleted, for reporting.
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    purpose            TEXT NOT NULL,         -- analyze | reanalyze | correct
    analyzer           TEXT NOT NULL,         -- claude | local | ocr
    model              TEXT,
    mode               TEXT,                  -- realtime | batch | local
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    web_searches       INTEGER NOT NULL DEFAULT 0,
    web_fetches        INTEGER NOT NULL DEFAULT 0,
    requests           INTEGER NOT NULL DEFAULT 0,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    ok                 INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS runs_item ON analysis_runs(item_id);
CREATE INDEX IF NOT EXISTS runs_created ON analysis_runs(created_at);

-- Claude requests waiting for / inside a Message Batch.
CREATE TABLE IF NOT EXISTS batch_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    purpose      TEXT NOT NULL,
    params       TEXT NOT NULL,              -- Messages API request (JSON)
    context      TEXT NOT NULL,              -- OCR text, earlier runs, ... (JSON)
    batch_id     TEXT,                       -- set once submitted
    submitted_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    item_id UNINDEXED, title, summary, tags, body, tokenize='unicode61 remove_diacritics 2'
);
"""

JSON_COLUMNS = ("metadata", "links", "analysis", "alternatives", "related", "captures")

# Columns added after the first release; created on startup for existing databases.
MIGRATIONS = {
    "confidence": "INTEGER",
    "confidence_reason": "TEXT",
    "alternatives": "TEXT NOT NULL DEFAULT '[]'",
    "corrected": "INTEGER NOT NULL DEFAULT 0",
    "kind": "TEXT NOT NULL DEFAULT 'screenshot'",
    "source_url": "TEXT",
    "confirmed": "INTEGER NOT NULL DEFAULT 0",   # the user marked this identification as correct
    "related": "TEXT NOT NULL DEFAULT '[]'",     # worth-a-look links: [{kind, label, url, why?}]
    "retry_at": "TEXT",                          # failed because a model hit its limit: retried automatically then
    "image_hash": "TEXT",                        # SHA256 hash for duplicate screenshot detection
    "captures": "TEXT NOT NULL DEFAULT '[]'",    # further screenshots of the same thing: [{image_file, image_hash, original_hash?, created_at}]
    "original_hash": "TEXT",                     # SHA256 of the photo as it was on the device, when the client converted it before upload
}
# Below this confidence an identification is flagged for the user to check.
REVIEW_THRESHOLD = 60
# Metadata sources that confirm what an item is (as opposed to the model's say-so or a generic page card).
VERIFIED_CONFIDENCE = 90   # default: a confident model answer counts as verified (settings: MAGPIE_VERIFIED_CONFIDENCE)
VERIFYING_SOURCES = {"github", "tmdb", "tmdb+omdb", "omdb", "openlibrary", "schema.org/Recipe", "npm", "huggingface"}

EDITABLE_COLUMNS = {
    "status", "error", "note", "category", "source_platform", "title", "subtitle",
    "summary", "canonical_url", "image_url", "metadata", "links", "analysis",
    "confidence", "confidence_reason", "alternatives", "corrected", "confirmed", "related", "retry_at", "captures",
}


def now() -> str:
    # Microsecond precision: sync cursors compare these strings.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_tag(tag: str) -> str:
    tag = re.sub(r"\s+", "-", tag.strip().lower().lstrip("#"))
    return re.sub(r"[^\w\-+.]", "", tag)[:40]


class Database:
    def __init__(self, path: Path | str, verified_confidence=None):
        # () -> the confidence from which an answer counts as verified (a callable, so settings changes apply at once)
        self.verified_confidence = verified_confidence or (lambda: VERIFIED_CONFIDENCE)
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
        with self.conn:
            for column, ddl in MIGRATIONS.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE items ADD COLUMN {column} {ddl}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS items_source_url ON items(source_url)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS items_image_hash ON items(image_hash)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS items_original_hash ON items(original_hash)")

    def fail_interrupted(self, message: str) -> int:
        """Items left 'processing' by a previous run (the server stopped mid-analysis) would wait
        forever; mark them failed so the user can re-analyze. Items queued for a batch are fine."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE items SET status = 'error', error = ?, updated_at = ? WHERE status = 'processing' "
                "AND id NOT IN (SELECT item_id FROM batch_jobs)", (message, now()))
        return cur.rowcount

    # ---- items -------------------------------------------------------------

    def create_item(
        self,
        image_file: str,
        note: str | None = None,
        tags: list[str] | None = None,
        item_id: str | None = None,
        created_at: str | None = None,
        kind: str = "screenshot",
        source_url: str | None = None,
        image_hash: str | None = None,
        original_hash: str | None = None,
    ) -> dict:
        """Create an item. Clients may supply the id (so offline uploads can be retried safely)
        and the original capture time."""
        item_id = item_id or uuid.uuid4().hex[:12]
        ts = now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO items (id, created_at, updated_at, status, image_file, image_hash, original_hash, note, kind, source_url) "
                "VALUES (?, ?, ?, 'processing', ?, ?, ?, ?, ?, ?)",
                (item_id, created_at or ts, ts, image_file, image_hash, original_hash, note, kind, source_url),
            )
            self.conn.execute("DELETE FROM tombstones WHERE id = ?", (item_id,))
        if tags:
            self.set_tags(item_id, tags)
        self._reindex(item_id)
        return self.get_item(item_id)

    def update_item(self, item_id: str, **fields: Any) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in EDITABLE_COLUMNS}
        if "status" in fields and "retry_at" not in fields:
            fields["retry_at"] = None   # any new outcome replaces a pending automatic retry
        if fields:
            for col in JSON_COLUMNS:
                if col in fields and not isinstance(fields[col], str) and fields[col] is not None:
                    fields[col] = json.dumps(fields[col], ensure_ascii=False)
            fields["updated_at"] = now()
            assignments = ", ".join(f"{k} = ?" for k in fields)
            with self.conn:
                self.conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*fields.values(), item_id))
            self._reindex(item_id)
        return self.get_item(item_id)

    def due_retries(self, limit: int = 5) -> list[str]:
        """Items that failed because a model hit its limit, whose retry time has come."""
        rows = self.conn.execute(
            "SELECT id FROM items WHERE status = 'error' AND retry_at IS NOT NULL AND retry_at <= ? ORDER BY retry_at LIMIT ?",
            (now(), limit)).fetchall()
        return [r["id"] for r in rows]

    def waiting_for_limits(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM items WHERE status = 'error' AND retry_at IS NOT NULL").fetchone()[0]

    def requests_since(self, analyzer: str, model: str, since: str) -> int:
        """Requests made to one provider's model since a time (for providers that don't report their limits)."""
        row = self.conn.execute("SELECT COALESCE(SUM(requests), 0) AS n FROM analysis_runs "
                                "WHERE analyzer = ? AND model = ? AND created_at >= ?", (analyzer, model, since)).fetchone()
        return int(row["n"])

    def get_item(self, item_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def delete_item(self, item_id: str) -> dict | None:
        item = self.get_item(item_id)
        if item:
            with self.conn:
                self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
                self.conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
                self.conn.execute("INSERT OR REPLACE INTO tombstones (id, deleted_at) VALUES (?, ?)", (item_id, now()))
                self.conn.execute("DELETE FROM batch_jobs WHERE item_id = ? AND batch_id IS NULL", (item_id,))
        return item

    def list_items(
        self,
        q: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        needs_review: bool = False,
        unverified: bool = False,
        to_check: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        where, params = [], []
        order = "i.created_at DESC"
        join = ""
        match = fts_query(q) if q else None
        if match:
            join = "JOIN items_fts f ON f.item_id = i.id"
            where.append("items_fts MATCH ?")
            params.append(match)
            order = "bm25(items_fts), i.created_at DESC"
        if category:
            where.append("i.category = ?")
            params.append(category)
        if needs_review:
            where.append(f"i.status = 'ready' AND i.corrected = 0 AND i.confidence < {REVIEW_THRESHOLD}")
        for tag in tags or []:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.item_id = i.id AND t.tag = ?)")
            params.append(normalize_tag(tag))
        sql = f"SELECT i.* FROM items i {join}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        if to_check:  # failed, unsure or unverified: derived per item, so filter after loading
            rows = self.conn.execute(sql.replace(" LIMIT ? OFFSET ?", ""), params).fetchall()
            found = [i for i in (self._row_to_item(r) for r in rows) if i["to_check"]]
            return found[offset:offset + limit]
        if unverified:  # derived from each item's metadata sources, so filter after loading
            rows = self.conn.execute(sql.replace(" LIMIT ? OFFSET ?", ""), params).fetchall()
            found = [i for i in (self._row_to_item(r) for r in rows) if i["status"] == "ready" and not i["verified"]]
            return found[offset:offset + limit]
        rows = self.conn.execute(sql, (*params, limit, offset)).fetchall()
        return [self._row_to_item(r) for r in rows]

    def find_by_image_hash(self, *hashes: str | None) -> dict | None:
        """Item with the same screenshot, if any: as uploaded, or as it was on the device before the client converted it."""
        for h in dict.fromkeys(h for h in hashes if h):
            row = self.conn.execute("SELECT id FROM items WHERE image_hash = ? OR original_hash = ? ORDER BY created_at LIMIT 1",
                                    (h, h)).fetchone()
            if not row:   # a screenshot merged into another card earlier
                row = self.conn.execute("SELECT id FROM items WHERE captures LIKE ? OR captures LIKE ? ORDER BY created_at LIMIT 1",
                                        (f'%"image_hash": "{h}"%', f'%"original_hash": "{h}"%')).fetchone()
            if row:
                return self.get_item(row["id"])
        return None

    def merge_capture(self, new_id: str, into_id: str) -> dict | None:
        """The new item shows something already in the library: its screenshot joins the existing card
        (the image file is kept) and the new item goes away."""
        new, target = self.get_item(new_id), self.get_item(into_id)
        if not new or not target:
            return target
        captures = list(target.get("captures") or [])
        if new.get("image_file"):
            captures.append({"image_file": new["image_file"], "image_hash": new.get("image_hash"),
                             **({"original_hash": new["original_hash"]} if new.get("original_hash") else {}),
                             "created_at": new["created_at"]})
            captures += new.get("captures") or []
        with self.conn:
            self.conn.execute("DELETE FROM items WHERE id = ?", (new_id,))
            self.conn.execute("DELETE FROM items_fts WHERE item_id = ?", (new_id,))
            self.conn.execute("INSERT OR REPLACE INTO tombstones (id, deleted_at) VALUES (?, ?)", (new_id, now()))
            self.conn.execute("DELETE FROM batch_jobs WHERE item_id = ? AND batch_id IS NULL", (new_id,))
        self.set_tags(into_id, sorted({*target.get("tags", []), *new.get("tags", [])}))
        note = "\n".join(n for n in dict.fromkeys([target.get("note"), new.get("note")]) if n) or None
        return self.update_item(into_id, captures=captures, note=note)

    def find_by_canonical_url(self, url: str) -> dict | None:
        """First analyzed item with the same canonical URL, if any."""
        if not url:
            return None
        row = self.conn.execute("SELECT id FROM items WHERE canonical_url = ? AND status = 'ready' ORDER BY created_at LIMIT 1", (url,)).fetchone()
        return self.get_item(row["id"]) if row else None

    def find_by_source_url(self, url: str) -> dict | None:
        row = self.conn.execute("SELECT id FROM items WHERE source_url = ? ORDER BY created_at LIMIT 1", (url,)).fetchone()
        return self.get_item(row["id"]) if row else None

    def is_deleted(self, item_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM tombstones WHERE id = ?", (item_id,)).fetchone() is not None

    def changes_since(self, since: str | None) -> dict:
        """Everything a client needs to catch up: items changed and ids deleted after `since`."""
        server_time = now()
        if since:
            rows = self.conn.execute(
                "SELECT * FROM items WHERE updated_at >= ? ORDER BY updated_at", (since,)
            ).fetchall()
            deleted = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tombstones WHERE deleted_at >= ?", (since,)
            ).fetchall()]
        else:
            rows = self.conn.execute("SELECT * FROM items ORDER BY updated_at").fetchall()
            deleted = []
        return {"server_time": server_time, "items": [self._row_to_item(r) for r in rows], "deleted": deleted}

    # ---- usage ---------------------------------------------------------------

    def record_runs(self, item_id: str, runs: list[dict], purpose: str = "analyze") -> None:
        cols = ("analyzer", "model", "mode", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "web_searches", "web_fetches", "requests", "duration_ms", "cost_usd", "ok")

        def value(run: dict, col: str):
            if col in ("analyzer", "model", "mode"):
                return run.get(col) or ("unknown" if col == "analyzer" else None)
            if col == "ok":
                return int(bool(run.get("ok", True)))
            return run.get(col) or 0

        with self.conn:
            self.conn.executemany(
                f"INSERT INTO analysis_runs (item_id, created_at, purpose, {', '.join(cols)}) "
                f"VALUES (?, ?, ?, {', '.join('?' for _ in cols)})",
                [(item_id, now(), purpose, *(value(r, c) for c in cols)) for r in runs],
            )

    def month_cost(self) -> float:
        """Measured spend since the start of the current (UTC) month."""
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="microseconds")
        return self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM analysis_runs WHERE created_at >= ?", (start,)).fetchone()[0]

    def usage_report(self, days: int = 30) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")
        q = lambda sql, *a: [dict(r) for r in self.conn.execute(sql, (since, *a)).fetchall()]  # noqa: E731
        totals = q("""SELECT COUNT(DISTINCT item_id) AS screenshots, COUNT(*) AS runs,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens, COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(web_searches), 0) AS web_searches, COALESCE(SUM(web_fetches), 0) AS web_fetches
                      FROM analysis_runs WHERE created_at >= ?""")[0]
        by_analyzer = q("""SELECT analyzer, mode, model, COUNT(*) AS runs, COUNT(DISTINCT item_id) AS screenshots,
                           SUM(cost_usd) AS cost_usd, AVG(cost_usd) AS avg_cost_usd, SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens, SUM(web_searches) AS web_searches,
                           AVG(duration_ms) AS avg_duration_ms, SUM(1 - ok) AS failures
                           FROM analysis_runs WHERE created_at >= ? GROUP BY analyzer, mode, model ORDER BY cost_usd DESC""")
        by_day = q("""SELECT substr(created_at, 1, 10) AS day, COUNT(DISTINCT item_id) AS screenshots, SUM(cost_usd) AS cost_usd
                      FROM analysis_runs WHERE created_at >= ? GROUP BY day ORDER BY day""")
        escalated = q("""SELECT COUNT(DISTINCT item_id) AS n FROM analysis_runs
                         WHERE created_at >= ? AND mode IN ('realtime', 'batch', 'hosted')""")[0]["n"]
        by_day_mode = q("""SELECT substr(created_at, 1, 10) AS day, mode, SUM(cost_usd) AS cost_usd
                           FROM analysis_runs WHERE created_at >= ? GROUP BY day, mode ORDER BY day""")
        by_category = q("""SELECT CASE WHEN i.id IS NULL THEN 'deleted' ELSE COALESCE(i.category, 'other') END AS category,
                           SUM(r.cost_usd) AS cost_usd, COUNT(DISTINCT r.item_id) AS items
                           FROM analysis_runs r LEFT JOIN items i ON i.id = r.item_id
                           WHERE r.created_at >= ? GROUP BY category ORDER BY cost_usd DESC""")
        top_items = q("""SELECT r.item_id, i.title, i.category, SUM(r.cost_usd) AS cost_usd, COUNT(*) AS runs
                         FROM analysis_runs r LEFT JOIN items i ON i.id = r.item_id
                         WHERE r.created_at >= ? GROUP BY r.item_id HAVING SUM(r.cost_usd) > 0 ORDER BY SUM(r.cost_usd) DESC LIMIT 5""")
        n = totals["screenshots"] or 0
        per = totals["cost_usd"] / n if n else 0.0
        active_days = len(by_day) or 1
        paid_cost = sum(r["cost_usd"] or 0 for r in by_analyzer if r["mode"] in ("realtime", "batch", "hosted"))
        saved = (n - escalated) * (paid_cost / escalated) if escalated else 0.0  # what answering locally kept out of the bill
        return {
            "period_days": days,
            "totals": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in totals.items()},
            "per_screenshot_usd": round(per, 4),
            "cloud_share": round(escalated / n, 3) if n else 0.0,  # sent to any paid provider
            "claude_share": round(escalated / n, 3) if n else 0.0,  # legacy alias (older clients)
            "projected_30d_usd": round(totals["cost_usd"] / min(days, max(active_days, 1)) * 30, 2) if n else 0.0,
            "by_analyzer": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in by_analyzer],
            "by_day": [{**r, "cost_usd": round(r["cost_usd"], 4)} for r in by_day],
            "by_day_mode": [{**r, "cost_usd": round(r["cost_usd"] or 0, 5)} for r in by_day_mode],
            "by_category": [{**r, "cost_usd": round(r["cost_usd"] or 0, 4)} for r in by_category],
            "top_items": [{**r, "cost_usd": round(r["cost_usd"] or 0, 4)} for r in top_items],
            "paid_screenshots": escalated,
            "saved_by_local_usd": round(saved, 2),
        }

    def usage_rows(self, days: int = 30) -> list[dict]:
        """One row per model call, for the CSV export."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")
        rows = self.conn.execute(
            """SELECT r.created_at, r.item_id, i.title, r.purpose, r.analyzer, r.model, r.mode, r.input_tokens, r.output_tokens,
                      r.cache_read_tokens, r.cache_write_tokens, r.web_searches, r.web_fetches, r.requests, r.duration_ms, r.cost_usd, r.ok
               FROM analysis_runs r LEFT JOIN items i ON i.id = r.item_id WHERE r.created_at >= ? ORDER BY r.created_at""", (since,)).fetchall()
        return [dict(r) for r in rows]

    # ---- batch jobs ----------------------------------------------------------

    def add_batch_job(self, item_id: str, purpose: str, params: dict, context: dict) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM batch_jobs WHERE item_id = ? AND batch_id IS NULL", (item_id,))
            self.conn.execute(
                "INSERT INTO batch_jobs (item_id, created_at, purpose, params, context) VALUES (?, ?, ?, ?, ?)",
                (item_id, now(), purpose, json.dumps(params), json.dumps(context)),
            )

    def unsubmitted_jobs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM batch_jobs WHERE batch_id IS NULL ORDER BY id")]

    def mark_submitted(self, job_ids: list[int], batch_id: str) -> None:
        with self.conn:
            self.conn.executemany("UPDATE batch_jobs SET batch_id = ?, submitted_at = ? WHERE id = ?",
                                  [(batch_id, now(), j) for j in job_ids])

    def open_batches(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT batch_id FROM batch_jobs WHERE batch_id IS NOT NULL")]

    def batch_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM batch_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def delete_batch_job(self, job_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM batch_jobs WHERE id = ?", (job_id,))

    # ---- tags --------------------------------------------------------------

    def set_tags(self, item_id: str, tags: list[str]) -> list[str]:
        clean = sorted({t for t in (normalize_tag(t) for t in tags) if t})
        with self.conn:
            self.conn.execute("DELETE FROM tags WHERE item_id = ?", (item_id,))
            self.conn.executemany("INSERT INTO tags (item_id, tag) VALUES (?, ?)", [(item_id, t) for t in clean])
            self.conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (now(), item_id))
        self._reindex(item_id)
        return clean

    def add_tags(self, item_id: str, tags: list[str]) -> list[str]:
        return self.set_tags(item_id, self.get_tags(item_id) + list(tags))

    def get_tags(self, item_id: str) -> list[str]:
        rows = self.conn.execute("SELECT tag FROM tags WHERE item_id = ? ORDER BY tag", (item_id,)).fetchall()
        return [r["tag"] for r in rows]

    def tag_counts(self) -> list[dict]:
        rows = self.conn.execute("SELECT tag, COUNT(*) AS count FROM tags GROUP BY tag ORDER BY count DESC, tag").fetchall()
        return [dict(r) for r in rows]

    def category_counts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) AS count FROM items WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- internals ---------------------------------------------------------

    def _row_to_item(self, row: sqlite3.Row) -> dict:
        item = dict(row)
        for col in JSON_COLUMNS:
            if item.get(col):
                try:
                    item[col] = json.loads(item[col])
                except json.JSONDecodeError:
                    pass
        item.setdefault("metadata", {})
        item["corrected"] = bool(item.get("corrected"))
        item["needs_review"] = (
            item.get("status") == "ready" and not item["corrected"]
            and item.get("confidence") is not None and item["confidence"] < REVIEW_THRESHOLD
        )
        sources = (item.get("metadata") or {}).get("sources") or []
        item["confirmed"] = bool(item.get("confirmed"))
        item["verified"] = (item["corrected"] or item["confirmed"] or bool(VERIFYING_SOURCES.intersection(sources))
                            or (item.get("confidence") or 0) >= self.verified_confidence())
        item["to_check"] = (item.get("status") == "error" or item["needs_review"]
                            or (item.get("status") == "ready" and not item["verified"]))
        item["tags"] = self.get_tags(item["id"])
        cost = self.conn.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost, "
            "COALESCE(SUM(web_searches), 0) AS searches, GROUP_CONCAT(DISTINCT analyzer || ':' || mode) AS how "
            "FROM analysis_runs WHERE item_id = ?", (item["id"],),
        ).fetchone()
        item["usage"] = {
            "cost_usd": round(cost["cost"], 4), "runs": cost["runs"], "web_searches": cost["searches"],
            "via": sorted((cost["how"] or "").split(",")) if cost["how"] else [],
        }
        last = self.conn.execute(
            "SELECT analyzer, model FROM analysis_runs WHERE item_id = ? AND ok = 1 AND analyzer != 'ocr' "
            "ORDER BY id DESC LIMIT 1", (item["id"],)).fetchone()
        item["usage"]["model"] = (last["model"] or last["analyzer"]) if last else None
        item["batch_pending"] = self.conn.execute(
            "SELECT 1 FROM batch_jobs WHERE item_id = ? LIMIT 1", (item["id"],)).fetchone() is not None
        return item

    def _reindex(self, item_id: str) -> None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return
        tags = " ".join(self.get_tags(item_id))
        body_parts = [row["subtitle"], row["note"], row["category"], row["source_platform"], row["canonical_url"], row["source_url"]]
        try:
            meta = json.loads(row["metadata"] or "{}")
            body_parts.extend(_flatten_text(meta))
        except json.JSONDecodeError:
            pass
        body = " ".join(str(p) for p in body_parts if p)
        with self.conn:
            self.conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
            self.conn.execute(
                "INSERT INTO items_fts (item_id, title, summary, tags, body) VALUES (?, ?, ?, ?, ?)",
                (item_id, row["title"] or "", row["summary"] or "", tags.replace("-", " ") + " " + tags, body),
            )


def _flatten_text(value: Any) -> list[str]:
    """Collect searchable strings from metadata (skipping URLs, which only add noise)."""
    out: list[str] = []
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_text(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_flatten_text(v))
    elif isinstance(value, str) and not value.startswith("http"):
        out.append(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out.append(str(value))
    return out


def fts_query(q: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match, as a prefix."""
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words)
