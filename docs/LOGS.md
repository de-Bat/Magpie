# Conversation logs

Magpie can save every conversation with an AI model, for debriefing and inspection: what it sent, what came back, how long
it took, what it cost, and what it decided to do with the answer. **Settings → Logging** (or `MAGPIE_LOG_CONVERSATIONS=true`).
It is off by default, because the logs contain what is in your screenshots. API keys and request headers are never written.

## What is saved

One file per **operation** on one item, named by the UTC start time, the operation and the item:

```
<data dir>/logs/2026-10-02/153045_analyze_ab12cd34.jsonl
                           │      │       └ first 8 characters of the item id
                           │      └ analyze | reanalyze | correct | bulk | batch_result
                           └ HHMMSS (UTC)
```

| Operation | When |
|---|---|
| `analyze` | a new screenshot or link is identified |
| `reanalyze` | you re-analyzed an item |
| `correct` | you corrected an item and the model looked again |
| `bulk` | Settings → Library → Re-analyze all |
| `batch_result` | the answer of a Claude batch arrived (the request itself is in the `analyze` file as `batch_queued`) |

Each line of a file is one event, written as it happens, so a crash keeps what was said up to then:

| Event | Contents |
|---|---|
| `session_start` | operation, item, how the analyzer was configured (models, hybrid threshold), the item's previous answer |
| `ocr` | the text the OCR pre-pass read |
| `llm_request` | provider, model, mode (`realtime`, `hosted`, `local`), the **system prompt**, the **messages**, parameters. Images become their type, size and hash |
| `llm_response` | HTTP status, time, the reply (text, tool call, thinking), tokens, rate-limit headers, errors |
| `decision` | a choice Magpie made: escalated to the fallback model, kept the local answer, repaired a link, dropped dead links |
| `analysis` / `enrichment` / `result` | what the model said, what the lookups (GitHub, TMDB, the article page…) found, and what was stored |
| `session_end` | outcome (`ok`, `error`, `rate_limited`, `queued_for_batch`), error, duration, number of calls, cost |

Every retry is its own `llm_request` / `llm_response` pair (`attempt` 1, 2, …), so you can see a 429 and what followed.

## Settings

| Setting | Default | |
|---|---|---|
| `MAGPIE_LOG_CONVERSATIONS` | off | turn logging on |
| `MAGPIE_LOG_IMAGES` | off | also save the screenshot that was sent (after downscaling) next to each log, as `<name>.1.png` |
| `MAGPIE_LOG_RETENTION_DAYS` | 30 | delete day folders older than this; `0` keeps them until you delete them |

A single session stops at 8 MB, and a text field longer than 60,000 characters is cut with a note.

## Looking at them

**Settings → Logging** lists the sessions (filter by operation), opens one as a collapsible conversation, downloads a readable
transcript (`.md`) or the raw `.jsonl`, and deletes one or all. The files are plain text, so you can also read them from the
data directory, or:

```
jq -c 'select(.event=="llm_response") | {t, model, status, duration_ms}' logs/2026-10-02/*.jsonl
```

API (same access as settings: your access token, or the setup code while there is none): `GET /api/logs?operation=&item=&outcome=`,
`GET /api/logs/<day>/<name>` (`?format=md|jsonl`), `GET /api/logs/<day>/<name>/image/<file>`, `DELETE /api/logs/<day>/<name>`,
`DELETE /api/logs`.
