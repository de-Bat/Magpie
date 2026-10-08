"""Screenshot -> analysis -> enrichment -> stored item."""

import json
import logging
import mimetypes
import re
from datetime import timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import anthropic
import httpx

from .analyzer import AnalysisError, RateLimited
from .analyzers import AnalyzerRouter, Deferred
from .usage import Run, claude_cost
from . import convlog, links, readability
from .findlink import is_aggregator, repair_link
from .related import clean_related, drop_dead, same_site, text_related
from .images import best_image, picture_is_opaque
from .enrich import fetch_page, link_is_gone
from .catalog import entry_analysis
from .config import Settings
from .db import Database, normalize_tag
from .enrich import Enrichment, run_enrichers

log = logging.getLogger(__name__)


LOOSE_CATEGORIES = {None, "", "other", "article", "app", "product"}


def reclassify(analysis: dict) -> dict:
    """The item's own address is a GitHub repository, so it is one, whatever the model filed it under ("other", an
    "article" about it, an "app"). The category decides which lookups run and how the card looks."""
    repo = links.github_repo_of(analysis.get("canonical_url"))
    if analysis.get("category") not in LOOSE_CATEGORIES:
        return analysis
    named = (analysis.get("details") or {}).get("github_full_name")
    if not repo and named and re.fullmatch(r"[\w.-]+/[\w.-]+", named) and not analysis.get("canonical_url"):
        repo, analysis = named, {**analysis, "canonical_url": f"https://github.com/{named}"}   # named a repository, gave no address
    if not repo:
        return analysis
    convlog.event("decision", what="category_from_link", was=analysis.get("category"), now="github_repo", repo=repo)
    details = {**(analysis.get("details") or {}), "github_full_name": (analysis.get("details") or {}).get("github_full_name") or repo}
    return {**analysis, "category": "github_repo", "details": details}


def card_snapshot(item: dict) -> dict:
    """The card as it came out: everything the app shows for it, without the bulky raw fields."""
    heavy = {"analysis", "image_file"}
    card = {k: v for k, v in item.items() if k not in heavy}
    card["metadata"] = {k: v for k, v in (item.get("metadata") or {}).items() if k not in ("article_text", "ocr_text", "screenshot_text")}
    return card


def jsonable_error(result: Any) -> Any:
    """Why a batch request didn't succeed, for the log."""
    return None if getattr(result, "type", None) == "succeeded" else convlog.jsonable(getattr(result, "error", None) or getattr(result, "type", None))


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


LEGACY_CONFIDENCE = {"high": 90, "medium": 70, "low": 40}
CORRECTABLE = ("title", "category", "year", "canonical_url")


def confidence_score(value: Any) -> int | None:
    if isinstance(value, str):
        return LEGACY_CONFIDENCE.get(value.lower())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, min(100, int(round(value))))
    return None


def titles_match(a: str | None, b: str | None) -> bool:
    """Loose title equality that tolerates OCR artefacts (dropped spaces, punctuation, case)."""
    if not a or not b:
        return False
    norm = lambda s: re.sub(r"[^0-9a-z]", "", s.lower())  # noqa: E731
    a, b = norm(a), norm(b)
    return bool(a) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.9)


SOURCE_NAMES = {"github": "GitHub", "tmdb": "TMDB", "omdb": "OMDb", "tmdb+omdb": "TMDB", "openlibrary": "Open Library",
                "npm": "npm", "huggingface": "Hugging Face", "schema.org/Recipe": "the recipe page",
                "opengraph": "the page itself", "opengraph+readability": "the page itself"}


def source_confirms(title: str | None, matched: str | None) -> bool:
    """Does what a source found carry the name the model gave? Also when the model named a repository without its
    owner ("uv" for astral-sh/uv), or a page title carries the site's name ("Headline | XDA")."""
    if titles_match(title, matched):
        return True
    if matched and "/" in matched and titles_match((title or "").split("/")[-1], matched.split("/")[-1]):
        return True
    head = re.split(r"\s[|–—-]\s", matched or "")[0]
    return bool(head) and head != matched and titles_match(title, head)


def merge(analysis: dict, enrichments: list[Enrichment]) -> dict:
    """Combine Claude's identification with enrichment results into item fields.

    Enrichers come from authoritative sources (GitHub, TMDB, the recipe page itself),
    so their facts win over the model's; the model's summary and tags are kept.
    """
    details = analysis.get("details") or {}
    metadata: dict[str, Any] = {k: v for k, v in details.items() if not _empty(v)}
    for key in ("imdb_rating", "rotten_tomatoes"):   # models sometimes answer with a number instead of text
        if isinstance(metadata.get(key), (int, float)) and not isinstance(metadata[key], bool):
            metadata[key] = f"{metadata[key]}/10" if key == "imdb_rating" else f"{metadata[key]}%"
    for key in ("year", "screenshot_text"):
        if not _empty(analysis.get(key)):
            metadata[key] = analysis[key]

    canonical_url = analysis.get("canonical_url")
    image_url = analysis.get("image_url")
    subtitle = analysis.get("subtitle")
    summary = analysis.get("summary") or None
    links = list(analysis.get("links") or [])
    related = list(analysis.get("related") or [])
    tags = list(analysis.get("tags") or [])
    sources = ["claude"]
    image_kind = None

    for e in enrichments:
        metadata.update({k: v for k, v in e.metadata.items() if not _empty(v)})
        canonical_url = e.canonical_url or canonical_url
        if e.image_url:
            if e.image_kind == "logo" and image_url and image_kind != "logo" and not (e.source or "").startswith("github") and analysis.get("category") != "github_repo":
                pass  # Keep the specific item picture from analysis over a generic site logo
            else:
                image_url, image_kind = e.image_url, e.image_kind
        subtitle = subtitle or e.subtitle
        if e.subtitle and e.subtitle != subtitle:
            metadata.setdefault("description", e.subtitle)
        if e.summary:
            if summary:
                metadata["description"] = e.summary
            else:
                summary = e.summary  # e.g. after a manual correction, when there is no model summary
        links.extend(e.links)
        related.extend(e.related)
        tags.extend(e.tags)
        if e.source:
            sources.append(e.source)

    if details.get("post_url"):
        links.insert(0, {"label": "Original post", "url": details["post_url"]})
    seen, unique_links = {canonical_url}, []
    for link in links:
        url = (link or {}).get("url")
        if url and url.startswith("http") and url not in seen:
            seen.add(url)
            unique_links.append({"label": link.get("label") or url, "url": url})
    metadata.pop("image_kind", None)
    if image_kind:
        metadata["image_kind"] = image_kind   # the card shows a logo whole on the themed cover instead of cropping it
    # links written in the capture itself: the tool's site, its repository or package, a paper
    written = text_related("\n".join(filter(None, [analysis.get("screenshot_text"), analysis.get("_ocr_text")])))
    related += [r for r in written if not (r["kind"] == "site" and canonical_url and same_site(r["url"], canonical_url))]
    used = analysis.get("_analyzer") or ["claude"]
    metadata["sources"] = used + sources[1:]
    if analysis.get("_ocr_text"):
        metadata["ocr_text"] = analysis["_ocr_text"][:5000]  # full OCR text: searchable even if identification fails

    confidence = confidence_score(analysis.get("confidence"))
    confidence_reason = analysis.get("confidence_reason") or None
    # Claude verifies with web search itself; other analyzers get a second opinion from the
    # authoritative source the enrichers matched (GitHub, TMDB, Open Library, the recipe page).
    if "claude" not in used and confidence is not None and confidence < 90:
        match = next((e for e in enrichments if e.matched_title and source_confirms(analysis.get("title"), e.matched_title)), None)
        if match:
            confidence = max(confidence, 85)
            confidence_reason = f"{confidence_reason or ''} Confirmed by {SOURCE_NAMES.get(match.source, match.source)}.".strip()

    alternatives = [
        {k: a.get(k) for k in ("title", "category", "year", "canonical_url", "why")}
        for a in analysis.get("alternatives") or [] if isinstance(a, dict) and a.get("title")
    ][:3]

    return {
        "category": analysis.get("category") or "other",
        "source_platform": analysis.get("source_platform"),
        "title": analysis.get("title"),
        "subtitle": subtitle,
        "summary": summary,
        "canonical_url": canonical_url,
        "image_url": image_url,
        "metadata": metadata,
        "links": unique_links,
        # worth a look, but not what the item already links to (its own page, source links)
        "related": clean_related(related, exclude=[canonical_url, analysis.get("canonical_url"), *[l["url"] for l in unique_links]]),
        "tags": tags,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "alternatives": alternatives,
    }


def corrected_analysis(previous: dict, correction: dict) -> dict:
    """Rebuild an analysis from the user's corrected facts, without asking the model again.

    Anything that described the wrongly identified thing (details, poster, links, summary)
    is dropped so the enrichers can look up the right one; facts about the post itself stay.
    """
    fixed = {k: correction[k] for k in CORRECTABLE if correction.get(k) not in (None, "")}
    same_thing = all(fixed.get(k, previous.get(k)) == previous.get(k) for k in ("title", "category"))
    old_details = previous.get("details") or {}
    return {
        "category": fixed.get("category") or previous.get("category") or "other",
        "source_platform": previous.get("source_platform"),
        "title": fixed.get("title") or previous.get("title"),
        "year": fixed.get("year", previous.get("year") if same_thing else None),
        "canonical_url": fixed.get("canonical_url") or (previous.get("canonical_url") if same_thing else None),
        "subtitle": previous.get("subtitle") if same_thing else None,
        "summary": previous.get("summary") if same_thing else None,
        "image_url": None,
        "links": [],
        "related": [],
        "tags": list(previous.get("tags") or []) if same_thing else [],
        "screenshot_text": previous.get("screenshot_text"),
        "details": {k: old_details.get(k) for k in ("posted_by", "post_url") if old_details.get(k)},
        "confidence": 100,
        "confidence_reason": "Corrected by you.",
        "alternatives": [],
        **({"_tmdb": previous["_tmdb"]} if same_thing and previous.get("_tmdb") else {}),
    }


class Pipeline:
    def __init__(self, db: Database, settings: Settings, analyzer: Any, http: httpx.AsyncClient):
        self.db, self.settings, self.analyzer, self.http = db, settings, analyzer, http

    async def process(self, item_id: str, correction: dict | None = None, purpose: str = "analyze") -> dict | None:
        """Identify the screenshot (optionally guided by a user's correction)."""
        item = self.db.get_item(item_id)
        if not item:
            return None
        if item.get("kind") == "url":
            return await self.process_url(item_id, correction, purpose)
        if item.get("kind") == "entry":
            return await self.process_entry(item_id, correction, purpose)
        path = self.settings.uploads_dir / item["image_file"]
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        context = None
        if correction:
            context = {**correction, "previous_title": item.get("title"), "previous_category": item.get("category")}
        kwargs: dict[str, Any] = {"note": item.get("note"), "correction": context}
        if isinstance(self.analyzer, AnalyzerRouter):
            # The user is waiting on corrections and re-analyses: never send those to a batch.
            kwargs["interactive"] = purpose not in ("analyze", "bulk")

        async def identify() -> dict:
            analysis = await self.analyzer.analyze(Path(path).read_bytes(), media_type, **kwargs)
            if correction:
                # The user's explicit facts beat the model's.
                explicit = {k: correction[k] for k in CORRECTABLE if correction.get(k) not in (None, "")}
                analysis.update(explicit)
                if explicit:
                    analysis["confidence"], analysis["confidence_reason"] = 100, "Corrected by you."
            return analysis

        return await self._run(item_id, purpose, identify(), corrected=bool(correction))

    async def process_entry(self, item_id: str, correction: dict | None = None, purpose: str = "add") -> dict | None:
        """Something the user added by name (a movie, TV show or book): no screenshot to read, just the metadata lookups."""
        item = self.db.get_item(item_id)
        if not item:
            return None
        previous = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        spec = {**previous, **{k: v for k, v in (correction or {}).items() if k in CORRECTABLE and v not in (None, "")}}
        spec["author"] = (previous.get("details") or {}).get("author")
        spec["imdb_id"] = (previous.get("details") or {}).get("imdb_id")
        spec["tmdb"] = previous.get("_tmdb") if not correction or not {"title", "category"} & set(correction) else None
        spec["category"] = spec.get("category") or item.get("category") or "other"
        spec["title"] = spec.get("title") or item.get("title") or ""

        async def lookup() -> dict:
            return entry_analysis(spec)

        return await self._run(item_id, purpose, lookup(), corrected=True)

    async def process_url(self, item_id: str, correction: dict | None = None, purpose: str = "analyze") -> dict | None:
        """Identify a shared link: by URL pattern / structured data if possible (no model, free),
        otherwise from the page's reader-view text with the configured model, otherwise a generic card."""
        item = self.db.get_item(item_id)
        if not item:
            return None
        url = item["source_url"]
        page = await fetch_page(url, self.http)
        article = readability.extract(page.html, page.url) if page else None
        known = None if correction else links.classify(url, page)
        router = self.analyzer if isinstance(self.analyzer, AnalyzerRouter) else None

        async def identify() -> dict:
            if known:
                analysis = known
            elif router and router.mode != "ocr":
                context = None
                if correction:
                    context = {**correction, "previous_title": item.get("title"), "previous_category": item.get("category")}
                readable = bool(article and article.word_count >= 150)
                analysis = await router.analyze(
                    None, None, note=item.get("note"), correction=context, interactive=purpose not in ("analyze", "bulk"),
                    link_url=url, page_hints=links.page_context(url, page, article), web=not readable,
                )
                analysis["_analyzer"] = ["link", "readability"] + analysis.get("_analyzer", [])
            else:
                analysis = links.generic(url, page, article, item.get("note"))
            return self._finish_link(url, analysis, article, correction)

        return await self._run(item_id, purpose, identify(), corrected=bool(correction))

    def _finish_link(self, url: str, analysis: dict, article, correction: dict | None) -> dict:
        analysis.setdefault("source_platform", links.platform_of(url))
        if not analysis.get("canonical_url"):
            analysis["canonical_url"] = url
        if analysis["canonical_url"] != url:
            analysis.setdefault("links", []).insert(0, {"label": "Shared link", "url": url})
        if article and article.text and not analysis.get("screenshot_text"):
            analysis["_ocr_text"] = article.text  # searchable, like a screenshot's OCR text
        if correction:
            explicit = {k: correction[k] for k in CORRECTABLE if correction.get(k) not in (None, "")}
            analysis.update(explicit)
            if explicit:
                analysis["confidence"], analysis["confidence_reason"] = 100, "Corrected by you."
        return analysis

    async def correct(self, item_id: str, correction: dict) -> dict | None:
        """Apply a user's correction. With a free-text hint the model looks again; otherwise the
        corrected facts are used as-is and only the metadata lookups run."""
        if correction.get("hint"):
            return await self.process(item_id, correction, purpose="correct")
        item = self.db.get_item(item_id)
        if not item:
            return None
        previous = item.get("analysis") if isinstance(item.get("analysis"), dict) else {
            k: item.get(k) for k in ("title", "category", "canonical_url", "subtitle", "summary", "source_platform")
        }

        async def fixed() -> dict:
            return corrected_analysis(previous, correction)

        return await self._run(item_id, "correct", fixed(), corrected=True)

    async def _mark_opaque(self, metadata: dict, image_url: str | None) -> None:
        """Record whether the cover picture is fully opaque (the card then adds a blurred backdrop behind it)."""
        metadata.pop("image_opaque", None)
        if image_url and self.settings.enrich:
            opaque = await picture_is_opaque(self.http, image_url)
            if opaque is not None:
                metadata["image_opaque"] = opaque

    async def refresh_metadata(self, item_id: str) -> dict | None:
        """Look the item up again in the metadata sources (posters, covers, ratings, links) without asking a
        model anything: free, and nothing the user edited or the model decided (title, category, tags,
        confidence) changes. Fields a source doesn't return this time keep their current value."""
        item = self.db.get_item(item_id)
        if not item:
            return None
        stored = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        candidate_image = item.get("image_url") or stored.get("image_url")
        # Look up what the item is now, including the user's edits, not what the model first said.
        analysis = {
            **stored,
            "category": item.get("category"),
            "title": item.get("title"),
            "year": stored.get("year"),
            "canonical_url": item.get("canonical_url"),
            "image_url": candidate_image,
            "links": list(item.get("links") or stored.get("links") or []),
            "tags": [],
            "details": {
                **(stored.get("details") or {}),
                **{k: v for k, v in (item.get("metadata") or {}).items()
                   if k in ("imdb_id", "github_full_name", "author", "isbn", "publisher")},
            },
        }
        skip_repair = item.get("kind") == "url" and not is_aggregator(item.get("canonical_url"))
        repaired = await repair_link({**analysis, "_analyzer": ["link"] if skip_repair else []}, self.http) \
            if self.settings.enrich else analysis
        analysis = {**analysis, **repaired}
        canonical_after_repair = analysis.get("canonical_url")
        enrichments = await run_enrichers(analysis, self.settings, self.http)
        fresh = merge(analysis, enrichments)
        from_page = any((e.source or "").startswith("opengraph") for e in enrichments)

        links, seen = list(item.get("links") or []), {l.get("url") for l in item.get("links") or []}
        for l in (repaired.get("links") or []) + (fresh.get("links") or []):
            if l.get("url") and l["url"] not in seen:
                seen.add(l["url"])
                links.append(l)

        existing_img = item.get("image_url") or stored.get("image_url")
        existing_kind = (item.get("metadata") or {}).get("image_kind") or (stored.get("metadata") or {}).get("image_kind")
        fresh_img = fresh.get("image_url")
        fresh_kind = fresh.get("metadata", {}).get("image_kind")

        # Use fresh image if available, unless it's just a site's favicon logo and we already have a real image.
        # A repository's own logo is the picture of it (merge() makes the same exception), and replaces GitHub's generated card.
        if fresh_img and (fresh_kind != "logo" or existing_kind == "logo" or not existing_img
                         or analysis.get("category") == "github_repo" or any((e.source or "").startswith("github") for e in enrichments)):
            new_image = fresh_img
            new_kind = fresh_kind
        else:
            new_image = existing_img or fresh_img
            new_kind = existing_kind if new_image == existing_img else fresh_kind

        page_canonical = fresh.get("canonical_url") if (from_page and not is_aggregator(fresh.get("canonical_url"))) else None
        target_url = page_canonical or canonical_after_repair or fresh.get("canonical_url") or analysis.get("canonical_url")

        if item.get("corrected") and not is_aggregator(item.get("canonical_url")):
            canonical_url = item.get("canonical_url")
        else:
            canonical_url = target_url

        changes = {
            "image_url": new_image,
            "metadata": {**(item.get("metadata") or {}), **{k: v for k, v in fresh["metadata"].items() if k != "screenshot_text"}},
            "links": links,
            "related": clean_related([*(item.get("related") or []), *fresh["related"]],
                                     exclude=[canonical_url, *[l.get("url") for l in links]]),
            "subtitle": item.get("subtitle") or fresh["subtitle"],
            "summary": item.get("summary") or fresh["summary"],
            "canonical_url": canonical_url,
            "analysis": {
                **stored,
                **analysis,
                **fresh,
                "canonical_url": canonical_url,
                "image_url": new_image,
                "links": links,
                "details": {**(stored.get("details") or {}), **(analysis.get("details") or {})},
            },
        }
        if new_kind:
            changes["metadata"]["image_kind"] = new_kind
        else:
            changes["metadata"].pop("image_kind", None)
        await self._mark_opaque(changes["metadata"], changes["image_url"])
        return self.db.update_item(item_id, **changes)

    async def complete_batch_job(self, job: dict, result: Any) -> dict | None:
        """Finish an analysis whose Claude step ran in a Message Batch."""
        params, context = json.loads(job["params"]), json.loads(job["context"])
        router: AnalyzerRouter = self.analyzer

        async def finish() -> dict:
            if getattr(result, "type", None) == "succeeded":
                return await router.finish_batch(params, result.message, context)
            # errored / expired / canceled: run it now at the normal price rather than leave it stuck
            log.warning("Batch request for %s ended as %s; running it in real time", job["item_id"], getattr(result, "type", "?"))
            return router.finish(await router.claude.run(params), context)

        if not self.db.get_item(job["item_id"]):
            # Deleted while queued: still account for what the batch cost.
            if getattr(result, "type", None) == "succeeded":
                self.db.record_runs(job["item_id"], [self._batch_only_run(result.message)], job["purpose"])
            return None
        with convlog.session("batch_result", job["item_id"], requested_as=job["purpose"], batch_id=job.get("batch_id")):
            msg = getattr(result, "message", None)
            convlog.log_response("claude", params.get("model"), mode="batch", batch_result=getattr(result, "type", None),
                                 model_used=getattr(msg, "model", None), stop_reason=getattr(msg, "stop_reason", None),
                                 content=getattr(msg, "content", None), usage=getattr(msg, "usage", None),
                                 error=jsonable_error(result))
            return await self._run(job["item_id"], job["purpose"], finish(), corrected=job["purpose"] == "correct")

    def _batch_only_run(self, message: Any) -> dict:
        run = Run("claude", model=getattr(message, "model", ""), mode="batch")
        run.add_claude_usage(getattr(message, "usage", None))
        run.cost_usd = claude_cost(run)
        return run.to_dict()

    async def apply_analysis(self, item_id: str, analysis: dict, corrected: bool = False) -> dict | None:
        if not corrected:   # what you set yourself is never second-guessed
            analysis = reclassify(analysis)
        if self.settings.enrich:
            before = analysis.get("canonical_url")
            analysis = await repair_link(analysis, self.http)   # a made-up address becomes the real article's, or none
            if analysis.get("canonical_url") != before:
                convlog.event("decision", what="link_repaired", model_gave=before, now=analysis.get("canonical_url"))
            offered = clean_related(analysis.get("related"))
            analysis = {**analysis, "related": await drop_dead(offered, self.http, fetch_page, link_is_gone)}
            if len(analysis["related"]) != len(offered):
                convlog.event("decision", what="dead_related_links_dropped", dropped=[r["url"] for r in offered if r not in analysis["related"]])
        enrichments = await run_enrichers(analysis, self.settings, self.http)
        convlog.event("enrichment", sources=[e.source for e in enrichments], matched=[e.matched_title for e in enrichments if e.matched_title],
                      canonical_url=[e.canonical_url for e in enrichments if e.canonical_url], images=[e.image_url for e in enrichments if e.image_url])
        fields = merge(analysis, enrichments)
        if fields["image_url"] and (not any(e.image_url for e in enrichments) or fields["image_url"] == analysis.get("image_url")):
            # If the model suggested this image, verify it isn't dead; otherwise fall back to any enrichment image found.
            verified = await best_image(self.http, [fields["image_url"]])
            if verified:
                fields["image_url"] = verified
            else:
                enrichment_img = next((e.image_url for e in enrichments if e.image_url), None)
                enrichment_kind = next((e.image_kind for e in enrichments if e.image_url), None)
                fields["image_url"] = enrichment_img
                if enrichment_kind:
                    fields["metadata"]["image_kind"] = enrichment_kind
                else:
                    fields["metadata"].pop("image_kind", None)
        await self._mark_opaque(fields["metadata"], fields["image_url"])

        # Replace the tags generated for the previous identification, keep the user's own.
        item = self.db.get_item(item_id) or {}
        previous = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        stale = {normalize_tag(t) for t in previous.get("_auto_tags") or previous.get("tags") or []}
        auto_tags = sorted({t for t in (normalize_tag(t) for t in fields.pop("tags")) if t})
        analysis = {**analysis, "_auto_tags": auto_tags}
        self.db.set_tags(item_id, [t for t in item.get("tags", []) if t not in stale] + auto_tags)

        url = fields.get("canonical_url")
        first_time = not previous and not corrected and item.get("kind") != "url" and item.get("image_file")
        twin = self.db.find_by_canonical_url(url) if url and first_time else None
        if twin and twin["id"] != item_id:
            convlog.event("decision", what="merged_into_existing_card", into=twin["id"], canonical_url=url)
            merged = self.db.merge_capture(item_id, twin["id"])
            return {**merged, "duplicate": True, "merged_item_id": item_id}

        return self.db.update_item(
            item_id, **fields, analysis=analysis, status="ready", error=None, confirmed=0,
            corrected=int(corrected or bool(item.get("corrected"))),
        )

    async def _run(self, item_id: str, purpose: str, work, corrected: bool) -> dict | None:
        """Run an identification, record what it cost, and store the result or the error. When conversation logging
        is on, everything said to the models meanwhile goes into one log file for this operation."""
        item = self.db.get_item(item_id) or {}
        s = self.settings
        with convlog.session(purpose, item_id, item_title=item.get("title"), item_kind=item.get("kind"), corrected=corrected,
                             note=item.get("note"), analyzer=s.resolved_analyzer(), claude_model=s.model,
                             hosted_provider=s.hosted_llm if s.hosted_llm != "none" else None, hosted_model=s.llm_model,
                             local_model=s.local_llm_model if s.local_llm_url else None, escalate_below=s.escalate_below,
                             claude_batch=s.claude_batch, previous={k: item.get(k) for k in ("title", "category", "canonical_url", "confidence")},
                             input={"kind": item.get("kind") or "screenshot", "source_url": item.get("source_url"), "note": item.get("note")},
                             input_image=(s.uploads_dir / item["image_file"]) if item.get("image_file") and item.get("kind") != "url" else None):
            return await self._run_logged(item_id, purpose, work, corrected)

    async def _run_logged(self, item_id: str, purpose: str, work, corrected: bool) -> dict | None:
        def record(runs: list[dict]) -> None:
            self.db.record_runs(item_id, runs, purpose)
            cost = sum(r.get("cost_usd") or 0 for r in runs)
            if cost and convlog.current():
                convlog.current().cost += cost

        try:
            analysis = await work
        except Deferred as d:
            record(d.context.get("runs", []))  # OCR / local runs so far
            d.context["runs"] = []
            self.db.add_batch_job(item_id, purpose, d.params, d.context)
            convlog.event("batch_queued", note="The Claude request was queued for the Message Batches API; its answer is logged as a batch_result session.",
                          model=d.params.get("model"), system=d.params.get("system"), messages=d.params.get("messages"),
                          parameters={k: v for k, v in d.params.items() if k not in ("system", "messages")})
            convlog.set_outcome(outcome="queued_for_batch")
            return self.db.get_item(item_id)
        except RateLimited as e:
            record(e.runs)
            convlog.set_outcome(outcome="rate_limited", error=str(e), retry_at=e.until.isoformat() if e.until else None)
            return self.db.update_item(item_id, status="error", error=str(e),
                                       retry_at=e.until.astimezone(timezone.utc).isoformat(timespec="microseconds") if e.until else None)
        except AnalysisError as e:
            record(e.runs)
            convlog.set_outcome(outcome="error", error=str(e))
            return self.db.update_item(item_id, status="error", error=str(e))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            record(getattr(e, "magpie_runs", []))
            convlog.set_outcome(outcome="error", error=f"{type(e).__name__}: {e}")
            return self.db.update_item(item_id, status="error", error=(
                "The server's Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY and re-analyze."
            ))
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            record(getattr(e, "magpie_runs", []))
            convlog.set_outcome(outcome="error", error=f"{type(e).__name__}: {e}")
            return self.db.update_item(item_id, status="error", error=f"Temporary problem reaching Claude ({type(e).__name__}). Try re-analyzing.")
        except Exception as e:  # keep the item; the user can retry
            log.exception("Processing %s failed", item_id)
            record(getattr(e, "magpie_runs", []))
            convlog.set_outcome(outcome="error", error=f"{type(e).__name__}: {e}")
            return self.db.update_item(item_id, status="error", error=f"{type(e).__name__}: {e}")
        convlog.event("analysis", used=analysis.get("_analyzer"), title=analysis.get("title"), category=analysis.get("category"),
                      confidence=analysis.get("confidence"), confidence_reason=analysis.get("confidence_reason"),
                      canonical_url=analysis.get("canonical_url"), alternatives=analysis.get("alternatives"),
                      related=analysis.get("related"), details=analysis.get("details"), summary=analysis.get("summary"))
        record(analysis.pop("_runs", []))
        try:
            stored = await self.apply_analysis(item_id, analysis, corrected=corrected)
        except Exception as e:
            log.exception("Storing the analysis for %s failed", item_id)
            convlog.set_outcome(outcome="error", error=f"{type(e).__name__}: {e}")
            return self.db.update_item(item_id, status="error", error=f"{type(e).__name__}: {e}")
        if stored:
            convlog.event("card", card=card_snapshot(stored))
            convlog.event("result", title=stored.get("title"), category=stored.get("category"), confidence=stored.get("confidence"),
                          canonical_url=stored.get("canonical_url"), image_url=stored.get("image_url"), status=stored.get("status"),
                          verified=stored.get("verified"), sources=(stored.get("metadata") or {}).get("sources"),
                          related=[r.get("url") for r in stored.get("related") or []])
        return stored
