"""List the models a provider offers, for the settings screen's model picker."""

import httpx

from .config import HOSTED_LLMS, Settings, _origin

# Ids that are not chat/vision models, hidden from the picker.
_SKIP = ("embed", "whisper", "tts", "dall-e", "moderation", "transcribe", "rerank", "image-gen")


async def fetch_models(settings: Settings, http: httpx.AsyncClient, provider: str,
                       url: str | None = None, key: str | None = None) -> list[dict]:
    """Return [{"id": ..., "label": ...}] sorted by id. `key` is one typed but not saved yet; otherwise the
    saved key is used, and only when it would be sent to the server it belongs to."""
    key = (key or "").strip() or None
    if provider == "claude":
        key = key or settings.anthropic_api_key
        if not key:
            raise ValueError("Enter your Anthropic API key first.")
        r = await http.get("https://api.anthropic.com/v1/models", params={"limit": 100},
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        items = [(m["id"], m.get("display_name") or m["id"]) for m in _json(r).get("data", [])]
    else:
        if provider in HOSTED_LLMS:
            base, _, key_attr, label = HOSTED_LLMS[provider]
            key = key or getattr(settings, key_attr)
            if not key:
                raise ValueError(f"Enter your {label} API key first.")
        elif provider == "local":
            base = (url or settings.local_llm_url or "").strip()
            if not base:
                raise ValueError("Enter the server endpoint first.")
            if not key and _origin(base) == _origin(settings.local_llm_url):
                key = settings.local_llm_api_key
        else:
            raise ValueError(f"Unknown provider {provider!r}")
        r = await http.get(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"} if key else {})
        items = []
        for m in _json(r).get("data", []):
            mid = str(m.get("id") or m.get("name") or "").removeprefix("models/")
            if mid and not any(s in mid.lower() for s in _SKIP):
                items.append((mid, mid))
    return [{"id": i, "label": l} for i, l in sorted(set(items))]


def _json(r: httpx.Response) -> dict:
    if r.status_code in (401, 403):
        raise ValueError("The provider rejected the API key.")
    if r.status_code >= 400:
        raise ValueError(f"The provider answered {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError:
        raise ValueError("The provider's reply wasn't a model list.") from None
    if isinstance(data, list):  # some servers return a bare list
        data = {"data": data}
    return data if isinstance(data, dict) else {}
