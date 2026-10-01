"""Analyzer backends and the router that picks between them.

  claude  - Claude with web search (most accurate; needs ANTHROPIC_API_KEY)
  local   - an on-prem LLM behind an OpenAI-compatible API (Ollama, vLLM, LM Studio, llama.cpp)
  hybrid  - local first; escalate to Claude only when the local model isn't confident
  ocr     - no LLM at all: OCR + rules (fast, private, rough)

All of them get the OCR pre-pass (see ocr.py) and return the same dict shape as
analyzer.SAVE_TOOL, so the rest of the pipeline doesn't care which one ran.
"""

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any

import httpx

from .analyzer import CATEGORIES, PLATFORMS, SAVE_TOOL, SYSTEM_PROMPT, AnalysisError, RateLimited, ScreenshotAnalyzer, correction_prompt, prepare_image
from .config import HOSTED_LLMS, Settings
from .related import clean_related
from .ocr import Ocr, OcrResult, Signals, extract_signals
from . import limits
from .usage import Run, local_cost, record_limits, token_cost

log = logging.getLogger(__name__)

SCHEMA = SAVE_TOOL["input_schema"]
DETAIL_KEYS = list(SCHEMA["properties"]["details"]["properties"])
ARRAY_DETAILS = {k for k, v in SCHEMA["properties"]["details"]["properties"].items() if v.get("type") == "array"}

# Models without web access (local servers, hosted OpenAI-compatible providers) get the same instructions,
# except where Claude searches the web: Magpie looks things up after they answer (GitHub, TMDB, Open Library, the
# article by its headline), so they should name the thing exactly as it would be searched for.
NO_WEB_STEP = (
    "2. You have no web access; Magpie looks things up after you answer. So name the thing exactly as it would be "
    "searched for: for code, the repository's name as the title, and owner/repo in details.github_full_name when it is "
    "visible or you are certain; for articles, the headline exactly as shown and the site or publisher in "
    "details.publisher; for films, TV and books, the exact title and year. Give a canonical URL only if it is written "
    "in the screenshot; never guess an address."
)
LOCAL_SYSTEM_PROMPT = re.sub(r"(?m)^2\. Use web search.*$", lambda _: NO_WEB_STEP, SYSTEM_PROMPT).replace(
    "Finish by calling save_analysis once. Do not ask the user questions.",
    "Reply with a single JSON object matching the requested schema. Do not ask the user questions.",
).replace(
    "Only report URLs, ratings and facts you actually saw in search results or the screenshot.",
    "Only report URLs, ratings and facts you saw in the screenshot or are certain of; ratings are looked up later.",
).replace(
    "Each link must be a page you saw, and must not be the item's own page.",
    "Only include links written in the screenshot or the OCR text (never guessed addresses), and not the item's own page; "
    "otherwise leave `related` empty.",
)


def blank_analysis() -> dict:
    return {
        "category": "other", "source_platform": "other", "title": "", "subtitle": None, "year": None,
        "summary": "", "canonical_url": None, "image_url": None, "links": [], "related": [], "tags": [],
        "screenshot_text": "", "confidence": 0, "confidence_reason": "", "alternatives": [],
        "details": {k: ([] if k in ARRAY_DETAILS else None) for k in DETAIL_KEYS},
    }


def normalize(raw: dict) -> dict:
    """Coerce a model's (possibly partial or sloppy) output into the analysis shape."""
    out = blank_analysis()
    for k in out:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    out["details"] = {k: details.get(k, [] if k in ARRAY_DETAILS else None) for k in DETAIL_KEYS}
    for k in ARRAY_DETAILS:
        v = out["details"][k]
        out["details"][k] = [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])

    cat = str(out["category"]).lower().replace(" ", "_").replace("-", "_")
    out["category"] = {"tv": "tv_show", "tv_series": "tv_show", "series": "tv_show", "film": "movie",
                       "repo": "github_repo", "github": "github_repo"}.get(cat, cat)
    if out["category"] not in CATEGORIES:
        out["category"] = "other"
    plat = str(out["source_platform"]).lower()
    out["source_platform"] = {"x": "twitter", "fb": "facebook", "ig": "instagram"}.get(plat, plat)
    if out["source_platform"] not in PLATFORMS:
        out["source_platform"] = "other"
    try:
        c = out["confidence"]
        c = {"high": 90, "medium": 70, "low": 40}.get(c, c) if isinstance(c, str) else c
        c = float(c)
        out["confidence"] = int(round(c * 100 if 0 < c <= 1 else c))  # some models answer 0.8
        out["confidence"] = max(0, min(100, out["confidence"]))
    except (TypeError, ValueError):
        out["confidence"] = 30
    try:
        out["year"] = int(out["year"]) if out["year"] not in (None, "") else None
    except (TypeError, ValueError):
        out["year"] = None
    out["tags"] = [str(t) for t in out["tags"]] if isinstance(out["tags"], list) else []
    out["links"] = [l for l in out["links"] if isinstance(l, dict) and l.get("url")] if isinstance(out["links"], list) else []
    out["related"] = clean_related(out["related"] if isinstance(out["related"], list) else [])
    out["alternatives"] = [
        {"title": str(a.get("title")), "category": a.get("category") if a.get("category") in CATEGORIES else "other",
         "year": a.get("year") if isinstance(a.get("year"), int) else None,
         "canonical_url": a.get("canonical_url"), "why": str(a.get("why") or "")}
        for a in (out["alternatives"] if isinstance(out["alternatives"], list) else [])
        if isinstance(a, dict) and a.get("title")
    ][:3]
    for k in ("title", "summary", "screenshot_text", "confidence_reason"):
        out[k] = str(out[k] or "")
    return out


def parse_json(text: str) -> dict:
    """Pull a JSON object out of a reply that may wrap it in prose or ``` fences."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AnalysisError("The local model did not return JSON.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AnalysisError(f"The local model returned invalid JSON: {e}") from e


def hints_prompt(ocr: OcrResult | None, signals: Signals | None) -> str:
    if not ocr or not ocr.lines:
        return ""
    text = ocr.text
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    out = f"\n\nText found in the screenshot by OCR (may contain recognition errors, e.g. missing spaces):\n<ocr>\n{text}\n</ocr>"
    if signals and signals.as_hint_text():
        out += f"\nAutomatic clues (may be wrong): {signals.as_hint_text()}"
    return out


class LocalLLMAnalyzer:
    """A model behind an OpenAI-compatible /chat/completions API: Ollama, vLLM, LM Studio,
    llama.cpp, hosted OpenAI-compatible providers, or NVIDIA NIM (self-hosted or build.nvidia.com)."""

    RETRY_STATUSES = (429, 502, 503, 504)
    MAX_RETRIES = 3

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None, source: str = "auto"):
        """source: "local" = the LOCAL_LLM_* server, "hosted" = the chosen hosted provider, "auto" = whichever
        is configured (the hosted provider wins)."""
        hosted = source == "hosted" or (source == "auto" and settings.hosted_llm in HOSTED_LLMS)
        self.hosted = hosted
        if hosted:
            if settings.hosted_llm not in HOSTED_LLMS:
                raise ValueError("No hosted provider is selected")
            url, key = HOSTED_LLMS[settings.hosted_llm][0], settings.llm_api_key
            model = settings.llm_model
            self.name, self.provider = settings.hosted_llm, "openai"
        else:
            url, key, model = settings.local_llm_url, settings.local_llm_api_key, settings.local_llm_model
            self.name, self.provider = "local", settings.resolved_llm_provider(local=True)
        if not url:
            raise ValueError("LOCAL_LLM_URL is not set")
        self.max_image_edge = settings.max_image_edge
        # NIM: JSON schema via response_format on newer releases, else its nvext.guided_json extension.
        self.formats = ("json_schema", "nvext", "json_object", "none") if self.provider == "nim" else self.FORMATS
        # NIM vision models accept JPEG/PNG only.
        self.image_types = ("image/png", "image/jpeg") if self.provider == "nim" else ("image/png", "image/jpeg", "image/webp", "image/gif")
        self._sleep = asyncio.sleep
        self.url = url.rstrip("/") + "/chat/completions"
        self.model = model
        self.vision = settings.local_llm_vision
        self.max_output_tokens = settings.max_output_tokens
        self.timeout = settings.local_llm_timeout
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.cost_per_hour = settings.local_cost_per_hour
        self.http = http or httpx.AsyncClient()
        self._format_mode = "json_schema"  # downgraded automatically if the server doesn't support it

    @property
    def label(self) -> str:
        return f"{'nim' if self.provider == 'nim' else self.name}:{self.model}"

    async def analyze(self, image: bytes | None, media_type: str | None, note: str | None = None,
                      correction: dict | None = None, hints: str = "", link_url: str | None = None, web: bool = True) -> dict:
        prompt = "Identify what this screenshot is recommending and catalogue it."
        if image is None:
            prompt = (f"The user shared a link, not a screenshot: {link_url}\n"
                      "Identify what it is (or what it recommends) and catalogue it, using the page content below.")
        elif not self.vision:
            prompt = "Identify what this screenshot is recommending, using only the OCR text below, and catalogue it."
        if note:
            prompt += f"\n\nThe user added this note when saving it: {note}"
        if correction:
            prompt += "\n\n" + correction_prompt(correction)
        prompt += hints
        prompt += ("\n\nAnswer with JSON only, with these keys: " + ", ".join(SCHEMA["properties"]) +
                   ". confidence is an integer 0-100. details has these keys: " + ", ".join(DETAIL_KEYS) + ".")

        content: list[dict] = [{"type": "text", "text": prompt}]
        if self.vision and image is not None:
            data, mt = prepare_image(image, media_type, self.max_image_edge, self.image_types)
            content.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{base64.b64encode(data).decode()}"}})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": LOCAL_SYSTEM_PROMPT}, {"role": "user", "content": content}],
            "temperature": 0.1,
            "stream": False,
        }
        if self.max_output_tokens:
            body["max_tokens"] = self.max_output_tokens
        run = Run("nim" if self.provider == "nim" and not self.hosted else self.name, model=self.model,
                  mode="hosted" if self.hosted else "local")
        started = time.monotonic()
        try:
            text, usage = await self._complete(body)
            result = normalize(parse_json(text))
        except AnalysisError as e:
            run.ok = False
            # paused before anything was sent: no request to account for
            e.runs = [] if getattr(e, "unsent", False) else [self._finish_run(run, started, {})]
            raise
        result["_runs"] = [self._finish_run(run, started, usage)]
        return result

    def _finish_run(self, run: Run, started: float, usage: dict) -> dict:
        run.requests = 1
        run.duration_ms = int((time.monotonic() - started) * 1000)
        run.input_tokens = int(usage.get("prompt_tokens") or 0)
        run.output_tokens = int(usage.get("completion_tokens") or 0)
        run.cost_usd = token_cost(run) if self.hosted else local_cost(run.duration_ms, self.cost_per_hour)
        return run.to_dict()

    FORMATS = ("json_schema", "json_object", "none")

    async def _complete(self, body: dict) -> tuple[str, dict]:
        # Prefer schema-constrained decoding; step down for servers that don't support it,
        # and remember what worked so later requests skip the failed attempts.
        for mode in self.formats[self.formats.index(self._format_mode):]:
            req = dict(body)
            if mode == "json_schema":
                req["response_format"] = {"type": "json_schema", "json_schema": {"name": "save_analysis", "schema": SCHEMA, "strict": True}}
            elif mode == "nvext":
                req["nvext"] = {"guided_json": SCHEMA}
            elif mode == "json_object":
                req["response_format"] = {"type": "json_object"}
            r = await self._post(req)
            if r.status_code in (401, 403):
                hint = " (NVIDIA_API_KEY / LOCAL_LLM_API_KEY)" if self.provider == "nim" else " (LOCAL_LLM_API_KEY)"
                raise AnalysisError(f"The LLM server rejected the API key{hint}: {r.text[:200]}")
            if r.status_code in (400, 422) and mode != "none":
                log.info("Local LLM rejected response_format=%s (%s); trying a simpler format", mode, r.text[:200])
                continue
            if r.status_code == 404:
                raise AnalysisError(f"Local LLM: model or endpoint not found ({r.text[:200]}). Is '{self.model}' pulled?")
            if r.status_code >= 400:
                raise AnalysisError(f"Local LLM error {r.status_code}: {r.text[:300]}")
            self._format_mode = mode
            try:
                data = r.json()
                return data["choices"][0]["message"]["content"] or "", data.get("usage") or {}
            except (KeyError, IndexError, ValueError, TypeError) as e:
                raise AnalysisError(f"Unexpected reply from the local LLM: {r.text[:300]}") from e
        raise AnalysisError("The local LLM rejected every request format.")

    async def _post(self, req: dict) -> httpx.Response:
        """POST with retries on rate limits and temporary unavailability (the hosted NVIDIA API
        allows ~40 requests/minute; a self-hosted NIM answers 503 while the model loads). A hosted model that
        hit its limit is paused: short waits are waited out, longer ones fail fast with the time it's back."""
        for attempt in range(self.MAX_RETRIES + 1):
            if self.hosted:
                hit = limits.blocked(self.name, self.model)
                if hit and limits.wait_seconds(hit[0]) > limits.WAIT_AT_MOST:
                    e = RateLimited(self.name, self.model, hit[0], hit[1])
                    e.unsent = attempt == 0
                    raise e
                if hit:
                    await self._sleep(limits.wait_seconds(hit[0]))
            try:
                r = await self.http.post(self.url, json=req, headers=self.headers, timeout=self.timeout)
            except httpx.HTTPError as e:
                raise AnalysisError(f"Can't reach the local LLM at {self.url}: {e!r}") from e
            if self.hosted:
                record_limits(self.name, r.headers, model=self.model, status=r.status_code)
                if r.status_code in (402, 429):
                    entry = limits.mark_limited(self.name, self.model, r.headers, r.text, r.status_code)
                    until = limits._parse_iso(entry["blocked_until"])
                    if entry["blocked_reason"] == "credit" or limits.wait_seconds(until) > limits.WAIT_AT_MOST or attempt == self.MAX_RETRIES:
                        raise RateLimited(self.name, self.model, until, entry["blocked_reason"])
                    continue   # the pause is short: the check above waits it out
            if r.status_code not in self.RETRY_STATUSES or attempt == self.MAX_RETRIES:
                return r
            try:
                wait = float(r.headers.get("retry-after", ""))
            except ValueError:
                wait = 2.0 * 2 ** attempt
            log.info("LLM server answered %s; retrying in %.0fs", r.status_code, wait)
            await self._sleep(min(wait, 30.0))
        return r


def rules_analysis(ocr: OcrResult | None, signals: Signals | None, note: str | None = None) -> dict:
    """No-LLM identification from OCR + rules. Rough by design: always low confidence."""
    out = blank_analysis()
    if not ocr or not ocr.lines:
        out["confidence_reason"] = "No text could be read from the screenshot."
        out["title"] = "Screenshot"
        return out
    s = signals or extract_signals(ocr)
    out["screenshot_text"] = ocr.text[:500]
    out["source_platform"] = s.platform or "other"
    out["details"]["posted_by"] = s.handles[0] if s.handles else None
    reason = []
    if s.github_repos:
        repo = s.github_repos[0]
        out.update(category="github_repo", title=repo, canonical_url=f"https://github.com/{repo}", confidence=75)
        out["details"]["github_full_name"] = repo
        reason.append(f"a GitHub link ({repo}) is visible")
    elif s.imdb_ids:
        out.update(category="movie", canonical_url=f"https://www.imdb.com/title/{s.imdb_ids[0]}/", confidence=65)
        out["details"]["imdb_id"] = s.imdb_ids[0]
        out["title"] = s.title_candidates[0] if s.title_candidates else s.imdb_ids[0]
        reason.append("an IMDb id is visible")
    else:
        out["category"] = s.category or "other"
        out["title"] = s.title_candidates[0] if s.title_candidates else ocr.lines[0].text
        out["canonical_url"] = next((u if u.startswith("http") else f"https://{u}" for u in s.urls), None)
        out["confidence"] = 35 if s.category else 20
        reason.append(f"guessed from {'vocabulary and ' if s.category else ''}the most prominent text")
    out["summary"] = note or ""
    out["tags"] = [t for t in (out["category"] if out["category"] != "other" else None, s.platform) if t]
    out["confidence_reason"] = "OCR + rules only (no AI model): " + ", ".join(reason) + ". Please check."
    return out


class Deferred(Exception):
    """The Claude step was queued for the Batches API; the pipeline finishes it later."""

    def __init__(self, params: dict, context: dict):
        super().__init__("queued for batch")
        self.params, self.context = params, context


class AnalyzerRouter:
    """Runs OCR once, then the configured backend(s)."""

    LOCAL_RETRY_AFTER = 300  # seconds to skip an unreachable local server in hybrid mode

    def __init__(self, settings: Settings, http: httpx.AsyncClient, ocr: Ocr | None = None,
                 claude: Any = None, local: Any = None, fallback: Any = None, spent: Any = None):
        self.mode = settings.resolved_analyzer()
        self.budget = settings.monthly_budget_usd
        self.spent = spent  # () -> USD measured so far this month
        self.hosted = settings.hosted_llm in HOSTED_LLMS
        self.escalate_below = settings.escalate_below
        self.batch = settings.claude_batch
        self.ocr = ocr if ocr is not None else Ocr(settings.ocr_engine, settings.ocr_langs)
        self.claude = claude
        self.local = local
        self.fallback = fallback  # hybrid: a hosted provider used instead of Claude
        self._local_down_until = 0.0
        # Hybrid: your own server first, then the fallback provider: Claude, or the selected hosted provider.
        hosted_fallback = self.mode == "hybrid" and settings.hosted_llm in HOSTED_LLMS
        if self.mode in ("claude", "hybrid") and self.claude is None and not hosted_fallback:
            if not settings.anthropic_api_key:
                raise ValueError(f"MAGPIE_ANALYZER={self.mode} needs ANTHROPIC_API_KEY")
            self.claude = ScreenshotAnalyzer(model=settings.model, effort=settings.effort or None,
                                             fetch_max_tokens=settings.fetch_max_tokens or None,
                                             max_tokens=settings.max_output_tokens or None,
                                             max_image_edge=settings.max_image_edge)
        if self.mode in ("local", "hybrid") and self.local is None:
            self.local = LocalLLMAnalyzer(settings, http, source="local" if self.mode == "hybrid" else "auto")
        if hosted_fallback and self.fallback is None:
            if not settings.llm_api_key:
                raise ValueError(f"Hybrid fallback {settings.hosted_llm} needs its API key")
            self.fallback = LocalLLMAnalyzer(settings, http, source="hosted")
        log.info("Analyzer: %s (OCR: %s, Claude batch: %s)", self.mode, settings.ocr_engine, self.batch)

    def over_budget(self) -> bool:
        """This month's measured spend has reached MAGPIE_MONTHLY_BUDGET_USD (0 = no limit)."""
        return bool(self.budget and self.spent and self.spent() >= self.budget)

    def budget_error(self) -> AnalysisError:
        return AnalysisError(f"The monthly budget of ${self.budget:g} is used up, so paid AI providers are paused "
                             "until next month. Raise it under Settings → Spending limits.")

    async def analyze(self, image: bytes | None, media_type: str | None, note: str | None = None,
                      correction: dict | None = None, interactive: bool = False,
                      link_url: str | None = None, page_hints: str = "", web: bool = True) -> dict:
        """Identify a screenshot, or a shared link when `image` is None (then `page_hints` carries
        the page's reader-view text). `interactive` requests (the user is waiting, e.g. a
        correction) never go through a batch."""
        started = time.monotonic()
        ocr = await self.ocr.read(image) if image is not None else None
        signals = extract_signals(ocr) if ocr and ocr.lines else None
        hints = hints_prompt(ocr, signals) + page_hints
        link = {"link_url": link_url, "web": web} if link_url else {}
        context: dict[str, Any] = {"used": [], "runs": [], "ocr_text": ocr.text if ocr and ocr.lines else ""}
        if ocr and ocr.lines:
            context["used"].append("ocr")
            context["runs"].append(Run("ocr", model=ocr.engine, mode="local", requests=1,
                                       duration_ms=int((time.monotonic() - started) * 1000)).to_dict())

        async def claude_step() -> dict:
            if self.over_budget():
                raise self.budget_error()
            if self.batch and not interactive:
                raise Deferred(self.claude.build_params(image, media_type, note, correction, hints, **link), context)
            try:
                result = await self.claude.analyze(image, media_type, note=note, correction=correction, hints=hints, **link)
            except AnalysisError as e:
                e.runs = context["runs"] + e.runs
                raise
            context["used"].append("claude")
            return result

        async def fallback_step() -> dict:
            """The stronger second opinion in hybrid mode: Claude (batchable) or the chosen hosted provider."""
            if self.fallback is None:
                return await claude_step()
            if self.over_budget():
                raise self.budget_error()
            try:
                result = await self.fallback.analyze(image, media_type, note=note, correction=correction, hints=hints, **link)
            except AnalysisError as e:
                e.runs = context["runs"] + e.runs
                raise
            context["used"].append(self.fallback.label)
            return result

        if self.mode == "ocr":
            result = rules_analysis(ocr, signals, note)
            context["used"].append("rules")
        elif self.mode == "claude":
            result = await claude_step()
        elif self.mode == "local":
            if self.hosted and self.over_budget():
                raise self.budget_error()
            try:
                result = await self.local.analyze(image, media_type, note=note, correction=correction, hints=hints, **link)
            except AnalysisError as e:
                e.runs = context["runs"] + e.runs
                raise
            context["used"].append(self.local.label)
        else:  # hybrid
            result = None
            if time.monotonic() >= self._local_down_until:
                try:
                    result = await self.local.analyze(image, media_type, note=note, correction=correction, hints=hints, **link)
                    context["used"].append(self.local.label)
                except AnalysisError as e:
                    context["runs"] += e.runs
                    if "Can't reach" in str(e):
                        self._local_down_until = time.monotonic() + self.LOCAL_RETRY_AFTER
                    log.warning("Local model failed, asking Claude: %s", e)
            if result is None or result.get("confidence", 0) < self.escalate_below:
                if result is not None and self.over_budget():
                    log.info("Monthly budget reached: keeping the local answer")  # nothing paid is called
                else:
                    local = result
                    if result is not None:
                        context["runs"] += result.pop("_runs", [])
                        context["local_result"] = {k: v for k, v in result.items() if not k.startswith("_")}
                    try:
                        result = await fallback_step()
                    except RateLimited as e:
                        if local is None:
                            raise
                        log.info("%s; keeping the local answer", e)   # a less sure answer beats none
                        context["runs"] = e.runs
                        result = local
        return self.finish(result, context)

    async def finish_batch(self, params: dict, message: Any, context: dict) -> dict:
        """Complete a Claude step that ran in a batch."""
        try:
            result = await self.claude.finish(params, message)
        except AnalysisError as e:
            e.runs = context["runs"] + e.runs
            raise
        context["used"].append("claude")
        return self.finish(result, context)

    @staticmethod
    def finish(result: dict, context: dict) -> dict:
        result["_runs"] = context["runs"] + result.pop("_runs", [])
        result["_analyzer"] = context["used"]
        if context.get("ocr_text"):
            result["_ocr_text"] = context["ocr_text"]
            if not result.get("screenshot_text"):
                result["screenshot_text"] = context["ocr_text"][:500]
        return result
