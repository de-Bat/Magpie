"""Plugins: optional integrations that act on a saved item, e.g. send a film to Radarr.

A plugin subclasses `Plugin`, declares the settings it needs (they appear in the settings screen under
their own tab) and the item categories it applies to, and is added with `register()`. The app shows one
button per applicable, configured plugin on an item's card.
"""
from __future__ import annotations

import re
from contextlib import contextmanager


class PluginError(Exception):
    """Something the user can act on (unreachable server, wrong key, no match). `status` is the HTTP code reported."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class Plugin:
    id: str = ""
    label: str = ""
    action_label: str = ""
    categories: tuple[str, ...] = ()
    specs: tuple[Spec, ...] = ()
    url_key: tuple[str, str] | None = None  # (url env, key env), see config.register_specs
    source: str = "built-in"                 # set by register(): "built-in" or the id of the repo/file it came from

    def configured(self, settings) -> bool:
        return False

    def applies_to(self, item: dict) -> bool:
        return item.get("category") in self.categories

    def problems(self, settings) -> list[tuple[str, str, str | None]]:
        """(level, message, setting env) for the settings screen's status banner."""
        return []

    async def status(self, item: dict, settings, http) -> dict:
        """{"state": "available" | "added", "message": str | None, "url": str | None}. Raises PluginError."""
        return {"state": "available"}

    async def run(self, item: dict, settings, http) -> dict:
        """Do the plugin's thing for `item`. Same shape as `status`, plus a message to show. Raises PluginError."""
        raise NotImplementedError

    async def test(self, settings, http, url: str | None = None, key: str | None = None) -> dict:
        """Check the connection (optionally with values not saved yet). Raises PluginError."""
        raise PluginError("This plugin has no connection test.", 400)

    def describe(self, settings) -> dict:
        return {"id": self.id, "label": self.label, "action_label": self.action_label,
                "categories": list(self.categories), "configured": self.configured(settings), "source": self.source}


_REGISTRY: dict[str, Plugin] = {}


_SOURCE = "built-in"


@contextmanager
def loading_from(source: str):
    """Plugins registered inside this block are attributed to `source` (the loader uses it for external code)."""
    global _SOURCE
    previous, _SOURCE = _SOURCE, source
    try:
        yield
    finally:
        _SOURCE = previous


def register(plugin: Plugin) -> Plugin:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", plugin.id or ""):
        raise ValueError(f"Plugin id {plugin.id!r} must be 2-32 characters: lowercase letters, digits, underscores")
    if plugin.id in _REGISTRY:
        raise ValueError(f"A plugin with the id {plugin.id!r} already exists ({_REGISTRY[plugin.id].source})")
    plugin.source = _SOURCE
    _REGISTRY[plugin.id] = plugin
    register_specs(plugin.specs, plugin.url_key)
    return plugin


def unregister(plugin_id: str) -> Plugin | None:
    plugin = _REGISTRY.pop(plugin_id, None)
    if plugin is not None:
        unregister_specs(plugin.specs, plugin.url_key)
    return plugin


def sync_settings() -> None:
    """(Re-)add every registered plugin's settings; config calls this each time it is imported or reloaded."""
    for plugin in _REGISTRY.values():
        register_specs(plugin.specs, plugin.url_key)


def get(plugin_id: str) -> Plugin | None:
    return _REGISTRY.get(plugin_id)


def all() -> list[Plugin]:
    return list(_REGISTRY.values())


from ..config import Spec, register_specs, unregister_specs  # noqa: E402  (after the definitions above: config imports this package)
from . import radarr, sonarr  # noqa: E402,F401  (each registers itself)
