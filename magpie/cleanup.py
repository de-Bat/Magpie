"""Photo cleanup: is this photo already saved in Magpie?

Clients that can see the photo library (the iOS app, an iOS Shortcut, a browser with a folder or
the photo picker) send each photo here, usually shrunk. The answer is "yes" when it is the
screenshot of a finished capture: byte-identical (SHA256 of the original, which the client sends
along with the shrunk copy), or the same picture by look (see Fingerprint). The client deletes it.

Only finished captures count: identified, not flagged for review, not waiting for analysis.
Anything else might still turn out wrong, and then the photo is the only copy worth having.
"""

import hashlib
import io
import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path

from .db import REVIEW_THRESHOLD, Database

# Two checks, cheap then strict:
#  1. a 256-bit difference hash finds candidates with the same layout (and the same shape);
#  2. a 54x96 grayscale thumbnail confirms it: no pixel may differ by more than a little. Re-encoding and
#     shrinking move pixels by a few levels; a different screenshot, even one changed line of text,
#     moves some by 40+. A wrong "yes" deletes a photo, so this errs towards "no".
THUMB = (54, 96)
HASH_DISTANCE = 40      # bits (of 256): wide on purpose, the thumbnail decides
MAX_PIXEL_DIFF = 24     # of 255, anywhere in the thumbnail
MAX_MEAN_DIFF = 4.0
SHAPE_TOLERANCE = 0.02  # width/height may differ by 2% (rounding when a client shrinks the photo)


@dataclass(frozen=True)
class Fingerprint:
    bits: int
    aspect: float           # width / height
    thumb: bytes            # THUMB-sized 8-bit grayscale

    def distance(self, other: "Fingerprint") -> int:
        return (self.bits ^ other.bits).bit_count()

    def same_shape(self, other: "Fingerprint") -> bool:
        return abs(self.aspect - other.aspect) / max(self.aspect, other.aspect) <= SHAPE_TOLERANCE

    def same_picture(self, other: "Fingerprint") -> bool:
        from PIL import Image, ImageChops, ImageStat
        diff = ImageChops.difference(Image.frombytes("L", THUMB, self.thumb), Image.frombytes("L", THUMB, other.thumb))
        return diff.getextrema()[1] <= MAX_PIXEL_DIFF and ImageStat.Stat(diff).mean[0] <= MAX_MEAN_DIFF


def fingerprint(data: bytes) -> Fingerprint | None:
    """Fingerprint of an image, or None if it can't be read (or Pillow isn't installed)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            width, height = im.size
            if not width or not height:
                return None
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, "white")   # transparency counts as white
            flat.paste(im, mask=im.getchannel("A"))
    except Exception:  # not a picture Pillow can read (e.g. HEIC without a plugin)
        return None
    # Two steps, so a full-size original and a shrunk copy land on (nearly) the same pixels.
    stage = flat.resize((THUMB[0] * 4, THUMB[1] * 4), Image.Resampling.LANCZOS).convert("L")
    thumb = stage.resize(THUMB, Image.Resampling.LANCZOS)
    grid = thumb.resize((17, 16), Image.Resampling.LANCZOS).load()
    bits = 0
    for y in range(16):
        for x in range(16):
            bits = (bits << 1) | (grid[x, y] > grid[x + 1, y])
    return Fingerprint(bits, width / height, thumb.tobytes())


@dataclass(frozen=True)
class Capture:
    item_id: str
    title: str
    image_file: str
    image_hash: str | None
    original_hash: str | None = None   # the photo on the device, when the client converted it before upload


class CleanupIndex:
    """Fingerprints of finished captures' screenshots, stored so each image is decoded once."""

    def __init__(self, db: Database, uploads_dir: Path):
        self.db = db   # the Database, or main's proxy for it
        self.uploads_dir = Path(uploads_dir)
        self._known: dict[str, Fingerprint] | None = None   # image_file -> fingerprint, loaded on first use
        self._lock = threading.Lock()

    def captures(self) -> list[Capture]:
        """The screenshots of every finished capture, including further screenshots merged into it."""
        rows = self.db.conn.execute(
            "SELECT id, title, image_file, image_hash, original_hash, captures FROM items "
            "WHERE kind = 'screenshot' AND status = 'ready' AND image_file != '' "
            "AND (corrected = 1 OR confidence IS NULL OR confidence >= ?) "
            "AND id NOT IN (SELECT item_id FROM batch_jobs)", (REVIEW_THRESHOLD,)).fetchall()
        result = []
        for row in rows:
            title = row["title"] or "Untitled"
            result.append(Capture(row["id"], title, row["image_file"], row["image_hash"], row["original_hash"]))
            for extra in _json_list(row["captures"]):
                if extra.get("image_file"):
                    result.append(Capture(row["id"], title, extra["image_file"], extra.get("image_hash"), extra.get("original_hash")))
        return result

    def fingerprints(self, captures: list[Capture]) -> dict[str, Fingerprint]:
        """Fingerprints of these captures' screenshots; ones not seen before are computed and stored."""
        conn = self.db.conn
        with self._lock:
            if self._known is None:
                self._known = {r["image_file"]: Fingerprint(int(r["bits"], 16), r["aspect"], bytes(r["thumb"]))
                               for r in conn.execute("SELECT image_file, bits, aspect, thumb FROM fingerprints")}
            known = self._known
            for name in dict.fromkeys(c.image_file for c in captures if c.image_file not in known):
                try:
                    fp = fingerprint((self.uploads_dir / name).read_bytes())
                except OSError:
                    fp = None
                if fp is None:
                    continue
                known[name] = fp
                with conn:
                    conn.execute("INSERT OR REPLACE INTO fingerprints (image_file, bits, aspect, thumb) VALUES (?, ?, ?, ?)",
                                 (name, f"{fp.bits:064x}", fp.aspect, fp.thumb))
            return dict(known)

    def shapes(self) -> dict:
        """Width/height of every finished capture, so clients can skip photos that can't match without sending them."""
        captures = self.captures()
        known = self.fingerprints(captures)
        shapes = sorted({round(known[c.image_file].aspect, 4) for c in captures if c.image_file in known})
        return {"shapes": shapes, "tolerance": SHAPE_TOLERANCE, "captures": len({c.item_id for c in captures}),
                "keys": shape_keys(shapes)}

    def check(self, data: bytes, sha256: str | None = None) -> dict:
        """Is this photo a finished capture? `sha256` is the hash of the original when `data` is a shrunk copy."""
        captures = self.captures()
        hashes = {h for h in (sha256 and sha256.strip().lower(), hashlib.sha256(data).hexdigest()) if h}
        for c in captures:
            if hashes & {c.image_hash, c.original_hash}:
                return {"match": True, "how": "exact", "distance": 0, "item_id": c.item_id, "title": c.title}

        photo = fingerprint(data)
        if photo is None:
            return {"match": False, "reason": "unreadable"}
        known = self.fingerprints(captures)
        candidates = []
        for c in captures:
            fp = known.get(c.image_file)
            if fp is not None and fp.same_shape(photo):
                d = fp.distance(photo)
                if d <= HASH_DISTANCE:
                    candidates.append((d, c, fp))
        for d, c, fp in sorted(candidates, key=lambda x: x[0]):
            if fp.same_picture(photo):
                return {"match": True, "how": "similar", "distance": d, "item_id": c.item_id, "title": c.title}
        return {"match": False}

    def forget(self, image_files: list[str]) -> None:
        """Drop fingerprints of screenshots that were deleted."""
        if not image_files:
            return
        with self._lock:
            for f in image_files:
                (self._known or {}).pop(f, None)
            with self.db.conn as conn:
                conn.executemany("DELETE FROM fingerprints WHERE image_file = ?", [(f,) for f in image_files])


def shape_keys(shapes: list[float]) -> dict[str, bool]:
    """The shapes as a lookup table for an iOS Shortcut, which can't easily loop over a list with a tolerance:
    key = round(1000 * width / height), every key within the tolerance of a shape is present. Numbers of 1000 and
    up are also given with the thousands separators a phone's locale may add when Shortcuts turns them into text."""
    keys: dict[str, bool] = {}
    for aspect in shapes:
        lo, hi = math.floor(1000 * aspect * (1 - SHAPE_TOLERANCE)), math.ceil(1000 * aspect * (1 + SHAPE_TOLERANCE))
        for k in range(max(lo, 1), hi + 1):
            keys[str(k)] = True
            if k >= 1000:
                grouped = f"{k:,}"
                for sep in (",", ".", " ", " ", "'"):
                    keys[grouped.replace(",", sep)] = True
    return keys


def _json_list(value) -> list[dict]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [x for x in parsed if isinstance(x, dict)]
