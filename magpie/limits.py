"""What each AI provider and model has left, and when a model that hit its limit is available again.

Providers report limits differently, so each gets what it needs:
  - Anthropic: anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset},
    per model class; the reset is a timestamp. A 429 carries retry-after.
  - OpenAI, Groq, OpenRouter: x-ratelimit-{limit,remaining,reset}-{requests,tokens}, per model; the reset is a
    duration ("6m0s", "20ms"). Groq's request limit is per day, its token limit per minute. OpenAI answers
    429 "insufficient_quota" when billing runs out, which doesn't reset by itself.
  - Gemini: no headers at all. A 429 RESOURCE_EXHAUSTED names the quota (per minute or per day) and a
    retryDelay; daily quotas reset at midnight Pacific. Its published free-tier limits (below) and the
    requests Magpie has made give an estimate of what's left.
  - OpenRouter also reports the key's credit (models.check_key).
A model that hit its limit is paused until it's available again: requests wait if that's a few seconds away,
otherwise fail fast with the time, and the item is retried automatically then (see main.RetryWorker).
"""

import email.utils
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

LIMITS: dict[str, dict] = {}   # "provider" (key-wide: credit) or "provider:model" -> latest snapshot

# Published free-tier limits for providers that don't report them: (requests per minute, requests per day).
# Paid tiers are much higher; set yours with MAGPIE_RATE_LIMITS='{"gemini-2.5-flash": [1000, 10000]}'.
KNOWN_LIMITS: dict[str, dict[str, tuple[int, int]]] = {
    "gemini": {"gemini-2.5-pro": (5, 100), "gemini-2.5-flash-lite": (15, 1000), "gemini-2.5-flash": (10, 250),
               "gemini-2.0-flash-lite": (30, 200), "gemini-2.0-flash": (15, 200)},
}
DAILY_RESET_TZ = {"gemini": "America/Los_Angeles"}
WAIT_AT_MOST = 20          # seconds: a limit that frees up sooner than this is waited out instead of failing
CREDIT_RECHECK = 15 * 60   # out of credit doesn't reset by itself: try again after this long
LOW_SHARE = 0.1            # "running low" below this share of the limit

# (provider, model, since ISO timestamp) -> requests Magpie made; set by the app (Database.requests_since)
count_requests: Callable[[str, str, str], int] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def duration_seconds(value) -> float | None:
    """"6m0s", "1h2m", "20ms", "2.5s", "37s", "1d" or a plain number of seconds."""
    if value is None:
        return None
    text = str(value).strip().lower()
    try:
        return float(text)
    except ValueError:
        pass
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|d|h|m|s)", text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        return None
    unit = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(float(n) * unit[u] for n, u in parts)


def reset_at(value, now: datetime | None = None) -> str | None:
    """A reset given as a timestamp (Anthropic) or as time from now (OpenAI-style), as an ISO timestamp."""
    if value in (None, ""):
        return None
    now = now or _now()
    stamp = _parse_iso(str(value))
    if stamp:
        return _iso(stamp)
    secs = duration_seconds(value)
    return _iso(now + timedelta(seconds=secs)) if secs is not None else None


def _day_start(provider: str, now: datetime) -> tuple[datetime, datetime]:
    """Start of the provider's quota day and when the next one begins (UTC)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DAILY_RESET_TZ.get(provider, "UTC"))
    except Exception:  # no tz database: Pacific standard time is close enough
        tz = timezone(timedelta(hours=-8)) if provider in DAILY_RESET_TZ else timezone.utc
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


KINDS = (("requests", "requests"), ("tokens", "tokens"), ("input_tokens", "input-tokens"), ("output_tokens", "output-tokens"))


def parse_limits(headers) -> dict | None:
    """Normalize rate-limit headers into {"requests": {remaining, limit, reset, reset_at}, "tokens": {...}, ...}."""
    h = {str(k).lower(): v for k, v in dict(headers or {}).items()}
    now, out = _now(), {}
    for key, name in KINDS:
        for remaining, limit, reset in ((f"anthropic-ratelimit-{name}-remaining", f"anthropic-ratelimit-{name}-limit", f"anthropic-ratelimit-{name}-reset"),
                                        (f"x-ratelimit-remaining-{name}", f"x-ratelimit-limit-{name}", f"x-ratelimit-reset-{name}")):
            if remaining in h:
                try:
                    out[key] = {"remaining": int(float(h[remaining])),
                                "limit": int(float(h[limit])) if limit in h else None,
                                "reset": h.get(reset), "reset_at": reset_at(h.get(reset), now)}
                except ValueError:
                    pass
                break
    return out or None


def _key(provider: str, model: str | None) -> str:
    return f"{provider}:{model}" if model else provider


def record_limits(provider: str, headers=None, model: str | None = None, status: int | None = None, **extra) -> dict | None:
    """Remember what a provider just reported about what's left (headers and/or fields such as credit).
    A successful answer ends any pause on that model."""
    key = _key(provider, model)
    fresh = (parse_limits(headers) if headers is not None else None) or {}
    fresh.update({k: v for k, v in extra.items() if v is not None})
    entry = LIMITS.get(key)
    if status is not None and status < 400 and entry and "blocked_until" in entry:
        entry.pop("blocked_until", None)
        entry.pop("blocked_reason", None)
    if not fresh:
        return entry
    entry = {**(entry or {}), **fresh, "provider": provider, "model": model, "updated": _iso(_now())}
    LIMITS[key] = entry
    return entry


def retry_delay(headers=None, body: str = "") -> float | None:
    """Seconds until the provider will take requests again, if it said so."""
    h = {str(k).lower(): v for k, v in dict(headers or {}).items()}
    value = h.get("retry-after")
    if value:
        secs = duration_seconds(value)
        if secs is not None:
            return secs
        try:
            return max(0.0, (email.utils.parsedate_to_datetime(value) - _now()).total_seconds())
        except (TypeError, ValueError):
            pass
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', body or "")   # Gemini's RetryInfo
    return float(m.group(1)) if m else None


def mark_limited(provider: str, model: str | None, headers=None, body: str = "", status: int = 429, default_delay: float = 60) -> dict:
    """The provider refused because a limit is reached: pause this model until it's available again."""
    now = _now()
    text = (body or "").lower()
    entry = record_limits(provider, headers, model) or {"provider": provider, "model": model}
    if status == 402 or any(s in text for s in ("insufficient_quota", "insufficient credit", "credit balance", "billing", "more credits")):
        reason, until = "credit", now + timedelta(seconds=CREDIT_RECHECK)   # add credit, then it works again
        entry = LIMITS.setdefault(provider, {"provider": provider, "model": None})   # the whole key, not one model
    else:
        daily = bool(re.search(r"per ?day|perday|daily|requests_per_day|rpd", text))
        exhausted = [v for k, v in entry.items() if isinstance(v, dict) and v.get("remaining") == 0 and v.get("reset_at")]
        delay = retry_delay(headers, body)
        if delay is not None:
            until = now + timedelta(seconds=delay)
        elif exhausted:
            until = max(_parse_iso(v["reset_at"]) for v in exhausted)
        elif daily:
            until = _day_start(provider, now)[1]
        else:
            until = now + timedelta(seconds=default_delay)   # the provider didn't say: the caller's backoff
        reason = "daily quota" if daily or (until - now) > timedelta(hours=1) else "rate limit"
    entry.update(blocked_until=_iso(until), blocked_reason=reason, updated=_iso(now))
    LIMITS[_key(provider, entry.get("model"))] = entry
    return entry


def _known(provider: str, model: str | None) -> tuple[int, int] | None:
    try:
        override = json.loads(os.environ.get("MAGPIE_RATE_LIMITS") or "{}")
    except ValueError:
        override = {}
    if model and model in override:
        return tuple(override[model])[:2]  # type: ignore[return-value]
    table = KNOWN_LIMITS.get(provider, {})
    for known in sorted(table, key=len, reverse=True):   # "gemini-2.5-flash-preview-09" -> gemini-2.5-flash
        if model and model.removeprefix("models/").startswith(known):
            return table[known]
    return None


def estimate(provider: str, model: str | None) -> dict | None:
    """What's left by the published limits and the requests Magpie made, for providers that don't say."""
    known = _known(provider, model)
    if not known or not count_requests or not model:
        return None
    rpm, rpd = known
    now = _now()
    day_start, next_day = _day_start(provider, now)
    try:
        today = count_requests(provider, model, _iso(day_start))
        minute = count_requests(provider, model, _iso(now - timedelta(seconds=60)))
    except Exception:
        return None
    return {"requests": {"remaining": max(0, rpd - today), "limit": rpd, "reset_at": _iso(next_day), "per": "day"},
            "requests_minute": {"remaining": max(0, rpm - minute), "limit": rpm, "reset_at": _iso(now + timedelta(seconds=60)), "per": "minute"},
            "estimated": True}


def blocked(provider: str, model: str | None) -> tuple[datetime, str] | None:
    """(until, reason) while this model can't be used, else None."""
    now = _now()
    for key in (_key(provider, model), provider):
        entry = LIMITS.get(key) or {}
        until = _parse_iso(entry.get("blocked_until"))
        if until and until > now:
            return until, entry.get("blocked_reason") or "rate limit"
    return None   # an estimate (published free-tier limits) never blocks: only the provider saying no does


def wait_seconds(until: datetime) -> float:
    return max(0.0, (until - _now()).total_seconds())


def report(current: list[tuple[str, str | None]] = ()) -> list[dict]:
    """Every provider/model seen, the ones in use first: what's left, when it resets, and whether it's paused."""
    now = _now()
    rows: dict[str, dict] = {}
    for key, entry in LIMITS.items():
        rows[key] = {**entry}
    for provider, model in current:
        rows.setdefault(_key(provider, model), {"provider": provider, "model": model})
    out = []
    for key, row in rows.items():
        provider, model = row.get("provider") or key.split(":")[0], row.get("model")
        if not any(isinstance(row.get(k), dict) for k, _ in KINDS):
            row.update(estimate(provider, model) or {})
        until = _parse_iso(row.get("blocked_until"))
        if not until or until <= now:
            row.pop("blocked_until", None)
            row.pop("blocked_reason", None)
            hit = blocked(provider, model)   # a paused provider key (credit)
            if hit:
                row["blocked_until"], row["blocked_reason"] = _iso(hit[0]), hit[1]
        shares = [v["remaining"] / v["limit"] for k, v in row.items()
                  if isinstance(v, dict) and v.get("limit") and v.get("remaining") is not None]
        row["low"] = bool(shares) and min(shares) < LOW_SHARE
        row["current"] = (provider, model) in current or (provider, None) in current or (
            model is None and any(p == provider for p, _ in current))
        row["key"] = key
        out.append(row)
    return sorted(out, key=lambda r: (not r["current"], r["key"]))
