"""Whole-library jobs: look everything up again, or identify everything again.

One job at a time, in the background, a few items at a time so the server stays responsive. Progress is
kept in memory and polled by the settings screen; a restart stops the job (items already done stay done).
"""

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

KINDS = {"refresh": 4, "reanalyze": 2}          # job -> how many items at once
SCOPES = ("all", "check", "failed")


class BulkBusy(Exception):
    pass


class BulkRunner:
    def __init__(self, db: Any, pipeline: Callable[[], Any]):
        self.db, self._pipeline = db, pipeline
        self.state: dict[str, Any] = self._idle()
        self._task: asyncio.Task | None = None
        self._cancel = False

    @staticmethod
    def _idle() -> dict:
        return {"running": False, "kind": None, "scope": None, "total": 0, "done": 0, "failed": 0, "cancelled": False,
                "started": None, "finished": None}

    def snapshot(self) -> dict:
        return dict(self.state)

    def select(self, scope: str, skip_confirmed: bool = False) -> list[str]:
        """The ids a job would cover, oldest first. Items that are being analyzed right now are never included."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        items = self.db.list_items(to_check=True, limit=100_000) if scope == "check" else self.db.list_items(limit=100_000)
        if scope == "failed":
            items = [i for i in items if i["status"] == "error"]
        items = [i for i in items if i["status"] != "processing" and not i.get("pending_upload") and not i.get("batch_pending")]
        if skip_confirmed:
            items = [i for i in items if not (i.get("corrected") or i.get("confirmed"))]
        return [i["id"] for i in sorted(items, key=lambda i: i.get("created_at") or "")]

    def start(self, kind: str, scope: str, skip_confirmed: bool = False) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {tuple(KINDS)}")
        if self.state["running"]:
            raise BulkBusy("A library job is already running.")
        ids = self.select(scope, skip_confirmed)
        self._cancel = False
        self.state = {**self._idle(), "running": bool(ids), "kind": kind, "scope": scope, "total": len(ids),
                      "started": time.time(), "finished": None if ids else time.time()}
        if ids:
            self._task = asyncio.create_task(self._run(kind, ids))
        return self.snapshot()

    def cancel(self) -> dict:
        self._cancel = True
        return self.snapshot()

    async def _run(self, kind: str, ids: list[str]) -> None:
        gate = asyncio.Semaphore(KINDS[kind])

        async def one(item_id: str) -> None:
            async with gate:
                if self._cancel:
                    return
                failed = False
                try:
                    pipeline = self._pipeline()
                    if kind == "refresh":
                        failed = await pipeline.refresh_metadata(item_id) is None
                    else:
                        self.db.update_item(item_id, status="processing", error=None)
                        # purpose "bulk": a new identification like any other, so Claude's step can go through a batch (half price)
                        result = await pipeline.process(item_id, None, "bulk")
                        failed = result is None or result.get("status") == "error"
                except Exception:
                    log.exception("Bulk %s failed for %s", kind, item_id)
                    failed = True
                self.state["done"] += 1
                self.state["failed"] += int(failed)

        try:
            await asyncio.gather(*(one(i) for i in ids))
        finally:
            self.state.update(running=False, cancelled=self._cancel and self.state["done"] < self.state["total"], finished=time.time())
