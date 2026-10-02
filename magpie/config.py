"""Settings: environment variables (and .env), overridden by what's saved from the web UI.

Every value is parsed leniently: a bad value never stops the server. It falls back to the
default and is reported as a problem, which the UI shows next to the setting.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the app runs without extra dependencies."""
    try:
        if not path.exists():
            return
        text = path.read_text()
    except OSError as e:
        log.warning("Can't read %s: %s", path, e)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# The project used to be called Keeper: keep honouring KEEPER_* settings from existing installs.
for _key, _value in list(os.environ.items()):
    if _key.startswith("KEEPER_") and "MAGPIE_" + _key[7:] not in os.environ:
        os.environ["MAGPIE_" + _key[7:]] = _value

OVERRIDES_FILE = "settings.json"
ANALYZERS = ("auto", "claude", "local", "hybrid", "ocr")

# Hosted OpenAI-compatible providers usable in place of a local LLM: preset -> (base URL, default model, key attr, label)
HOSTED_LLMS = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "openai_api_key", "OpenAI"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "gemini_api_key", "Google Gemini"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "openrouter_api_key", "OpenRouter"),
    "groq": ("https://api.groq.com/openai/v1", "meta-llama/llama-4-scout-17b-16e-instruct", "groq_api_key", "Groq"),
}


@dataclass(frozen=True)
class Spec:
    """One user-facing setting."""
    attr: str
    env: str
    kind: str                      # str | secret | int | float | bool | choice
    default: Any
    group: str
    label: str
    help: str = ""
    choices: tuple = ()
    min: float | None = None
    max: float | None = None


SPECS: list[Spec] = [
    Spec("analyzer", "MAGPIE_ANALYZER", "choice", "auto", "Identification", "Analyzer",
         "auto picks hybrid if Claude and a local LLM are both set, else whichever is set, else OCR only.", ANALYZERS),
    Spec("anthropic_api_key", "ANTHROPIC_API_KEY", "secret", None, "Identification", "Anthropic API key",
         "Needed when Anthropic (Claude) is the provider, or in Hybrid mode. https://console.anthropic.com"),
    Spec("model", "MAGPIE_MODEL", "str", "claude-opus-5", "Identification", "Claude model"),
    Spec("hosted_llm", "MAGPIE_LLM_PROVIDER", "choice", "none", "Other AI providers", "Hosted provider",
         "Use OpenAI, Gemini, OpenRouter or Groq instead of a local server or Claude. Sets the endpoint and a default "
         "model; the model can be overridden under Local LLM → Model. Works with the local and hybrid analyzers.",
         ("none", *HOSTED_LLMS)),
    Spec("openai_api_key", "OPENAI_API_KEY", "secret", None, "Other AI providers", "OpenAI API key",
         "https://platform.openai.com/api-keys"),
    Spec("gemini_api_key", "GEMINI_API_KEY", "secret", None, "Other AI providers", "Gemini API key",
         "https://aistudio.google.com/apikey"),
    Spec("openrouter_api_key", "OPENROUTER_API_KEY", "secret", None, "Other AI providers", "OpenRouter API key",
         "https://openrouter.ai/keys"),
    Spec("groq_api_key", "GROQ_API_KEY", "secret", None, "Other AI providers", "Groq API key",
         "https://console.groq.com/keys"),
    Spec("local_llm_url", "LOCAL_LLM_URL", "str", None, "Local LLM", "Server URL",
         "Any OpenAI-compatible server, e.g. http://ollama:11434/v1"),
    Spec("local_llm_model", "LOCAL_LLM_MODEL", "str", "qwen3-vl:8b", "Local LLM", "Model"),
    Spec("local_llm_api_key", "LOCAL_LLM_API_KEY", "secret", None, "Local LLM", "API key",
         "Only if the server needs one (NVIDIA_API_KEY is also accepted)."),
    Spec("local_llm_provider", "LOCAL_LLM_PROVIDER", "choice", "auto", "Local LLM", "Provider", "", ("auto", "openai", "nim")),
    Spec("local_llm_vision", "LOCAL_LLM_VISION", "bool", True, "Local LLM", "Vision model",
         "Turn off for text-only models: they get the OCR text instead of the image."),
    Spec("local_llm_timeout", "LOCAL_LLM_TIMEOUT", "float", 300.0, "Local LLM", "Timeout (s)", min=1),
    Spec("local_cost_per_hour", "MAGPIE_LOCAL_COST_PER_HOUR", "float", 0.0, "Local LLM", "Running cost (USD/hour)", min=0),
    # Cost controls that apply to every provider.
    Spec("monthly_budget_usd", "MAGPIE_MONTHLY_BUDGET_USD", "float", 0.0, "Spending limits", "Monthly budget (USD)",
         "0 = no limit. Once this month's measured spend reaches it, Magpie stops calling paid providers: hybrid keeps "
         "the first model's answer, other modes report the limit.", min=0),
    Spec("escalate_below", "MAGPIE_ESCALATE_BELOW", "int", 70, "Spending limits", "Escalate to the fallback below (confidence %)",
         "Hybrid mode: ask the fallback provider (Claude, OpenAI, Gemini, ...) when your own model is less sure than this.",
         min=0, max=100),
    Spec("gemini_web_search", "MAGPIE_GEMINI_WEB_SEARCH", "bool", True, "Other AI providers", "Gemini: search the web (Google Search)",
         "Lets Gemini models ground their identification in Google Search, as Claude does with web search: they find the "
         "repository, the article's own page and related links themselves. Search queries are billed by Google "
         "(Gemini 3: about $14 per 1,000; the free tier includes some). Off: Gemini answers from the screenshot, "
         "and Magpie looks things up afterwards."),
    Spec("max_output_tokens", "MAGPIE_MAX_OUTPUT_TOKENS", "int", 0, "Spending limits", "Max output tokens",
         "0 = provider default. Caps the reply (and, for Claude, its thinking) of every request.", min=0),
    Spec("max_image_edge", "MAGPIE_MAX_IMAGE_EDGE", "int", 2000, "Spending limits", "Max image edge (px)",
         "Screenshots are scaled down to this before being sent to any model. Smaller = fewer tokens.", min=256, max=10000),
    Spec("effort", "MAGPIE_EFFORT", "choice", "medium", "Claude options", "Effort", "",
         ("low", "medium", "high", "xhigh", "max")),
    Spec("claude_batch", "MAGPIE_CLAUDE_BATCH", "bool", True, "Claude options", "Use Message Batches",
         "50% cheaper; results usually within an hour."),
    Spec("batch_poll_seconds", "MAGPIE_BATCH_POLL_SECONDS", "int", 60, "Claude options", "Batch poll interval (s)", min=5),
    Spec("fetch_max_tokens", "MAGPIE_FETCH_MAX_TOKENS", "int", 8000, "Claude options", "Max tokens per fetched page",
         "0 = no cap.", min=0),
    Spec("verified_confidence", "MAGPIE_VERIFIED_CONFIDENCE", "int", 90, "Verification", "Count as verified from confidence (%)",
         "Answers this sure count as verified, even when no source (TMDB, GitHub, ...) confirmed them. Anything below and "
         "unconfirmed shows a Verify button and appears under To check. You can lower it to 60, no further.", min=60, max=100),
    Spec("ocr_engine", "MAGPIE_OCR", "choice", "rapidocr", "OCR", "OCR engine", "", ("rapidocr", "tesseract", "off")),
    Spec("ocr_langs", "MAGPIE_OCR_LANGS", "str", "eng", "OCR", "Tesseract languages", "e.g. eng+heb"),
    Spec("enrich", "MAGPIE_ENRICH", "bool", True, "Lookups", "Online lookups",
         "TMDB, GitHub, Open Library, recipe pages. Turn off for air-gapped installs."),
    Spec("tmdb_api_key", "TMDB_API_KEY", "secret", None, "Lookups", "TMDB API key",
         "Posters, cast, IMDb ids. https://www.themoviedb.org/settings/api"),
    Spec("omdb_api_key", "OMDB_API_KEY", "secret", None, "Lookups", "OMDb API key",
         "IMDb, Rotten Tomatoes and Metacritic scores. https://www.omdbapi.com/apikey.aspx"),
    Spec("github_token", "GITHUB_TOKEN", "secret", None, "Lookups", "GitHub token",
         "Raises the GitHub rate limit from 60 to 5000 requests/hour."),
    Spec("debug", "MAGPIE_DEBUG", "bool", False, "Logging", "Debug mode",
         "Records every conversation with the AI models, together with the screenshot or link that went in and the card that came "
         "out, and adds a Debug view to the app to browse them. Implies the two logging settings below; for troubleshooting, "
         "since the logs contain what is in your screenshots."),
    Spec("log_conversations", "MAGPIE_LOG_CONVERSATIONS", "bool", False, "Logging", "Save conversations with the AI models",
         "Keeps what Magpie sent to each model and what came back, one file per operation (analysis, correction, batch result), "
         "for debriefing and inspection. The logs contain what is in your screenshots; they never contain API keys."),
    Spec("log_images", "MAGPIE_LOG_IMAGES", "bool", False, "Logging", "Keep the screenshots in the logs",
         "Saves the image that was sent next to each log file. Off: only its size and a hash are recorded."),
    Spec("log_retention_days", "MAGPIE_LOG_RETENTION_DAYS", "int", 30, "Logging", "Delete logs older than (days)",
         "0 = keep them until you delete them.", min=0),
    Spec("api_token", "MAGPIE_API_TOKEN", "secret", None, "Access", "Access tokens",
         "Required by every client when set. Separate several tokens with commas or spaces (e.g. one per device, so one can be revoked alone). Recommended whenever the server is reachable beyond localhost."),
]
SPEC_BY_ATTR = {s.attr: s for s in SPECS}
SPEC_BY_ENV = {s.env: s for s in SPECS}
_FALSE = ("0", "false", "no", "off")
_TRUE = ("1", "true", "yes", "on")


def parse_value(spec: Spec, raw: Any) -> Any:
    """Convert a raw (env or JSON) value for `spec`. Raises ValueError with a readable reason."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return spec.default
    if spec.kind in ("str", "secret"):
        return str(raw).strip()
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        v = str(raw).strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        raise ValueError(f"{raw!r} is not true/false")
    if spec.kind == "choice":
        v = str(raw).strip().lower()
        if v not in spec.choices:
            raise ValueError(f"{raw!r} is not one of {', '.join(spec.choices)}")
        return v
    try:
        v = int(str(raw).strip()) if spec.kind == "int" else float(str(raw).strip())
    except ValueError:
        raise ValueError(f"{raw!r} is not a{'n integer' if spec.kind == 'int' else ' number'}") from None
    if spec.min is not None and v < spec.min:
        raise ValueError(f"{v} is below the minimum ({spec.min:g})")
    if spec.max is not None and v > spec.max:
        raise ValueError(f"{v} is above the maximum ({spec.max:g})")
    return v


def _env_value(spec: Spec) -> str | None:
    raw = os.environ.get(spec.env)
    if spec.attr == "local_llm_api_key" and not raw:
        raw = os.environ.get("NVIDIA_API_KEY")
    if spec.attr == "max_image_edge" and not raw:
        raw = os.environ.get("LOCAL_LLM_MAX_IMAGE_EDGE")  # its name before it applied to every provider
    return raw or None


def _from_env(attr: str):
    def factory():
        spec = SPEC_BY_ATTR[attr]
        try:
            return parse_value(spec, _env_value(spec))
        except ValueError:
            return spec.default  # reported by Settings.problems()
    return factory


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("MAGPIE_DATA_DIR") or "data").resolve())
    model: str = field(default_factory=_from_env("model"))
    tmdb_api_key: str | None = field(default_factory=_from_env("tmdb_api_key"))
    omdb_api_key: str | None = field(default_factory=_from_env("omdb_api_key"))
    github_token: str | None = field(default_factory=_from_env("github_token"))
    # When set, every API/media request must present this token (clients: Bearer header).
    api_token: str | None = field(default_factory=_from_env("api_token"))

    # Which analyzer identifies screenshots: auto | claude | local | hybrid | ocr
    analyzer: str = field(default_factory=_from_env("analyzer"))
    anthropic_api_key: str | None = field(default_factory=_from_env("anthropic_api_key"))
    # On-prem LLM: any OpenAI-compatible server (Ollama, vLLM, LM Studio, llama.cpp server)
    hosted_llm: str = field(default_factory=_from_env("hosted_llm"))
    openai_api_key: str | None = field(default_factory=_from_env("openai_api_key"))
    gemini_api_key: str | None = field(default_factory=_from_env("gemini_api_key"))
    gemini_web_search: bool = field(default_factory=_from_env("gemini_web_search"))
    openrouter_api_key: str | None = field(default_factory=_from_env("openrouter_api_key"))
    groq_api_key: str | None = field(default_factory=_from_env("groq_api_key"))
    local_llm_url: str | None = field(default_factory=_from_env("local_llm_url"))
    local_llm_model: str = field(default_factory=_from_env("local_llm_model"))
    local_llm_api_key: str | None = field(default_factory=_from_env("local_llm_api_key"))
    # openai (Ollama, vLLM, LM Studio, llama.cpp…) | nim (NVIDIA NIM, self-hosted or build.nvidia.com) | auto
    local_llm_provider: str = field(default_factory=_from_env("local_llm_provider"))
    # Longest image edge sent to the local model (smaller = faster, fewer tokens)
    max_image_edge: int = field(default_factory=_from_env("max_image_edge"))
    # Set to false for text-only models: they then get the OCR text instead of the image.
    local_llm_vision: bool = field(default_factory=_from_env("local_llm_vision"))
    local_llm_timeout: float = field(default_factory=_from_env("local_llm_timeout"))
    # hybrid mode: ask Claude when the local model's confidence is below this
    escalate_below: int = field(default_factory=_from_env("escalate_below"))
    # Cost controls for every provider (see docs/COSTS.md)
    monthly_budget_usd: float = field(default_factory=_from_env("monthly_budget_usd"))  # 0 = no limit
    max_output_tokens: int = field(default_factory=_from_env("max_output_tokens"))      # 0 = provider default
    # From this confidence an answer counts as verified (below it, and unconfirmed by a source, the item is "unverified")
    verified_confidence: int = field(default_factory=_from_env("verified_confidence"))
    # OCR pre-pass: rapidocr (bundled, CPU) | tesseract (needs the binary; better for Hebrew/Arabic/...) | off
    ocr_engine: str = field(default_factory=_from_env("ocr_engine"))
    ocr_langs: str = field(default_factory=_from_env("ocr_langs"))  # tesseract only, e.g. eng+heb
    # Claude-only options (see docs/COSTS.md)
    effort: str = field(default_factory=_from_env("effort"))         # low|medium|high|xhigh|max
    fetch_max_tokens: int = field(default_factory=_from_env("fetch_max_tokens"))  # 0 = no cap
    # Send new screenshots to Claude through the Message Batches API (50% cheaper; results in minutes, max 24 h)
    claude_batch: bool = field(default_factory=_from_env("claude_batch"))
    batch_poll_seconds: int = field(default_factory=_from_env("batch_poll_seconds"))
    # Running cost of your on-prem inference box, for the usage report (e.g. 350 W at $0.20/kWh = 0.07)
    local_cost_per_hour: float = field(default_factory=_from_env("local_cost_per_hour"))

    # Conversation logs: what was sent to each model and what came back (see magpie/convlog.py)
    debug: bool = field(default_factory=_from_env("debug"))   # records everything and adds the app's Debug view
    log_conversations: bool = field(default_factory=_from_env("log_conversations"))
    log_images: bool = field(default_factory=_from_env("log_images"))
    log_retention_days: int = field(default_factory=_from_env("log_retention_days"))

    # Online metadata lookups (TMDB, GitHub, recipe pages...). Turn off for air-gapped installs.
    enrich: bool = field(default_factory=_from_env("enrich"))

    # Values saved from the web UI (env name -> raw value); they take precedence over the environment.
    overrides: dict = field(default_factory=dict, repr=False)
    # Problems found while loading (e.g. an unreadable settings file).
    load_errors: list = field(default_factory=list, repr=False)

    @classmethod
    def load(cls) -> "Settings":
        """Environment + the overrides saved from the UI. Never raises."""
        s = cls()
        s.apply_overrides(s.read_overrides())
        return s

    # ---- UI overrides -----------------------------------------------------------

    @property
    def overrides_path(self) -> Path:
        return self.data_dir / OVERRIDES_FILE

    def read_overrides(self) -> dict:
        try:
            if not self.overrides_path.exists():
                return {}
            data = json.loads(self.overrides_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            return {k: v for k, v in data.items() if k in SPEC_BY_ENV}
        except (OSError, ValueError) as e:
            self.load_errors.append(f"Can't read saved settings ({self.overrides_path}): {e}")
            log.error("Can't read saved settings %s: %s", self.overrides_path, e)
            return {}

    def apply_overrides(self, overrides: dict) -> None:
        """Apply UI-saved values; invalid ones are kept (so they can be fixed) but not used."""
        self.overrides = dict(overrides)
        for env, raw in self.overrides.items():
            spec = SPEC_BY_ENV[env]
            try:
                setattr(self, spec.attr, parse_value(spec, raw))
            except ValueError:
                pass  # reported by problems()

    def save_overrides(self, changes: dict) -> list[str]:
        """Merge `changes` (env name -> value; None removes the saved value, back to env/default) and
        persist them. For secrets, "" saves an explicit "no key". Returns notices for the user.
        Raises ValueError for unknown names or invalid values, OSError if the file can't be written."""
        errors = {}
        for env, raw in changes.items():
            spec = SPEC_BY_ENV.get(env)
            if spec is None:
                errors[env] = "unknown setting"
                continue
            try:
                parse_value(spec, raw)
            except ValueError as e:
                errors[env] = str(e)
        if errors:
            raise SettingsError(errors)
        notices = []
        changes = dict(changes)
        # The local LLM key is sent to LOCAL_LLM_URL: never let it follow the URL to another server.
        if "LOCAL_LLM_URL" in changes and "LOCAL_LLM_API_KEY" not in changes and self.local_llm_api_key:
            spec = SPEC_BY_ENV["LOCAL_LLM_URL"]
            new_url = parse_value(spec, changes["LOCAL_LLM_URL"]) or parse_value(spec, _env_value(spec))
            if _origin(new_url) != _origin(self.local_llm_url):
                changes["LOCAL_LLM_API_KEY"] = ""
                notices.append("The local LLM API key was removed because the server URL now points to a different "
                               "server. Enter the key again if the new server needs one.")
        merged = dict(self.overrides)
        for env, raw in changes.items():
            blank = raw is None or (isinstance(raw, str) and raw.strip() == "")
            if raw is None or (blank and SPEC_BY_ENV[env].kind != "secret"):
                merged.pop(env, None)
            else:
                merged[env] = "" if blank else raw
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.overrides_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=2, sort_keys=True))
        try:
            os.chmod(tmp, 0o600)  # holds API keys
        except OSError:
            pass
        tmp.replace(self.overrides_path)
        self.reload_from(merged)
        return notices

    def reload_from(self, overrides: dict) -> None:
        """Recompute every setting from the environment plus `overrides` (in place)."""
        fresh = Settings(data_dir=self.data_dir)
        for f in fields(self):
            if f.name not in ("data_dir", "overrides", "load_errors"):
                setattr(self, f.name, getattr(fresh, f.name))
        self.load_errors = [e for e in self.load_errors if "saved settings" not in e]
        self.apply_overrides(overrides)

    def source_of(self, spec: Spec) -> str:
        if spec.env in self.overrides:
            return "ui"
        if _env_value(spec):
            return "env"
        return "default"

    # ---- diagnostics --------------------------------------------------------------

    def problems(self) -> list[dict]:
        """What's wrong or missing, for the status banner and the settings screen.
        level: error (something doesn't work) | warning (works, but degraded) | info (optional)."""
        out: list[dict] = []

        def add(level: str, message: str, key: str | None = None) -> None:
            out.append({"level": level, "key": key, "message": message})

        for message in self.load_errors:
            add("error", message)
        for spec in SPECS:
            source = self.source_of(spec)
            raw = self.overrides.get(spec.env) if source == "ui" else _env_value(spec)
            try:
                parse_value(spec, raw)
            except ValueError as e:
                where = "saved setting" if source == "ui" else spec.env
                add("error", f"{spec.label}: invalid value in {where} ({e}); using the default.", spec.env)

        mode = self.resolved_analyzer()
        hosted = self.hosted_llm in HOSTED_LLMS
        if mode == "claude" and not self.anthropic_api_key:
            add("error", "Anthropic (Claude) is selected but its API key is not set, so screenshots can't be identified.",
                "ANTHROPIC_API_KEY")
        if mode == "hybrid":
            if not self.local_llm_url:
                add("error", "Hybrid mode tries your own server first, so it needs its endpoint.", "LOCAL_LLM_URL")
            if not hosted and not self.anthropic_api_key:
                add("error", "Hybrid mode falls back to Claude when the first model is unsure, and that needs an Anthropic "
                    "API key. Add one, or pick another fallback provider.", "ANTHROPIC_API_KEY")
        if hosted and mode in ("local", "hybrid") and not self.llm_api_key:
            name = HOSTED_LLMS[self.hosted_llm][3]
            add("error", f"{name} is selected but its API key is not set.", HOSTED_LLMS[self.hosted_llm][2].upper())
        elif mode == "local" and not self.llm_url:
            add("error", "The local analyzer needs a server URL or a hosted provider.", "LOCAL_LLM_URL")
        if mode == "ocr" and self.analyzer == "auto":
            add("warning", "No AI model is configured, so screenshots are identified with OCR + rules only (rough). "
                "Add an Anthropic API key or a local LLM URL.", "ANTHROPIC_API_KEY")
        if not self.api_tokens:
            add("warning", "No access token is set: anyone who can reach this server can use it.", "MAGPIE_API_TOKEN")
        if self.enrich and not self.tmdb_api_key:
            add("info", "Without a TMDB API key, films and TV shows get fewer details (posters, cast).", "TMDB_API_KEY")
        if self.enrich and not self.omdb_api_key:
            add("info", "Without an OMDb API key, films and TV shows get no IMDb / Rotten Tomatoes scores.", "OMDB_API_KEY")
        return out

    # ---- derived values ---------------------------------------------------------

    @property
    def api_tokens(self) -> list[str]:
        """Every accepted access token (MAGPIE_API_TOKEN may hold several, separated by commas or whitespace)."""
        return [t for t in re.split(r"[,\s]+", self.api_token or "") if t]

    @property
    def llm_url(self) -> str | None:
        """Endpoint of the OpenAI-compatible model: the chosen hosted provider, else LOCAL_LLM_URL."""
        return HOSTED_LLMS[self.hosted_llm][0] if self.hosted_llm in HOSTED_LLMS else self.local_llm_url

    @property
    def llm_api_key(self) -> str | None:
        if self.hosted_llm in HOSTED_LLMS:
            return getattr(self, HOSTED_LLMS[self.hosted_llm][2])
        return self.local_llm_api_key

    @property
    def llm_model(self) -> str:
        """A hosted provider's default model applies unless LOCAL_LLM_MODEL was set explicitly."""
        if self.hosted_llm in HOSTED_LLMS and self.source_of(SPEC_BY_ATTR["local_llm_model"]) == "default":
            return HOSTED_LLMS[self.hosted_llm][1]
        return self.local_llm_model

    def resolved_llm_provider(self, local: bool = False) -> str:
        """Wire protocol of the OpenAI-compatible model: openai | nim. `local` ignores the hosted preset."""
        if self.hosted_llm in HOSTED_LLMS and not local:
            return "openai"
        if self.local_llm_provider != "auto":
            return self.local_llm_provider
        url = (self.local_llm_url if local else self.llm_url) or ""
        url, key = url.lower(), (self.local_llm_api_key if local else self.llm_api_key) or ""
        if "api.nvidia.com" in url or key.startswith("nvapi-"):
            return "nim"
        return "openai"

    def paid_models(self) -> list[tuple[str, str]]:
        """The (provider, model) pairs the configured analyzer calls that have limits: Claude and hosted providers."""
        mode, hosted = self.resolved_analyzer(), self.hosted_llm in HOSTED_LLMS
        if mode == "claude":
            return [("claude", self.model)]
        if mode == "local":
            return [(self.hosted_llm, self.llm_model)] if hosted else []
        if mode == "hybrid":
            return [(self.hosted_llm, self.llm_model)] if hosted else [("claude", self.model)]
        return []

    def resolved_analyzer(self) -> str:
        """`auto` picks the best configured option: Claude, else the local LLM, else OCR rules."""
        if self.analyzer != "auto":
            return self.analyzer
        if self.hosted_llm in HOSTED_LLMS:
            return "local"
        if self.anthropic_api_key and self.llm_url:
            return "hybrid"
        if self.anthropic_api_key:
            return "claude"
        if self.llm_url:
            return "local"
        return "ocr"

    @property
    def db_path(self) -> Path:
        legacy = self.data_dir / "keeper.db"  # created before the rename to Magpie
        current = self.data_dir / "magpie.db"
        return legacy if legacy.exists() and not current.exists() else current

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


def _origin(url: str | None) -> tuple:
    parts = urlsplit((url or "").strip().lower())
    try:
        port = parts.port
    except ValueError:  # malformed port: treat as its own origin
        port = parts.netloc
    return parts.scheme, parts.hostname, port


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def mask(value: str | None) -> str:
    if not value:
        return ""
    return "•" * 6 + value[-4:] if len(value) > 8 else "•" * 6
