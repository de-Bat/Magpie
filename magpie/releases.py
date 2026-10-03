"""Release notes for the About panel: the main features of each release, newest first (releases.json).

An entry whose version is "next" collects what is coming in the next release; tag.sh renames it to the new version.
"""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).with_name("releases.json")


def load(path: Path = PATH) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict) or not r.get("version"):
            continue
        features = [f for f in r.get("features") or [] if isinstance(f, dict) and f.get("title")]
        out.append({"version": str(r["version"]), "date": r.get("date"), "features": features})
    return out
