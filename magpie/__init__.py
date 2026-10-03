"""Magpie: save screenshots of things you want to remember and find them later."""
from pathlib import Path

try:  # written by tag.sh when a release is tagged, so the running app knows which version it is
    __version__ = (Path(__file__).parent / "VERSION").read_text().strip() or "dev"
except OSError:
    __version__ = "dev"
