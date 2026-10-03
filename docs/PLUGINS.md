# Plugins

A plugin adds an action to a saved item, such as the **Add to Radarr** button on a film. Magpie ships with
Radarr and Sonarr; you can add more from a git repository (one repository can hold many plugins) or from a
single `.py` file.

## Installing

Installing runs the plugin's code **on your server, with its full access**, so it is off by default and can
only be turned on in the server's environment (not from the app, where anyone holding an access token could
otherwise get code execution on the host):

```
MAGPIE_ALLOW_PLUGIN_INSTALL=true      # in .env, then restart
```

Then open **Settings → Plugins**:

- **Git repository**: a public `https://` URL, optionally a branch or tag. Every plugin module in it is
  installed. **Update** pulls the latest commit.
- **Single file**: a link to a `.py` file (a GitHub `blob` page link works) or an upload.
- **Remove** deletes a source, its plugins and the settings saved for them.

Plugins live in `<data dir>/plugins/` (`sources.json` plus one folder per source), so they survive restarts
and Docker rebuilds. You can also copy a `.py` file into `files/<name>/` by hand; installed sources are
loaded at startup.

Installs are all-or-nothing: if any module in a repository fails to import, nothing from it is kept and
the error is shown. A source that breaks later (say, after an update) is listed with its error and the
server still starts.

## Repository layout

A source's plugin modules are the `*.py` files in its `plugins/` folder, or else at its root. Files starting
with `_` (helpers) and `test_*.py` are not loaded as plugins, but plugins can import them:

```
my-magpie-plugins/
  plugins/
    _common.py        # helpers, imported by the plugins below
    jellyseerr.py     # calls register(...) for one or more plugins
    trakt.py
```

## Writing a plugin

A module calls `register()` with a `Plugin` when it is imported:

```python
from magpie.config import Spec
from magpie.plugins import Plugin, PluginError, register


class Jellyseerr(Plugin):
    id = "jellyseerr"                  # 2-32 chars: a-z, 0-9, _
    label = "Jellyseerr"
    action_label = "Request in Jellyseerr"
    categories = ("movie", "tv_show")  # which items get the button

    # Settings: they appear in Settings under their own tab (the group). Secrets are masked.
    specs = (
        Spec("jellyseerr_url", "JELLYSEERR_URL", "str", None, "Jellyseerr", "Server URL"),
        Spec("jellyseerr_api_key", "JELLYSEERR_API_KEY", "secret", None, "Jellyseerr", "API key"),
    )
    url_key = ("JELLYSEERR_URL", "JELLYSEERR_API_KEY")   # drop the saved key if the URL moves to another server

    def configured(self, settings):
        return bool(settings.value("jellyseerr_url") and settings.value("jellyseerr_api_key"))

    async def status(self, item, settings, http):
        """What the button shows. {"state": "available" | "added", "message": ..., "url": ...}"""
        return {"state": "available"}

    async def run(self, item, settings, http):
        """The button was pressed. `http` is a shared httpx.AsyncClient."""
        r = await http.post(f"{settings.value('jellyseerr_url')}/api/v1/request", json={...},
                            headers={"X-Api-Key": settings.value("jellyseerr_api_key")})
        if r.status_code >= 400:
            raise PluginError(f"Jellyseerr answered {r.status_code}", 502)   # shown to the user
        return {"state": "added", "message": f"Requested {item['title']}"}


register(Jellyseerr())
```

- `item` is the saved item as in the API: `title`, `category`, `canonical_url`, `metadata` (`year`,
  `tmdb_id`, `imdb_id`, ...), `tags`, ...
- Setting kinds are `str`, `secret`, `int`, `float`, `bool` and `choice`. Choice values are lowercase.
- Read settings with `settings.value("attr")`. Don't log them: they include secrets.
- `Spec` environment names must be unique; a name that is already taken is ignored.
- To build on Radarr/Sonarr's shared code (API v3 calls, lookup matching, root folder and quality
  profile choice), subclass `magpie.plugins.arr.ArrPlugin`.
- Define a plugin class once per id: registering an id that already exists is an error.
