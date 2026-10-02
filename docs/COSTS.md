# What does a screenshot cost?

This page estimates what it costs to identify one screenshot in each mode, explains where the money goes, and describes the cost controls Magpie applies by default. It also shows how to check the estimate against what your server actually measures.

> **These are estimates.** They are built from the size of the request Magpie sends, Anthropic list prices, and assumptions about how often Claude searches the web. Magpie records the real usage of every analysis (see [Measuring real costs](#measuring-real-costs)). After a week of normal use, trust the **Usage & cost** panel over this page.

## Summary

With the defaults (hybrid mode, Claude Opus 5, batch processing, effort `medium`, 8k-token cap on fetched pages):

| Mode | Per screenshot | 300 screenshots / month (~10/day) |
|---|---|---|
| **`hybrid`** (default: local model first, Claude only when it isn't sure) | **~$0.05** average, if ~25% go to Claude | **~$15** |
| `claude` (every screenshot to Claude, batched) | ~$0.06 easy · **~$0.21 typical** · up to ~$0.70 | ~$20–65 |
| `claude` in real time (`MAGPIE_CLAUDE_BATCH=false`) | ~$0.10 easy · ~$0.40 typical · up to ~$1.30 | ~$35–120 |
| **Saved link, recognized** (GitHub, IMDb, TMDB, schema.org pages…) | **$0**: no model call | $0 |
| Saved link, unrecognized → Claude reads the page text (no web search, batched) | ~$0.03 (~$0.05 in real time) | – |
| `local` (your own model only) | ~$0.0002–0.001 in electricity | < $1 |
| `ocr` (no model) | ~$0 (about 1 s of CPU) | $0 |

Not included: hosting and GPU hardware. The metadata services Magpie uses (TMDB, GitHub, Open Library, and OMDb's free tier of 1,000 requests a day) cost nothing at this volume.

Before these cost controls, a typical Claude screenshot was estimated at **~$0.45** and the worst case at **~$1.50+**. See [Before and after](#before-and-after).

## Where the Claude cost comes from

### 1. The fixed part of every request: ~5,600 input tokens

| Part | Tokens | Notes |
|---|---|---|
| Screenshot | ~2,460 | Image tokens ≈ width × height / 750. A 1170×2532 iPhone screenshot is downscaled to 924×2000 before sending. A 1920×1080 desktop screenshot is ~2,770. |
| `save_analysis` result schema | ~1,780 | The structured form Claude fills in (category, title, links, details, confidence, alternatives…) |
| Instructions | ~420 | System prompt |
| Web search + fetch tool definitions | ~700 | Estimated |
| OCR hints | ~50–300 | The text Magpie's OCR read, plus the rule-based clues |
| **Total** | **~5,600** | ≈ $0.028 at $5/M (half of that in a batch) |

### 2. Web research, the multiplier

Claude doesn't just look at the image. It searches the web to confirm what it sees and to find the canonical page (the IMDb title, the GitHub repo, the original recipe).

- **Each search costs $0.01** ($10 per 1,000).
- **Each search adds its results to the conversation** (~2–4k tokens). Each fetched page adds up to `MAGPIE_FETCH_MAX_TOKENS` (8k by default).
- **Claude re-reads the growing conversation after every step.** We assume every step is billed as input again, which is why input tokens grow faster than linearly.

| Scenario | Searches / fetches | Input tokens | Output tokens |
|---|---|---|---|
| **Easy**: a legible title, e.g. "Past Lives" on a poster | 1 / 0 | ~14k | ~1k |
| **Typical**: a caption mention, needs confirming | 3 / 1 | ~63k | ~2k |
| **Hard**: an obscure or ambiguous item; Magpie's limits are 6 searches and 3 fetches | 6 / 3 | ~220k | ~5k |

### 3. Output: reasoning plus the filled-in form

Output costs $25 per million tokens (Opus 5). It is Claude's (adaptive) thinking plus the ~500–1,000-token result. With effort `medium` we expect ~1–5k tokens, or $0.03–0.13 at real-time prices.

### Worked example: typical screenshot, defaults

```
input   63,000 tokens × $5  / 1M = $0.315
output   2,000 tokens × $25 / 1M = $0.050
                                   ------
tokens                             $0.365  × 0.5 (batch) = $0.183
web searches   3 × $0.01                                 = $0.030
                                                          ------
                                                          ≈ $0.21
```

### Links cost less than screenshots

A saved link that Magpie recognizes (from the URL or the page's structured data) never calls a model.

For other pages, the model gets the reader-view text instead of an image, and no web tools. The request is:
- ~3k tokens of page text (capped at 12,000 characters)
- the ~1.8k-token result form
- the ~0.4k-token instructions
- ~1k output tokens

That's about $0.05 in real time and $0.03 batched. Web search is enabled only when the page can't be read (e.g. a login wall); then the cost is closer to a screenshot's.

## Cost controls for every provider

These apply to Claude, OpenAI, Gemini, OpenRouter and Groq alike (Settings → Spending limits):

| Setting | Effect |
|---|---|
| `MAGPIE_MONTHLY_BUDGET_USD` | Hard stop. When this month's measured spend reaches it, paid providers aren't called: hybrid keeps your own model's answer, other modes report that the budget is used up. `0` (default) = no limit. |
| `MAGPIE_ESCALATE_BELOW` | Hybrid: only answers below this confidence go to the fallback provider. |
| `MAGPIE_MAX_OUTPUT_TOKENS` | Caps every reply (for Claude, thinking included). `0` = provider default. |
| `MAGPIE_MAX_IMAGE_EDGE` | Screenshots are scaled down to this before being sent to any model. |

## Claude-only cost controls (on by default)

| # | Control | Setting | Effect |
|---|---|---|---|
| 1 | **Cap on fetched pages** | `MAGPIE_FETCH_MAX_TOKENS=8000` | An IMDb, recipe or news page can be 20–40k tokens, and it is re-read on every later step. The cap limits the hard cases: without it, the worst case could exceed $2. Little accuracy is lost, because the facts Magpie needs are near the top of those pages, and the enrichers look up ratings and ingredients separately. |
| 2 | **Lower effort** | `MAGPIE_EFFORT=medium` | Less thinking, so fewer output tokens and fewer, more targeted tool calls. Identifying a screenshot is not deep reasoning; raise it to `high` if the review rate climbs. |
| 4 | **Batch processing** | `MAGPIE_CLAUDE_BATCH=true` | New screenshots go through the Message Batches API: **50% off all tokens**. Results usually arrive within an hour (max 24 h). Web searches are billed at the normal rate. Re-analyses and corrections always run immediately, because you're waiting for them. If a batch request fails or expires, Magpie runs it in real time instead of leaving it stuck. |
| 5 | **Hybrid mode** | `MAGPIE_ANALYZER=hybrid`, `MAGPIE_ESCALATE_BELOW=70` | Your local vision model tries first. Only answers below 70% confidence, or failures, go to Claude, and those go in a batch too. If the local server is unreachable, Magpie uses Claude and retries the local server after 5 minutes. |

Also on by default: the OCR pre-pass (it lets small local models get more screenshots right, which means fewer escalations), and downscaling screenshots to a 2000 px long edge.

### Not applied (your call)

- **A cheaper Claude model.** `MAGPIE_MODEL=claude-sonnet-5` is $2 / $10 per million tokens, **60% below Opus 5**, which would make a typical batched screenshot roughly **$0.09**. It may be less accurate on hard cases. Watch the *Needs review* rate in the app if you try it. See [MODELS.md](MODELS.md).
- **Lower confidence threshold.** `MAGPIE_ESCALATE_BELOW=60` sends fewer screenshots to Claude and accepts more local answers as they are.

## Before and after

| | Real time, no controls | Defaults (batch + medium + fetch cap) |
|---|---|---|
| Easy | ~$0.12 | ~$0.06 |
| Typical | ~$0.45 | ~$0.21 |
| Hard | ~$1.50+ (uncapped pages) | ~$0.70 |
| + hybrid, ~25% escalated | – | **~$0.05 average** |

## Local and OCR costs

- **OCR (RapidOCR):** ~1 s of CPU per screenshot. Effectively free.
- **Local LLM:** only the power the machine uses. A GPU box drawing 350 W at $0.20/kWh costs **$0.07/hour**. At ~5–10 s per screenshot on a GPU, that is **~$0.0002** per screenshot. On CPU it takes 1–3 minutes, still well under $0.01. Set `MAGPIE_LOCAL_COST_PER_HOUR=0.07` (your number) to include it in the usage report.
- **Hardware:** a used 12–24 GB GPU is a one-off cost. At ~$0.21 per Claude screenshot, a $600 GPU pays for itself after roughly 3,000 screenshots it keeps away from Claude.

## Measuring real costs

Magpie records every analyzer run: model, mode (realtime, batch or local), input, output and cache tokens, web searches and fetches, duration, and cost at list prices. This includes runs that failed and screenshots you later deleted.

- **Web app / PWA:** click the budget pill in the top bar. Shows total, per screenshot, the share sent to paid models, what answering locally saved, the month's budget with a projection, spend per day by model, what's left per provider, and breakdowns by model, item type and most expensive items. **Export CSV** gives one row per model call. Each item's details show what it cost and how it was identified (`ocr → local:qwen3-vl:8b → claude`).
- **iOS app:** Settings → *Usage & cost*, plus the per-item cost in its details.
- **API:** `GET /api/usage?days=30`

```json
{
  "per_screenshot_usd": 0.052,
  "claude_share": 0.24,
  "projected_30d_usd": 15.6,
  "totals": { "screenshots": 300, "cost_usd": 15.6, "web_searches": 210, "...": "..." },
  "by_analyzer": [ { "analyzer": "claude", "mode": "batch", "avg_cost_usd": 0.2, "...": "..." }, "..." ]
}
```

Costs are computed from the `usage` the API returns on every response, for every provider: Claude, OpenAI, Gemini, OpenRouter and Groq (their runs have mode `hosted`). `cloud_share` (also sent as `claude_share` for older clients) is the share of screenshots sent to any paid provider. Prices live in `magpie/usage.py`; a hosted model that isn't listed is counted as $0 until you add its price. If you have negotiated prices, or a model isn't listed, override them with `MAGPIE_PRICING='{"claude-opus-5": [5, 25]}'` (USD per million input and output tokens).

### Checking the estimate

After some real use, compare `by_analyzer[claude].input_tokens / runs` with the scenarios above:

- **Much higher than ~60k:** Claude is searching and fetching more than assumed. Lower `MAGPIE_FETCH_MAX_TOKENS`, or check whether one category (obscure recipes, say) drives it.
- **`claude_share` above ~30% in hybrid mode:** the local model is often unsure. Try a larger local model (see [MODELS.md](MODELS.md)), or lower `MAGPIE_ESCALATE_BELOW` if its answers turn out right anyway (check how often you use **Fix it**).

## Rate limits and quotas

Every provider limits how much you can send, and they report it differently. Magpie tracks it **per provider and model**:

- **Claude**: the `anthropic-ratelimit-*` headers (requests, tokens, input and output tokens) with their reset times.
- **OpenAI, Groq, OpenRouter**: the `x-ratelimit-*` headers. Groq's request limit is per day and its token limit per minute. OpenRouter also reports the key's credit.
- **Gemini** reports nothing, so Magpie estimates what's left from Google's published free-tier limits (requests per minute and per day; the day resets at midnight Pacific) and the requests it made. On a paid tier set your own with `MAGPIE_RATE_LIMITS`.

When a model hits a limit (HTTP 429, Gemini's `RESOURCE_EXHAUSTED`, or OpenAI's `insufficient_quota`), Magpie pauses that model until the time the provider gives: `retry-after`, Gemini's `retryDelay`, the reset of the exhausted limit, or the next quota day. A pause of a few seconds is waited out. A longer one fails fast without calling the provider, and the screenshot is **retried automatically** when the model is available again. Out of credit doesn't reset by itself, so Magpie tries again every 15 minutes. In hybrid mode, a paused fallback model means the local model's answer is kept instead.

The web app shows a red **⏳ Gemini limit · back in 25 min** pill in the header while a model in use is paused, and an amber one when less than 10% of a limit is left. **Usage & cost → What's left** shows each model's limits, with bars and reset times.

## Gemini web search

Gemini models search the web through Google Search grounding (Settings → Other AI providers → "Gemini: search the web", on by default),
so they find the repository, the article's own page and related links themselves, as Claude does. Google bills the search queries
(Gemini 3: about $14 per 1,000; Magpie counts $0.014 per query, and a screenshot typically uses 1–3). Turn it off to have Gemini answer
from the screenshot alone and let Magpie look things up afterwards. A model that doesn't accept the search tool is used without it.
