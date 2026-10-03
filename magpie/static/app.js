"use strict";

// Magpie web app / PWA.
//
// Local-first: the library lives in IndexedDB, so the app opens, browses and searches
// offline (the service worker caches the app itself and images). New screenshots and
// edits are applied locally and queued; the queue is replayed in order whenever the
// self-hosted server is reachable, then changes are pulled with GET /api/sync?since=.

const CATEGORY_LABELS = {
  movie: "🎬 Movie", tv_show: "📺 TV show", github_repo: "💻 GitHub repo", recipe: "🍳 Recipe",
  book: "📚 Book", music: "🎵 Music", podcast: "🎙️ Podcast", video: "▶️ Video", article: "📰 Article",
  product: "🛍️ Product", place: "📍 Place", event: "📅 Event", app: "📱 App", course: "🎓 Course", other: "📌 Other",
};
// Types whose pictures are landscape (repo social cards, page headers): shown whole over the type's cover, not cropped.
const WIDE_CATEGORIES = new Set(["github_repo", "article", "video", "product", "app", "other", "place", "event", "course"]);
const typeName = (c) => (CATEGORY_LABELS[c] || c || "Other").replace(/^\S+ /, "");
const TABS = [
  { id: "all", label: "All" },
  { id: "screen", label: "Movies & TV", cats: ["movie", "tv_show"] },
  { id: "repo", label: "Repos", cats: ["github_repo"] },
  { id: "recipe", label: "Recipes", cats: ["recipe"] },
  { id: "book", label: "Books", cats: ["book"] },
  { id: "music", label: "Music & podcasts", cats: ["music", "podcast"] },
  { id: "read", label: "Articles & videos", cats: ["article", "video"] },
  { id: "place", label: "Places & events", cats: ["place", "event"] },
  { id: "other", label: "Other", cats: ["other", "product", "app", "course"] },
];
// One icon per type, drawn with the same stroke so they read as a set. Shown on every card, whether or not it has a picture.
const TYPE_ICON_PATHS = {
  movie: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  tv_show: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M8 21h8M12 18v3M9 3l3 3 3-3"/>',
  github_repo: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="9" r="2"/><path d="M6 7v10M18 11c0 4-6 3-12 6"/>',
  recipe: '<path d="M5 11h14v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM3 11h18M9 7c0-2 1-3 3-3s3 1 3 3"/>',
  book: '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM5 17a3 3 0 0 1 3-3h11"/>',
  music: '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
  podcast: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9l5 3-5 3z"/>',
  article: '<path d="M6 3h9l4 4v14H6zM14 3v5h5M9 12h6M9 16h6"/>',
  product: '<path d="M5 8h14l-1 12H6zM9 8V6a3 3 0 0 1 6 0v2"/>',
  place: '<path d="M12 21s7-6 7-11a7 7 0 0 0-14 0c0 5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  event: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M8 3v4M16 3v4"/>',
  app: '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
  course: '<path d="M2 9l10-5 10 5-10 5zM6 11v5c0 1.5 3 3 6 3s6-1.5 6-3v-5"/>',
  other: '<path d="M4 4h8l8 8-8 8-8-8z"/><circle cx="8.5" cy="8.5" r="1.2"/>',
};
const typeIcon = (c, cls = "ticon") => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${TYPE_ICON_PATHS[c] || TYPE_ICON_PATHS.other}</svg>`;
// A tab groups related types and gives them one icon and color (its first type), so a card, its tab and the
// detail sheet always match. The chip on the card still names the exact type ("TV show", "Podcast").
const themeOf = (category) => TABS.find((t) => t.cats?.includes(category))?.cats[0] || "other";
const tabOf = (item) => TABS.find((t) => t.cats?.includes(item.category))?.id || "other";
// What each type's detail sheet lists first (anything else Magpie found follows).
const FACT_ORDER = {
  movie: ["release_date", "runtime", "rated", "directors", "cast", "genres", "network_or_studio", "awards", "where_to_watch", "tagline"],
  tv_show: ["first_air_date", "status", "seasons", "episodes", "creators", "cast", "genres", "network_or_studio", "where_to_watch", "tagline", "rated", "awards"],
  github_repo: ["programming_language", "license", "latest_release", "released", "last_push", "created", "open_issues", "forks", "watchers", "topics", "homepage", "archived"],
  recipe: ["total_time", "prep_time", "cook_time", "servings", "cuisine", "calories", "author"],
  book: ["author", "authors", "first_publish_year", "pages", "publisher", "isbn", "subjects"],
  article: ["author", "published", "reading_time", "site"],
};
const TYPE_BLOCK = { movie: "Film", tv_show: "Series", github_repo: "Repository", recipe: "Recipe", book: "Book", music: "Release", podcast: "Show",
  video: "Video", article: "Article", product: "Product", place: "Place", event: "Event", app: "App", course: "Course" };

// Metadata keys shown elsewhere in the detail view (or not useful to show).
const HIDDEN_META = new Set([
  "article_text", "excerpt", "word_count", "page_description",
  "screenshot_text", "sources", "confidence", "ingredients", "instructions", "imdb_rating", "rotten_tomatoes",
  "metacritic", "tmdb_rating", "stars", "rating", "rating_count", "description", "post_url", "imdb_votes", "tmdb_id",
  "page_description", "page_title", "github_full_name", "year", "ocr_text", "image_kind", "image_opaque", "found_by",
]);

const state = {
  q: "", tab: "all", show: "all", tags: [], cols: (() => { try { return Math.max(0, Math.min(8, parseInt(localStorage.getItem("magpie.columns"), 10) || 0)); } catch { return 0; } })(),
  layout: (() => { try { return localStorage.getItem("magpie.layout") === "list" ? "list" : "grid"; } catch { return "grid"; } })(),
  items: new Map(),        // id -> item (mirror of the IndexedDB "items" store)
  ops: [],                 // queued changes, oldest first
  sync: "idle",            // idle | syncing | offline | auth | error
  syncError: null,
  lastSync: null,
  server: null,            // GET /api/status: { status: ok|warning|error, problems: [...] }
};
const blobUrls = new Map(); // id -> object URL for screenshots not uploaded yet

const $ = (sel) => document.querySelector(sel);

// ---- small helpers ---------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function safeUrl(url) {
  if (!url || typeof url !== "string") return null;
  try {
    const u = new URL(url, location.href);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch { return null; }
}
function humanize(key) {
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
function fmtValue(v) {
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") return v >= 10000 ? v.toLocaleString() : String(v);
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return new Date(v).toLocaleDateString();
  return String(v);
}
// A popover lives in the browser's top layer, so a toast shows above an open dialog (Settings) instead of behind it.
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  clearTimeout(toast._t);
  if (t.showPopover) {
    try { if (t.matches(":popover-open")) t.hidePopover(); t.showPopover(); } catch {}
  } else {
    t.hidden = false;
  }
  toast._t = setTimeout(() => {
    if (t.hidePopover) { try { t.hidePopover(); } catch {} } else { t.hidden = true; }
  }, Math.max(3500, String(msg).length * 55));
}
if (!$("#toast").showPopover) $("#toast").hidden = true;
function newId() {
  // crypto.randomUUID needs a secure context; getRandomValues works on plain-http LAN servers too.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return "web-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
function fold(s) {
  return String(s).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
}
function normalizeTag(tag) {
  return tag.trim().toLowerCase().replace(/^#/, "").replace(/\s+/g, "-").replace(/[^\p{L}\p{N}_\-+.]/gu, "").slice(0, 40);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ---- IndexedDB -------------------------------------------------------------

const db = {
  _conn: null,
  open() {
    if (this._conn) return this._conn;
    this._conn = new Promise((resolve, reject) => {
      const req = indexedDB.open("magpie", 1);
      req.onupgradeneeded = () => {
        const d = req.result;
        d.createObjectStore("items", { keyPath: "id" });
        d.createObjectStore("ops", { keyPath: "seq", autoIncrement: true });
        d.createObjectStore("blobs");   // id -> Blob (screenshots waiting to upload)
        d.createObjectStore("kv");      // lastSync, token
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return this._conn;
  },
  async tx(stores, mode, fn) {
    const d = await this.open();
    return new Promise((resolve, reject) => {
      const t = d.transaction(stores, mode);
      let result;
      Promise.resolve(fn(t)).then((r) => (result = r));
      t.oncomplete = () => resolve(result);
      t.onerror = t.onabort = () => reject(t.error);
    });
  },
  all(store) {
    return this.tx([store], "readonly", (t) => new Promise((res) => {
      const r = t.objectStore(store).getAll();
      r.onsuccess = () => res(r.result);
    }));
  },
  get(store, key) {
    return this.tx([store], "readonly", (t) => new Promise((res) => {
      const r = t.objectStore(store).get(key);
      r.onsuccess = () => res(r.result);
    }));
  },
  put(store, value, key) {
    return this.tx([store], "readwrite", (t) => { t.objectStore(store).put(value, key); });
  },
  del(store, key) {
    return this.tx([store], "readwrite", (t) => { t.objectStore(store).delete(key); });
  },
  add(store, value) {
    return this.tx([store], "readwrite", (t) => new Promise((res) => {
      const r = t.objectStore(store).add(value);
      r.onsuccess = () => res(r.result);
    }));
  },
};

// Persistent storage keeps iOS/Safari from evicting the offline library under storage pressure.
navigator.storage?.persist?.().catch(() => {});

// ---- local store -----------------------------------------------------------

async function loadLocal() {
  const [items, ops, lastSync] = await Promise.all([db.all("items"), db.all("ops"), db.get("kv", "lastSync")]);
  state.items = new Map(items.map((i) => [i.id, i]));
  state.ops = ops.sort((a, b) => a.seq - b.seq);
  state.lastSync = lastSync || null;
  for (const item of items) {
    if (item.pending_upload && !blobUrls.has(item.id)) {
      const blob = await db.get("blobs", item.id);
      if (blob) blobUrls.set(item.id, URL.createObjectURL(blob));
    }
  }
}

async function putItem(item) {
  const prev = state.items.get(item.id);
  state.items.set(item.id, item);
  trackItem(prev, item);
  await db.put("items", item);
}

async function removeItem(id, { keepDeleteOp = false } = {}) {
  activityDrop(id);
  state.items.delete(id);
  await db.del("items", id);
  await db.del("blobs", id);
  if (blobUrls.has(id)) { URL.revokeObjectURL(blobUrls.get(id)); blobUrls.delete(id); }
  for (const op of state.ops.filter((o) => o.id === id && !(keepDeleteOp && o.type === "delete"))) await dropOp(op);
}

async function enqueue(op) {
  op.seq = await db.add("ops", op);
  state.ops.push(op);
}

async function dropOp(op) {
  state.ops = state.ops.filter((o) => o.seq !== op.seq);
  await db.del("ops", op.seq);
}

function hasPendingOps(id) { return state.ops.some((o) => o.id === id); }

// Merge a server copy without losing local-only fields.
async function mergeServerItem(server) {
  await putItem({ ...server, pending_upload: false });
  if (blobUrls.has(server.id)) { URL.revokeObjectURL(blobUrls.get(server.id)); blobUrls.delete(server.id); }
  await db.del("blobs", server.id);
}

// ---- activity tray: what is being analyzed / refreshed right now, and how it ended ----------------------------
// A stack at the bottom-left. A running row spins; a finished one shows the outcome, fades after a while (failures stay
// longer, hovering keeps it) and has an × to dismiss it. Item operations are noticed from the item's status changing
// (so work started on another device shows up too); refreshing and library-wide jobs report their own start and end.

const activities = new Map();   // key -> { key, kind, itemId, state: running|done|warn|failed, text, note, progress, startedAt }
const pendingKind = new Map();  // item id -> what the user just asked for (reanalyze / correct), until its row exists
const ACTIVITY_MAX = 5;
const ACTIVITY_FADE = { done: 6000, warn: 12000, failed: 15000 };
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const itemName = (item) => (item?.title ? `“${item.title}”` : item?.kind === "url" ? "link" : "screenshot");
const isBusy = (i) => !!i && (i.status === "queued" || i.status === "processing");
const bulkRunning = () => activities.get("bulk")?.state === "running";

function activityStart(kind, itemId, text, note = "") {
  const key = itemId ? `${kind}:${itemId}` : kind;
  const old = activities.get(key);
  clearTimeout(old?.timer);
  activities.set(key, { key, kind, itemId, state: "running", text, note, progress: null, startedAt: Date.now() });
  renderActivity();
}

function activityUpdate(key, fields) {
  const a = activities.get(key);
  if (!a || a.state !== "running") return;
  Object.assign(a, fields);
  renderActivity();
}

function activityFinish(key, state, text, note = "") {
  const old = activities.get(key);
  clearTimeout(old?.timer);
  const [kind, itemId] = key.split(/:(.*)/s);
  const a = Object.assign(old || { key, kind, itemId: itemId || null, startedAt: Date.now() }, { state, text, note, progress: null, leaving: false });
  activities.set(key, a);
  scheduleFade(a);
  renderActivity();
}

function scheduleFade(a) {
  clearTimeout(a.timer);
  if (document.visibilityState !== "visible") { a.waiting = true; return; }   // not seen yet: the clock starts when you are back
  a.waiting = false;
  a.timer = setTimeout(() => activityDismiss(a.key), ACTIVITY_FADE[a.state] || 6000);
}

function activityDismiss(key) {
  const a = activities.get(key);
  if (!a) return;
  clearTimeout(a.timer);
  if (reducedMotion()) { activities.delete(key); return renderActivity(); }
  a.leaving = true;
  renderActivity();
  setTimeout(() => { if (activities.get(key) === a) { activities.delete(key); renderActivity(); } }, 320);
}

function activityDrop(itemId) {   // the item is gone (deleted, or merged into another card): nothing left to report
  for (const [key, a] of activities) if (a.itemId === itemId) { clearTimeout(a.timer); activities.delete(key); }
  renderActivity();
}

// Called for every change to an item, wherever it came from.
function trackItem(prev, next) {
  const now = isBusy(next), waiting = next.status === "error" && !!next.retry_at;   // an AI limit: it retries by itself
  const open = ["analyze", "reanalyze", "correct"].map((k) => `${k}:${next.id}`).find((k) => activities.get(k)?.state === "running");
  if (now || waiting) {
    const note = next.pending_upload ? "Waiting to upload" : next.batch_pending ? "Queued in a Claude batch (cheaper; can take up to an hour)"
      : waiting ? `The AI limit was reached; trying again ${untilText(next.retry_at)}` : "";
    if (open) return activityUpdate(open, { note, text: activityText(activities.get(open).kind, next) });
    const explicit = pendingKind.get(next.id);
    pendingKind.delete(next.id);
    if (bulkRunning() && !explicit) return;   // a library-wide job has its own row
    const kind = explicit || (prev && !isBusy(prev) ? "reanalyze" : "analyze");
    return activityStart(kind, next.id, activityText(kind, next), note);
  }
  if (!open) return;
  const kind = open.split(":")[0];
  if (next.status === "ready") {
    const unsure = next.needs_review && !next.corrected;
    activityFinish(open, unsure ? "warn" : "done", activityText(kind, next, unsure ? "unsure" : "done"),
      unsure ? "Magpie isn't sure about this one. Open it to check." : "");
  } else {
    activityFinish(open, "failed", activityText(kind, next, "failed"), next.error || "");
  }
}

function activityText(kind, item, outcome = "running") {
  const name = itemName(item);
  const verbs = { analyze: ["Analyzing", "Identified", "identify"], reanalyze: ["Re-analyzing", "Re-analyzed", "re-analyze"],
    correct: ["Applying your correction to", "Updated", "apply the correction to"], refresh: ["Refreshing metadata for", "Refreshed metadata for", "refresh metadata for"] }[kind];
  if (outcome === "running") return `${verbs[0]} ${name}…`;
  if (outcome === "failed") return `Couldn't ${verbs[2]} ${name}`;
  const type = (kind === "analyze" || kind === "reanalyze") && item.category ? ` · ${typeName(item.category)}` : "";
  return `${verbs[1]} ${name}${type}`;
}

// Library-wide refresh / re-analyze: one row with progress.
let bulkWatch = null;
async function trackBulk() {
  clearTimeout(bulkWatch);
  let st;
  try { st = await api("/api/bulk", { timeout: 8000 }); } catch { return; }
  if (st.running) {
    const label = `${BULK_LABEL[st.kind] || "Working"}: ${st.done} of ${st.total}${st.failed ? ` · ${st.failed} failed` : ""}`;
    if (activities.get("bulk")?.state === "running") activityUpdate("bulk", { text: label, progress: st.total ? st.done / st.total : null });
    else { activityStart("bulk", null, label); activityUpdate("bulk", { progress: st.total ? st.done / st.total : null }); }
    bulkWatch = setTimeout(trackBulk, 1500);
  } else if (activities.get("bulk")?.state === "running") {
    const what = st.kind === "refresh" ? "refreshed" : "sent for analysis";
    const text = `${st.cancelled ? "Stopped" : "Done"}: ${st.done} of ${st.total} ${what}`;
    activityFinish("bulk", st.failed ? "warn" : "done", st.failed ? `${text}, ${st.failed} failed` : text,
      st.kind === "reanalyze" ? "Claude batches can take up to an hour; the library updates by itself." : "");
    requestSync();
  }
}

const ACTIVITY_ICON = { done: "✓", warn: "!", failed: "✗" };

function activityRow(a) {
  const el = document.createElement("div");
  el.dataset.key = a.key;
  el.classList.add("enter");
  el.addEventListener("animationend", () => el.classList.remove("enter"));
  el.addEventListener("mouseenter", () => clearTimeout(activities.get(a.key)?.timer));
  el.addEventListener("mouseleave", () => { const cur = activities.get(a.key); if (cur && cur.state !== "running") { clearTimeout(cur.timer); cur.timer = setTimeout(() => activityDismiss(cur.key), 3000); } });
  return el;
}

function fillActivityRow(el, a) {
  const sig = JSON.stringify([a.state, a.text, a.note, a.progress, a.leaving]);
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.className = `act act-${a.state}${a.leaving ? " leaving" : ""}${el.classList.contains("enter") ? " enter" : ""}`;
  const item = a.itemId && state.items.get(a.itemId);
  const finished = a.state !== "running";
  const retry = a.state === "failed" && item && !item.pending_upload && (a.kind === "analyze" || a.kind === "reanalyze");
  const main = `<span class="act-text">${esc(a.text)}</span>${a.note ? `<span class="act-note">${esc(a.note)}</span>` : ""}`;
  el.innerHTML = `<span class="act-icon" aria-hidden="true">${finished ? ACTIVITY_ICON[a.state] : ""}</span>
    ${item ? `<button type="button" class="act-main" data-open-item="${esc(a.itemId)}" title="Open it">${main}</button>` : `<span class="act-main">${main}</span>`}
    ${retry ? `<button type="button" class="act-retry" data-activity-retry="${esc(a.itemId)}">Retry</button>` : ""}
    ${finished ? `<button type="button" class="act-x" data-activity-dismiss="${esc(a.key)}" aria-label="Dismiss">×</button>` : ""}
    ${a.progress != null ? `<i class="act-bar"><i style="width:${Math.round(a.progress * 100)}%"></i></i>` : ""}`;
}

function renderActivity() {
  const tray = $("#activity");
  const rows = [...activities.values()].sort((x, y) => x.startedAt - y.startedAt);
  const shown = rows.slice(-ACTIVITY_MAX), extra = rows.slice(0, rows.length - shown.length);
  const wanted = new Map(shown.map((a) => [a.key, a]));
  for (const el of [...tray.querySelectorAll(".act[data-key]")]) if (!wanted.has(el.dataset.key)) el.remove();
  let more = tray.querySelector(".act-more");
  if (extra.length) {
    const working = extra.filter((a) => a.state === "running").length, failed = extra.filter((a) => a.state === "failed").length;
    if (!more) { more = document.createElement("div"); more.className = "act-more"; tray.prepend(more); }
    more.textContent = `+${extra.length} more${working ? ` · ${working} still working` : ""}${failed ? ` · ${failed} failed` : ""}`;
  } else more?.remove();
  let order = more ? [more] : [];
  for (const a of shown) {
    let el = tray.querySelector(`.act[data-key="${CSS.escape(a.key)}"]`);
    if (!el) el = activityRow(a);
    fillActivityRow(el, a);
    order.push(el);
  }
  if (order.some((el, i) => tray.children[i] !== el)) tray.append(...order);
  const visible = shown.length > 0;
  if (tray.showPopover) {
    try {
      if (visible && !tray.matches(":popover-open")) tray.showPopover();
      else if (!visible && tray.matches(":popover-open")) tray.hidePopover();
    } catch {}
  } else tray.hidden = !visible;
}

// A popover is stacked in the order it was shown, so a dialog opened later would cover the tray: show it again on top.
function raiseActivity() {
  const tray = $("#activity");
  if (!tray.showPopover || !tray.matches(":popover-open")) return;
  try { tray.hidePopover(); tray.showPopover(); } catch {}
}
const showModalOriginal = HTMLDialogElement.prototype.showModal;
HTMLDialogElement.prototype.showModal = function (...args) { const r = showModalOriginal.apply(this, args); raiseActivity(); return r; };
if (!$("#activity").showPopover) $("#activity").hidden = true;
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") for (const a of activities.values()) if (a.waiting && a.state !== "running") scheduleFade(a);
});

// ---- local changes (all work offline) --------------------------------------

async function addScreenshots(files, note) {
  $("#add-dialog")?.close();
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  if (!images.length) return toast("Only images can be added.");
  const now = new Date().toISOString();
  for (const file of images) {
    const id = newId();
    const blob = file.slice(0, file.size, file.type);   // detach from the <input> FileList
    await db.put("blobs", blob, id);
    blobUrls.set(id, URL.createObjectURL(blob));
    await putItem({
      id, created_at: now, updated_at: now, status: "queued", error: null, note: note || null,
      category: null, title: null, subtitle: null, summary: null, metadata: {}, links: [], tags: [],
      pending_upload: true, mime: file.type, filename: file.name || "screenshot",
    });
    await enqueue({ type: "upload", id });
  }
  // online, the activity tray shows the analysis; offline there is nothing running, so say where they are
  if (!navigator.onLine) toast(`Saved ${images.length} screenshot${images.length > 1 ? "s" : ""} offline — will upload when you're back online`);
  render();
  requestSync();
}

// Save a link. Works offline too: it's queued and identified when the server is reachable.
async function addLink(raw, note) {
  $("#add-dialog")?.close();
  let url = (raw || "").trim();
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) url = "https://" + url;
  let parsed;
  try { parsed = new URL(url); } catch { return toast("That doesn't look like a link."); }
  if (!/^https?:$/.test(parsed.protocol) || !parsed.hostname.includes(".")) return toast("That doesn't look like a web link.");
  const existing = [...state.items.values()].find((i) => i.source_url && sameLink(i.source_url, url));
  if (existing) { toast("Already saved"); return renderDetail(existing); }
  const now = new Date().toISOString();
  const id = newId();
  await putItem({
    id, kind: "url", source_url: url, created_at: now, updated_at: now, status: "queued", error: null,
    note: note || null, category: null, title: parsed.hostname.replace(/^www\./, "") + (parsed.pathname.length > 1 ? parsed.pathname : ""),
    subtitle: null, summary: null, metadata: {}, links: [], tags: [], pending_upload: true, image_file: "",
  });
  await enqueue({ type: "upload", id });
  toast(navigator.onLine ? "Link saved — looking it up…" : "Link saved offline — will be looked up when you're back online");
  render();
  requestSync();
}

// ---- add by name: movie, TV show, book ---------------------------------------

const FIND = {
  movie: { title: "Add a movie", placeholder: "Movie title", icon: "🎬" },
  tv_show: { title: "Add a TV show", placeholder: "TV show title", icon: "📺" },
  book: { title: "Add a book", placeholder: "Book title (and author)", icon: "📚" },
};
let findKind = null, findSeq = 0, findResults = [];

function setAddMenu(open) {
  $("#add-menu").hidden = !open;
  $("#fab-more").setAttribute("aria-expanded", String(open));
  if (open) $("#add-menu button")?.focus();
}

function openFind(kind) {
  if (!FIND[kind]) return;
  setAddMenu(false);
  findKind = kind;
  $("#find-title").textContent = FIND[kind].title;
  $("#find-input").placeholder = FIND[kind].placeholder;
  $("#find-input").value = "";
  $("#find-results").innerHTML = navigator.onLine ? "" : `<p class="find-hint">Searching needs a connection.</p>`;
  $("#find-dialog").showModal();
  $("#find-input").focus();
}

function findRowHtml(r, i) {
  const pic = r.image_url ? `<img src="${esc(r.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="find-ph" aria-hidden="true">${FIND[r.category]?.icon || ""}</span>`;
  return `<button type="button" class="find-row" data-find-pick="${i}">${pic}<span><b>${esc(r.title)}</b><small>${esc(r.subtitle || "")}</small>${r.overview ? `<span class="find-ov">${esc(r.overview)}</span>` : ""}</span></button>`;
}

async function runFind(query) {
  const q = query.trim();
  const box = $("#find-results");
  if (!q) return;
  if (!navigator.onLine) { box.innerHTML = `<p class="find-hint">Searching needs a connection.</p>`; return; }
  const seq = ++findSeq;
  box.innerHTML = `<p class="find-hint">Searching…</p>`;
  let data;
  try {
    data = await api(`/api/catalog/search?kind=${encodeURIComponent(findKind)}&q=${encodeURIComponent(q)}`);
  } catch (e) {
    if (seq === findSeq) box.innerHTML = `<p class="find-hint">Couldn't search: ${esc(errorMessage(e))}</p>${typedRowHtml(q)}`;
    return;
  }
  if (seq !== findSeq) return;   // a newer search is under way
  findResults = data.results || [];
  // exactly one match is not a choice: add it
  if (findResults.length === 1) return addEntry(findResults[0]);
  box.innerHTML = (findResults.length ? findResults.map(findRowHtml).join("") : `<p class="find-hint">${esc(data.note || "Nothing found.")}</p>`) + typedRowHtml(q);
  box.querySelector("button")?.focus();
}

function typedRowHtml(q) {
  return `<button type="button" class="find-row find-typed" data-find-typed="${esc(q)}"><span class="find-ph" aria-hidden="true">＋</span><span><b>Add “${esc(q)}” as typed</b><small>Look it up after adding</small></span></button>`;
}

async function addEntry(spec) {
  $("#find-dialog").close();
  if (!navigator.onLine) return toast("Adding by name needs a connection.");
  const id = newId();
  const body = { id, category: spec.category, title: spec.title, year: spec.year || null, author: spec.author || null,
    overview: spec.overview || null, image_url: spec.image_url || null, tmdb: spec.tmdb || null, imdb_id: spec.imdb_id || null, };
  try {
    const item = await api("/api/items/entry", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await putItem(item);
    render();
    requestSync();
  } catch (e) {
    toast(`Couldn't add it: ${errorMessage(e)}`);
  }
}

function sameLink(a, b) {
  const norm = (u) => { try { const x = new URL(u); return (x.hostname.replace(/^www\./, "") + x.pathname.replace(/\/$/, "")).toLowerCase(); } catch { return u; } };
  return norm(a) === norm(b);
}

function looksLikeUrl(text) {
  return /^(https?:\/\/)?[\w-]+(\.[\w-]+)+(\/\S*)?$/i.test((text || "").trim());
}

// The user confirms an identification Magpie couldn't check itself.
async function verifyItem(id) {
  await editItem(id, { confirmed: true });
  toast("Marked as correct.");
}

async function editItem(id, patch) {
  const item = state.items.get(id);
  if (!item) return;
  if (patch.tags) patch.tags = [...new Set(patch.tags.map(normalizeTag).filter(Boolean))].sort();
  await putItem({ ...item, ...patch });
  await enqueue({ type: "patch", id, patch });
  render();
  requestSync();
}

async function reanalyzeItem(id) {
  const item = state.items.get(id);
  if (!item || item.pending_upload) return;
  pendingKind.set(id, "reanalyze");
  await putItem({ ...item, status: "processing", error: null });
  await enqueue({ type: "reanalyze", id });
  render();
  requestSync();
}

// Look up posters, covers and ratings again; the identification itself stays as it is.
async function refreshItemMetadata(id) {
  const item = state.items.get(id);
  if (!item || item.pending_upload) return;
  await enqueue({ type: "refresh", id });
  activityStart("refresh", id, activityText("refresh", item), navigator.onLine ? "" : "Waiting for the connection");
  requestSync();
}

// "The model got it wrong": fix facts directly, or describe it and let Claude look again.
async function correctItem(id, correction) {
  const item = state.items.get(id);
  if (!item) return;
  const fix = Object.fromEntries(Object.entries(correction).filter(([, v]) => v !== "" && v != null));
  if (!Object.keys(fix).length) return toast("Enter what it really is, or describe it.");
  pendingKind.set(id, "correct");
  await putItem({
    ...item, ...("title" in fix ? { title: fix.title } : {}), ...("category" in fix ? { category: fix.category } : {}),
    status: item.pending_upload ? item.status : "processing", error: null,
    corrected: true, verified: true, needs_review: false, confidence: "hint" in fix && Object.keys(fix).length === 1 ? item.confidence : 100,
  });
  await enqueue({ type: "correct", id, correction: fix });
  toast(navigator.onLine ? "Correction saved — updating details…" : "Correction saved — will update when you're back online");
  render();
  requestSync();
}

async function deleteItem(id) {
  const item = state.items.get(id);
  if (!item) return;
  const neverUploaded = item.pending_upload;
  await removeItem(id);
  if (!neverUploaded) await enqueue({ type: "delete", id });  // the server never saw the others
  render();
  requestSync();
}

// ---- sync with the self-hosted server --------------------------------------

class HttpError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
  get permanent() { return this.status >= 400 && this.status < 500 && ![401, 403, 408, 429].includes(this.status); }
}

async function api(path, opts = {}) {
  const token = await db.get("kv", "token");
  const headers = new Headers(opts.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeout || 30000);
  let res;
  try {
    res = await fetch(path, { ...opts, headers, signal: controller.signal, cache: "no-store" });
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new HttpError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

async function setToken(token) {
  token = (token || "").trim();
  await db.put("kv", token, "token");
  // Cookie too, so <img src="/media/..."> requests are authorized.
  document.cookie = `magpie_token=${encodeURIComponent(token)}; path=/; max-age=31536000; SameSite=Strict`;
}

let syncing = false, rerun = false, followUp = null;
let fixing = null;  // id of the item whose correction form is open

function requestSync() { sync().catch((e) => console.error(e)); }

// A quiet background check for what other devices added or changed: no "Syncing…" flash, no redraw when nothing changed.
async function pollChanges() {
  if (syncing || document.visibilityState !== "visible" || state.sync === "auth") return;
  syncing = true;
  let changed = false;
  try {
    const delta = await api(`/api/sync${state.lastSync ? `?since=${encodeURIComponent(state.lastSync)}` : ""}`, { timeout: 5000 });
    for (const id of delta.deleted) if (!hasPendingOps(id)) { await removeItem(id); changed = true; }
    for (const item of delta.items) if (!hasPendingOps(item.id)) { await mergeServerItem(item); changed = true; }
    state.lastSync = delta.server_time;
    await db.put("kv", state.lastSync, "lastSync");
    if (state.sync === "offline") setSyncState("idle");
  } catch {
    syncing = false;
    return requestSync();  // let the full sync report offline / auth / errors
  }
  syncing = false;
  if (changed) render();
  if (rerun) requestSync();
}

async function sync() {
  if (syncing) { rerun = true; return; }
  syncing = true;
  setSyncState("syncing");
  try {
    do {
      rerun = false;
      await importShared();
      try {
        await api("/api/health", { timeout: 5000 });
      } catch {
        return setSyncState("offline");
      }
      await refreshServerStatus();
      await pushOps();
      const delta = await api(`/api/sync${state.lastSync ? `?since=${encodeURIComponent(state.lastSync)}` : ""}`);
      for (const id of delta.deleted) if (!hasPendingOps(id)) await removeItem(id);
      for (const item of delta.items) if (!hasPendingOps(item.id)) await mergeServerItem(item);
      state.lastSync = delta.server_time;
      await db.put("kv", state.lastSync, "lastSync");
    } while (rerun);
    setSyncState("idle");
    refreshCostPill();
    refreshLimits();
  } catch (e) {
    if (e instanceof HttpError && e.status === 401) setSyncState("auth");
    else if (e instanceof HttpError) setSyncState("error", e.message);
    else setSyncState("offline");
  } finally {
    syncing = false;
    render();
    clearTimeout(followUp);
    // While the server is still identifying screenshots, check back soon.
    if ([...state.items.values()].some((i) => i.status === "processing") && state.sync === "idle") {
      followUp = setTimeout(requestSync, 3000);
    }
  }
}

// Replay queued changes in order; stop at the first failure that might succeed later.
async function pushOps() {
  while (state.ops.length) {
    const op = state.ops[0];
    try {
      if (op.type === "upload") {
        const item = state.items.get(op.id);
        const blob = item?.kind === "url" ? null : await db.get("blobs", op.id);
        if (!item || (item.kind !== "url" && !blob)) { await dropOp(op); continue; }
        const fd = new FormData();
        if (item.kind === "url") fd.append("url", item.source_url);
        else fd.append("file", blob, item.filename || `${op.id}.png`);
        fd.append("id", item.id);
        fd.append("created_at", item.created_at);
        if (item.note) fd.append("note", item.note);
        if (item.tags?.length) fd.append("tags", item.tags.join(","));
        const saved = await api("/api/items", { method: "POST", body: fd, timeout: 120000 });
        await dropOp(op);
        if (saved.id !== op.id) await removeItem(op.id);  // the link was already saved (maybe from another device)
        await mergeServerItem(saved);
        if (saved.duplicate) toast(`Already saved${saved.title ? ` as “${saved.title}”` : ""} — opened the existing card.`);
      } else if (op.type === "patch") {
        const saved = await api(`/api/items/${encodeURIComponent(op.id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op.patch),
        });
        await dropOp(op);
        if (!hasPendingOps(op.id)) await mergeServerItem(saved);
      } else if (op.type === "correct") {
        const saved = await api(`/api/items/${encodeURIComponent(op.id)}/correct`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(op.correction),
        });
        await dropOp(op);
        if (!hasPendingOps(op.id)) await mergeServerItem(saved);
      } else if (op.type === "reanalyze") {
        const saved = await api(`/api/items/${encodeURIComponent(op.id)}/reanalyze`, { method: "POST" });
        await dropOp(op);
        if (!hasPendingOps(op.id)) await mergeServerItem(saved);
      } else if (op.type === "refresh") {
        const before = state.items.get(op.id);
        const saved = await api(`/api/items/${encodeURIComponent(op.id)}/refresh-metadata`, { method: "POST", timeout: 60000 });
        await dropOp(op);
        if (!hasPendingOps(op.id)) await mergeServerItem(saved);
        activityFinish(`refresh:${op.id}`, "done", activityText("refresh", saved, "done"),
          saved.image_url && saved.image_url !== before?.image_url ? "Found a new cover" : "");
      } else if (op.type === "delete") {
        try {
          await api(`/api/items/${encodeURIComponent(op.id)}`, { method: "DELETE" });
        } catch (e) {
          if (!(e instanceof HttpError && e.status === 404)) throw e;
        }
        await dropOp(op);
      } else {
        await dropOp(op);
      }
    } catch (e) {
      if (!(e instanceof HttpError && e.permanent)) {
        if (op.type === "refresh") activityUpdate(`refresh:${op.id}`, { note: "Waiting for the connection" });
        throw e;
      }
      if (op.type === "refresh") activityFinish(`refresh:${op.id}`, "failed", activityText("refresh", state.items.get(op.id), "failed"), e.message);
      // Retrying would fail forever. 410: deleted on another device while we were offline.
      if (e.status === 410) await removeItem(op.id);
      else toast(`A change was rejected by the server: ${e.message}`);
      await dropOp(op);
    }
    render();
  }
}

// Screenshots shared to the installed app (Web Share Target — Android/desktop Chrome).
async function importShared() {
  if (!("caches" in window)) return;
  const cache = await caches.open("magpie-share-inbox");
  const keys = await cache.keys();
  for (const req of keys) {
    const res = await cache.match(req);
    const blob = await res.blob();
    const note = decodeURIComponent(res.headers.get("X-Magpie-Note") || "");
    await addScreenshots([new File([blob], "shared", { type: blob.type })], note);
    await cache.delete(req);
  }
}

// ---- server setup status and settings ----------------------------------------

async function refreshServerStatus() {
  try {
    state.server = await api("/api/status", { timeout: 10000 });
  } catch (e) {
    if (e instanceof HttpError && e.status === 401) throw e;
    // Older servers have no /api/status; anything else shows up through sync errors.
    if (!(e instanceof HttpError && e.status === 404)) console.warn("Server status unavailable:", e);
  }
  renderServerStatus();
}

function renderServerStatus() {
  const dot = $("#settings-dot");
  const s = state.server;
  const level = s && s.status !== "ok" ? s.status : null;
  dot.hidden = !level;
  dot.className = `status-dot ${level || ""}`;
  const counts = s ? s.problems.filter((p) => p.level !== "info").length : 0;
  $("#debug-btn").hidden = !(s && s.debug);   // the server is in debug mode: there is a history of conversations to browse
  $("#settings-btn").title = level ? `Settings — ${counts} problem${counts === 1 ? "" : "s"} with the server setup` : "Settings";
}

const INPUT_TYPES = { int: "number", float: "number", secret: "password", str: "text" };

function problemIcon(level) { return { error: "⛔", warning: "⚠", info: "ℹ" }[level] || ""; }

function settingInputHtml(s, typed = {}) {
  const id = `set-${s.env}`;
  const shown = s.env in typed ? typed[s.env] : "saved" in s ? s.saved : s.value;
  if (s.kind === "bool") {
    return `<select id="${id}" name="${esc(s.env)}">
      <option value="true" ${shown === true || shown === "true" ? "selected" : ""}>On</option>
      <option value="false" ${shown === false || shown === "false" ? "selected" : ""}>Off</option></select>`;
  }
  if (s.kind === "choice") {
    return `<select id="${id}" name="${esc(s.env)}">${s.choices.map((c) =>
      `<option value="${esc(c)}" ${c === shown ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  }
  if (s.kind === "secret") {
    return `<input id="${id}" name="${esc(s.env)}" type="password" autocomplete="new-password" spellcheck="false"
      value="${esc(typed[s.env] ?? "")}" placeholder="${s.is_set ? `${esc(s.value)} (leave blank to keep)` : "not set"}">`;
  }
  return `<input id="${id}" name="${esc(s.env)}" type="${INPUT_TYPES[s.kind] || "text"}" ${s.kind === "float" ? 'step="any"' : ""}
    value="${esc(shown ?? "")}" placeholder="${esc(s.default ?? "")}" spellcheck="false" autocapitalize="off">`;
}

let pendingProvider = null;  // provider chosen in a save that the server rejected
let settingsTab = "AI provider";  // section shown in the settings dialog

// Settings edited by the "AI provider" form rather than as separate rows.
const PROVIDER_ENVS = new Set(["MAGPIE_ANALYZER", "ANTHROPIC_API_KEY", "MAGPIE_MODEL", "MAGPIE_LLM_PROVIDER", "OPENAI_API_KEY", "GEMINI_API_KEY",
  "OPENROUTER_API_KEY", "GROQ_API_KEY", "LOCAL_LLM_URL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_MODEL"]);
const ADVANCED_GROUPS = ["Identification", "Local LLM", "OCR"];  // shown under "Advanced" in the AI provider section
const HIDDEN_GROUPS = ["Other AI providers"];

function settingEntry(data, env) {
  for (const g of data.groups) for (const s of g.settings) if (s.env === env) return s;
  return null;
}

// How screenshots get identified. The provider (Claude, OpenAI, …) is chosen separately.
const MODES = { model: "AI model", hybrid: "Hybrid (own server, then fallback)", ocr: "OCR only (no AI)" };
const MODE_OF = { claude: "model", local: "model", hybrid: "hybrid", ocr: "ocr" };  // from the resolved analyzer

// Which element edits which setting, for "Go to setting" links and error focus.
function focusIdFor(env) {
  const visible = (id) => { const el = document.getElementById(id); return el && !el.closest("[hidden]") ? id : null; };
  const map = { ANTHROPIC_API_KEY: ["ot-key"], MAGPIE_MODEL: ["ot-model"], MAGPIE_LLM_PROVIDER: ["ot-provider"],
    LOCAL_LLM_URL: ["lc-url", "ot-url"], LOCAL_LLM_MODEL: ["lc-model", "ot-model"], LOCAL_LLM_API_KEY: ["lc-key", "ot-key"] };
  if (/^(OPENAI|GEMINI|OPENROUTER|GROQ)_API_KEY$/.test(env)) return "ot-key";
  return (map[env] || []).map(visible).find(Boolean) || (map[env] ? map[env][0] : `set-${env}`);
}

const otherProviders = (data) => data.providers.filter((x) => x.id !== "claude");
function currentOtherProvider(data) {
  const hosted = settingEntry(data, "MAGPIE_LLM_PROVIDER")?.value;
  return otherProviders(data).some((x) => x.id === hosted) ? hosted : "local";
}
// In Hybrid the first pass is your own server, so the provider picker (the fallback) has no "local" entry.
const providersFor = (data, mode) => mode === "hybrid" ? data.providers.filter((x) => x.id !== "local") : data.providers;
const providerOptionsHtml = (list, selected) =>
  list.map((x) => `<option value="${esc(x.id)}" ${x.id === selected ? "selected" : ""}>${esc(x.label)}</option>`).join("");
const modelValueOf = (s, p) => "saved" in s ? s.saved : (s.source === "default" && p.model ? "" : s.value) ?? "";
const fieldErr = (data, errors, env) => errors[env] || (settingEntry(data, env)?.problem?.level === "error" ? settingEntry(data, env).problem.message : "");

function formRow(label, help, control, error = "", attrs = "") {
  return `
    <div class="setting wide ${error ? "has-error" : ""}" ${attrs}>
      <div class="setting-text"><label>${label}</label>${help ? `<p class="setting-help">${help}</p>` : ""}${error ? `<p class="field-error">${esc(error)}</p>` : ""}</div>
      <div class="setting-control">${control}</div>
    </div>`;
}

const keyHelp = (p) => p.id === "local" ? "Only if the server needs one." : "Stored on the server; never shown again after saving.";
const keyStatus = {};   // key setting -> {ok, text}: what the last test of that key found
const keyPill = (p) => { const st = keyStatus[p.key_env]; return st ? ` <span class="pill ${st.ok ? "ok" : "bad"}" title="${esc(st.text)}">${st.ok ? "✓ Works" : "✗ Rejected"}</span>` : ""; };
const keyLabel = (p, s) => `${esc(p.id === "claude" ? "Anthropic" : p.label)} API key${s.is_set ? ' <span class="pill ok">Set</span>' : ""}${keyPill(p)}`;

// What a provider says is left, e.g. "3,950 of 4,000 requests, 1.9M tokens left".
function limitsText(l) {
  if (!l) return "";
  const n = (v) => v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e4 ? `${Math.round(v / 1e3)}k` : Number(v).toLocaleString();
  const part = (x, unit) => x ? `${n(x.remaining)}${x.limit ? ` of ${n(x.limit)}` : ""} ${unit}` : "";
  const credit = l.credit && l.credit.remaining != null ? `$${Number(l.credit.remaining).toFixed(2)} credit` : "";
  return [part(l.requests, "requests"), part(l.tokens, "tokens"), credit].filter(Boolean).join(", ") + (l.requests || l.tokens || credit ? " left" : "");
}

function setKeyStatus(p, ok, text) {
  keyStatus[p.key_env] = { ok, text };
  for (const prefix of ["ot", "lc"]) {   // refresh the label wherever this provider's key is shown
    const input = $(`#${prefix}-key`);
    const shown = prefix === "lc" ? p.id === "local" : $("#ot-provider")?.value === p.id;
    if (input && shown) input.closest(".setting").querySelector("label").innerHTML = keyLabel(p, settingEntry(settingsData, p.key_env));
  }
}

function errorMessage(e) {
  let msg = e.message;
  try { msg = JSON.parse(e.message).message || JSON.parse(e.message).detail || msg; } catch {}
  return msg;
}

// Check a provider's key (the saved one when `key` is null) and remember the answer. Returns the server's reply, or null.
async function verifyKey(p, key = null, url = null) {
  try {
    const headers = { "Content-Type": "application/json" };
    if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
    const res = await api("/api/models", { method: "POST", headers, body: JSON.stringify({ provider: p.id, key, url }) });
    modelLists[p.id] = res.models;
    setKeyStatus(p, true, `Verified · ${res.models.length} models${res.limits ? " · " + limitsText(res.limits) : ""}`);
    return res;
  } catch (e) {
    const msg = errorMessage(e);
    if (/rejected/i.test(msg)) setKeyStatus(p, false, msg);
    throw new Error(msg);
  }
}
const keyPlaceholder = (s) => s.is_set ? `${s.value} (leave blank to keep)` : "not set";

function providerFormHtml(data, errors, typed) {
  const mode = MODE_OF[data.resolved_analyzer] || "model";
  const hybrid = mode === "hybrid";
  const local = data.providers.find((x) => x.id === "local");
  const lKey = settingEntry(data, local.key_env), lModel = settingEntry(data, local.model_env), urlS = settingEntry(data, "LOCAL_LLM_URL");
  const hostedNow = settingEntry(data, "MAGPIE_LLM_PROVIDER")?.value;
  const initial = data.resolved_analyzer === "claude" ? "claude"
    : hybrid ? (otherProviders(data).some((x) => x.id === hostedNow) ? hostedNow : "claude") : currentOtherProvider(data);
  const cur = (Object.keys(errors).length && pendingProvider) || initial;
  const p = providersFor(data, mode).find((x) => x.id === cur) || providersFor(data, mode)[0];
  const isClaude = p.id === "claude";
  const key = settingEntry(data, p.key_env), model = settingEntry(data, p.model_env);
  const hosted = p.url != null;
  const urlOf = (v) => "saved" in urlS ? urlS.saved : v;
  const urlValue = hosted ? p.url : (typed.LOCAL_LLM_URL ?? urlOf(urlS.value) ?? "");
  const endpointHelp = "Any OpenAI-compatible server, e.g. http://ollama:11434/v1.";
  return `
    <div id="provider-form" data-initial-mode="${mode}" data-initial-provider="${esc(initial)}">
      ${formRow("Analyzer", "How screenshots get identified.",
        `<select id="set-MAGPIE_ANALYZER" name="analyzer">${Object.entries(MODES).map(([v, l]) =>
          `<option value="${v}" ${v === mode ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`, errors.MAGPIE_ANALYZER || "")}
      <div class="provider-block" data-block="local" ${hybrid ? "" : "hidden"}>
        <h4>First pass: your server</h4>
        ${formRow("Endpoint", endpointHelp,
          `<input id="lc-url" name="lc_url" type="text" value="${esc(typed.LOCAL_LLM_URL ?? urlOf(urlS.value) ?? "")}" spellcheck="false" autocapitalize="off" placeholder="http://host:11434/v1">`,
          fieldErr(data, errors, "LOCAL_LLM_URL"))}
        ${formRow(keyLabel(local, lKey), keyHelp(local),
          `<input id="lc-key" name="lc_key" type="password" autocomplete="new-password" spellcheck="false" placeholder="${esc(keyPlaceholder(lKey))}"><button class="link-btn" type="button" data-test-key="lc" title="Check the key with the provider (uses the saved key if this box is empty)">Test key</button>`,
          fieldErr(data, errors, "LOCAL_LLM_API_KEY"))}
        ${formRow("Model", "The default is used unless you pick another.",
          modelControlHtml("lc", local, modelValueOf(lModel, local), lModel.default || ""), fieldErr(data, errors, "LOCAL_LLM_MODEL"))}
      </div>
      <div class="provider-block" data-block="other" ${mode === "ocr" ? "hidden" : ""}>
        <h4 id="ot-heading" ${hybrid ? "" : "hidden"}>Fallback provider</h4>
        ${formRow("Provider", hybrid ? "Asked when your server isn't confident enough." : "Claude, OpenAI, Gemini, OpenRouter, Groq, or your own server.",
          `<select id="ot-provider" name="ot_provider">${providerOptionsHtml(providersFor(data, mode), p.id)}</select>`, "", 'id="ot-provider-row"')}
        <div id="ot-url-row" ${isClaude || hybrid ? "hidden" : ""}>${formRow("Endpoint", hosted ? "Set by the provider you picked." : endpointHelp,
          `<input id="ot-url" name="ot_url" type="text" value="${esc(urlValue)}" ${hosted ? "readonly" : ""} spellcheck="false" autocapitalize="off" placeholder="http://host:11434/v1">`,
          hosted ? "" : fieldErr(data, errors, "LOCAL_LLM_URL"))}</div>
        ${formRow(keyLabel(p, key), keyHelp(p),
          `<input id="ot-key" name="ot_key" type="password" autocomplete="new-password" spellcheck="false" value="${esc(typed[p.key_env] ?? "")}" placeholder="${esc(keyPlaceholder(key))}"><button class="link-btn" type="button" data-test-key="ot" title="Check the key with the provider (uses the saved key if this box is empty)">Test key</button>`,
          fieldErr(data, errors, p.key_env))}
        ${formRow("Model", "The default is used unless you pick another.",
          modelControlHtml("ot", p, modelValueOf(model, p), p.model || model.default || ""), fieldErr(data, errors, p.model_env))}
      </div>
    </div>`;
}

// The analyzer mode changed: show what it needs.
function applyMode(mode) {
  const data = settingsData, sel = $("#ot-provider"), hybrid = mode === "hybrid";
  document.querySelector('[data-block="other"]').hidden = mode === "ocr";
  document.querySelector('[data-block="local"]').hidden = !hybrid;
  $("#ot-heading").hidden = !hybrid;
  $("#ot-provider-row .setting-help").textContent = hybrid ? "Asked when your server isn't confident enough."
    : "Claude, OpenAI, Gemini, OpenRouter, Groq, or your own server.";
  const list = providersFor(data, mode);
  const keep = list.some((x) => x.id === sel.value) ? sel.value : list[0].id;
  sel.innerHTML = providerOptionsHtml(list, keep);
  if (keep !== sel.dataset.shown) providerSwitch(sel);
  else $("#ot-url-row").hidden = keep === "claude" || hybrid;
  if (hybrid && $("#lc-url").value) loadModels("lc", true);
}

// The provider changed: endpoint, key and model follow it.
function providerSwitch(sel) {
  const data = settingsData, p = data.providers.find((x) => x.id === sel.value);
  const hybrid = $("#set-MAGPIE_ANALYZER").value === "hybrid";
  sel.dataset.shown = p.id;
  const key = settingEntry(data, p.key_env), model = settingEntry(data, p.model_env), urlS = settingEntry(data, "LOCAL_LLM_URL");
  const hosted = p.url != null;
  $("#ot-url-row").hidden = p.id === "claude" || hybrid;
  const url = $("#ot-url");
  url.readOnly = hosted;
  url.value = p.id === "claude" ? "" : hosted ? p.url : ("saved" in urlS ? urlS.saved : urlS.value) ?? "";
  url.closest(".setting").querySelector(".setting-help").textContent =
    hosted ? "Set by the provider you picked." : "Any OpenAI-compatible server, e.g. http://ollama:11434/v1.";
  const input = $("#ot-key");
  input.value = "";
  input.placeholder = keyPlaceholder(key);
  const row = input.closest(".setting");
  row.querySelector("label").innerHTML = keyLabel(p, key);
  row.querySelector(".setting-help").textContent = keyHelp(p);
  renderModelControl("ot", p, modelValueOf(model, p), p.model || model.default || "");
  sel.closest(".provider-block").querySelectorAll(".setting.has-error").forEach((r) => r.classList.remove("has-error"));
  if (key.is_set || p.id === "local") loadModels("ot", true);
}

// ---- model picker: the provider's own model list, fetched by the server ----------
const modelLists = {};   // provider id -> [{id, label}]

// ---- fuzzy search over a provider's models --------------------------------------
// "gpt4omini" finds "gpt-4o-mini", "opus5" finds "claude-opus-5": every typed word must appear in order (not
// necessarily side by side); exact runs, word starts and tight matches rank first.
function fuzzyOne(token, text) {
  const at = text.indexOf(token);
  if (at >= 0) {
    const boundary = at === 0 || /[^a-z0-9]/.test(text[at - 1]);
    return { score: 1000 - at + (boundary ? 200 : 0) + token.length * 3, pos: Array.from({ length: token.length }, (_, i) => at + i) };
  }
  let from = 0, prev = -2, score = 0;
  const pos = [];
  for (const c of token) {
    const i = text.indexOf(c, from);
    if (i < 0) return null;
    score += (i === prev + 1 ? 18 : 0) + (i === 0 || /[^a-z0-9]/.test(text[i - 1]) ? 12 : 0) - Math.min(i - from, 12);
    pos.push(i);
    prev = i; from = i + 1;
  }
  return { score, pos };
}

function fuzzyMatch(query, text) {
  const t = fold(text), tokens = fold(query).split(/\s+/).filter(Boolean);
  if (!tokens.length) return { score: 0, pos: new Set() };
  let score = 0;
  const pos = new Set();
  for (const token of tokens) {
    const m = fuzzyOne(token, t);
    if (!m) return null;
    score += m.score;
    m.pos.forEach((i) => pos.add(i));
  }
  return { score: score - t.length * 0.2, pos };
}

function highlight(text, pos) {
  return [...text].map((ch, i) => (pos.has(i) ? `<mark>${esc(ch)}</mark>` : esc(ch))).join("");
}

function modelControlHtml(prefix, p, value, defaultModel) {
  const list = modelLists[p.id];
  const refresh = `<button class="link-btn" type="button" data-load-models="${prefix}" title="Fetch the models this provider offers">${list ? "Refresh" : "Load models"}</button>`;
  return `<div class="combo" data-provider="${esc(p.id)}">
    <input id="${prefix}-model" name="${prefix}_model" class="combo-input" type="text" value="${esc(value)}" placeholder="${esc(defaultModel)}"
      role="combobox" aria-expanded="false" aria-autocomplete="list" aria-controls="${prefix}-model-list" data-default="${esc(defaultModel)}"
      autocomplete="off" spellcheck="false" autocapitalize="off" ${list ? `title="Type to search ${list.length} models"` : ""}>
    <ul class="combo-list" id="${prefix}-model-list" role="listbox" hidden></ul></div>${refresh}`;
}

// The list under a model box: the provider's models that fit what was typed, best first.
function renderComboList(input, query) {
  const box = input.closest(".combo"), ul = box.querySelector(".combo-list");
  const models = modelLists[box.dataset.provider];
  if (!models) { ul.hidden = true; input.setAttribute("aria-expanded", "false"); return; }
  const rows = [];
  for (const m of models) {
    const shown = m.label === m.id ? m.id : `${m.label} (${m.id})`;
    const hit = fuzzyMatch(query, shown);
    if (hit) rows.push({ id: m.id, shown, hit });
  }
  if (query.trim()) rows.sort((a, b) => b.hit.score - a.hit.score);
  const def = input.dataset.default;
  const items = [...(!query.trim() ? [{ id: "", html: `<b>Default</b>${def ? ` <span class="combo-sub">${esc(def)}</span>` : ""}` }] : []),
    ...rows.slice(0, 60).map((r) => ({ id: r.id, html: highlight(r.shown, r.hit.pos) }))];
  ul.innerHTML = items.length
    ? items.map((it, i) => `<li role="option" class="combo-opt${i === 0 ? " active" : ""}" data-value="${esc(it.id)}" id="${ul.id}-${i}">${it.html}</li>`).join("")
    : `<li class="combo-empty">No model matches “${esc(query)}”. You can still use it as typed.</li>`;
  ul.hidden = false;
  input.setAttribute("aria-expanded", "true");
  if (!items.length) input.removeAttribute("aria-activedescendant"); else input.setAttribute("aria-activedescendant", `${ul.id}-0`);
  ul.scrollTop = 0;
  // a box at the bottom of the scrolling settings pane: scroll so the whole list is in view
  requestAnimationFrame(() => { if (!ul.hidden) ul.scrollIntoView({ block: "nearest" }); });
}

function closeCombo(input) {
  input.closest(".combo")?.querySelector(".combo-list")?.setAttribute("hidden", "");
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
}

function moveCombo(input, delta) {
  const ul = input.closest(".combo").querySelector(".combo-list");
  const opts = [...ul.querySelectorAll(".combo-opt")];
  if (!opts.length) return;
  const at = Math.max(0, opts.findIndex((o) => o.classList.contains("active")));
  const next = opts[(at + delta + opts.length) % opts.length];
  opts.forEach((o) => o.classList.toggle("active", o === next));
  input.setAttribute("aria-activedescendant", next.id);
  next.scrollIntoView({ block: "nearest" });
}

function pickCombo(input, value) {
  input.value = value;
  closeCombo(input);
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

document.addEventListener("focusin", (e) => { if (e.target.matches?.(".combo-input")) renderComboList(e.target, ""); });
document.addEventListener("input", (e) => { if (e.target.matches?.(".combo-input")) renderComboList(e.target, e.target.value); });
document.addEventListener("focusout", (e) => { if (e.target.matches?.(".combo-input")) setTimeout(() => closeCombo(e.target), 120); });
document.addEventListener("mousedown", (e) => {
  const opt = e.target.closest?.(".combo-opt");
  if (!opt) return;
  e.preventDefault();   // keep the box focused; the pick below closes the list
  pickCombo(opt.closest(".combo").querySelector(".combo-input"), opt.dataset.value);
});
document.addEventListener("keydown", (e) => {
  const input = e.target.matches?.(".combo-input") ? e.target : null;
  if (!input) return;
  const open = !input.closest(".combo").querySelector(".combo-list").hidden;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!open) renderComboList(input, ""); else moveCombo(input, e.key === "ArrowDown" ? 1 : -1);
  } else if (e.key === "Enter" && open) {
    e.preventDefault();   // choose, don't save the whole form
    const active = input.closest(".combo").querySelector(".combo-opt.active");
    if (active) pickCombo(input, active.dataset.value); else closeCombo(input);
  } else if (e.key === "Escape" && open) {
    e.preventDefault(); e.stopPropagation();   // closes the list, not the settings dialog
    closeCombo(input);
  }
}, true);

function renderModelControl(prefix, p, value, defaultModel) {
  const control = $(`#${prefix}-model`)?.closest(".setting-control");
  if (control) control.innerHTML = modelControlHtml(prefix, p, value, defaultModel);
}

async function loadModels(prefix, silent = false) {
  const form = $("#settings-form");
  if (!form?.elements.analyzer) return;
  const data = settingsData;
  const p = data.providers.find((x) => x.id === (prefix === "lc" ? "local" : form.elements.ot_provider.value));
  const btn = form.querySelector(`[data-load-models="${prefix}"]`);
  if (btn) { btn.disabled = true; btn.textContent = "Loading…"; }
  const body = { provider: p.id, key: form.elements[`${prefix}_key`].value.trim() || null,
                 url: p.id === "local" ? form.elements[`${prefix}_url`].value.trim() || null : null };
  try {
    const headers = { "Content-Type": "application/json" };
    if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
    const res = await verifyKey(p, body.key, body.url);
    if (prefix === "ot" && form.elements.ot_provider.value !== p.id) return;   // switched meanwhile
    const modelS = settingEntry(data, p.model_env);
    renderModelControl(prefix, p, form.elements[`${prefix}_model`].value, p.model || modelS.default || "");
    if (!res.models.length && !silent) toast("The provider returned no models.");
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = modelLists[p.id] ? "Refresh" : "Load models"; }
    if (silent) return;   // e.g. no key yet: the text box still works
    toast(`Couldn't load models: ${e.message}`);
  }
}

// "Test key": ask the server to call the provider's model list with the typed (or saved) key.
async function testKey(prefix) {
  const form = $("#settings-form");
  const p = settingsData.providers.find((x) => x.id === (prefix === "lc" ? "local" : form.elements.ot_provider.value));
  const btn = form.querySelector(`[data-test-key="${prefix}"]`);
  const idle = btn.textContent;
  btn.disabled = true; btn.textContent = "Testing…";
  const body = { provider: p.id, key: form.elements[`${prefix}_key`].value.trim() || null,
                 url: p.id === "local" ? form.elements[`${prefix}_url`].value.trim() || null : null };
  try {
    const res = await verifyKey(p, body.key, body.url);
    toast(`✓ ${p.label || p.id}: the key works (${res.models.length} models${res.limits ? "; " + limitsText(res.limits) : ""}).`);
  } catch (e) {
    toast(`✗ ${p.label || p.id}: ${e.message}`);
  } finally {
    btn.disabled = false; btn.textContent = idle;
  }
}

// What the AI provider form changes, as settings changes.
function providerChanges(form, data) {
  const changes = {};
  const box = form.querySelector("#provider-form");
  if (!box) return changes;
  const mode = form.elements.analyzer.value;
  const p = data.providers.find((x) => x.id === form.elements.ot_provider.value);
  if (mode !== box.dataset.initialMode || (mode !== "ocr" && p.id !== box.dataset.initialProvider)) {
    changes.MAGPIE_ANALYZER = mode === "model" ? (p.id === "claude" ? "claude" : "local") : mode;
  }
  const edit = (env, value, s, prov) => {   // a text setting: send only when it differs from what's stored
    if (value !== String(modelValueOf(s, prov))) changes[env] = value === "" ? null : value;
  };
  const urlEdit = (value) => {
    const urlS = settingEntry(data, "LOCAL_LLM_URL");
    if (value !== String("saved" in urlS ? urlS.saved : urlS.value ?? "")) changes.LOCAL_LLM_URL = value === "" ? null : value;
  };
  if (mode !== "ocr") {
    const hostedNow = settingEntry(data, "MAGPIE_LLM_PROVIDER")?.value;
    const target = p.url != null ? p.id : "none";   // which hosted preset is in use ("none" = Claude or your own server)
    if (target !== hostedNow) changes.MAGPIE_LLM_PROVIDER = target;
    const key = form.elements.ot_key.value.trim();
    if (key) changes[p.key_env] = key;
    edit(p.model_env, form.elements.ot_model.value.trim(), settingEntry(data, p.model_env), p);
    if (p.id === "local") urlEdit(form.elements.ot_url.value.trim());
  }
  if (mode === "hybrid") {   // the first pass: your own server
    const local = data.providers.find((x) => x.id === "local");
    const key = form.elements.lc_key.value.trim();
    if (key) changes[local.key_env] = key;
    edit(local.model_env, form.elements.lc_model.value.trim(), settingEntry(data, local.model_env), local);
    urlEdit(form.elements.lc_url.value.trim());
  }
  return changes;
}

function settingRowHtml(s, errors, typed) {
  const err = errors[s.env] || (s.problem && s.problem.level === "error" ? s.problem.message : "");
  const warn = !err && s.problem ? s.problem.message : "";
  const wide = s.kind === "str" || s.kind === "secret";
  return `
    <div class="setting ${wide ? "wide" : ""} ${err ? "has-error" : warn ? "has-warning" : ""}">
      <div class="setting-text">
        <label for="set-${esc(s.env)}">${esc(s.label)}${s.kind === "secret" && s.is_set ? ' <span class="pill ok">Set</span>' : ""}${
          s.source === "ui" ? ' <span class="pill">Saved here</span>' : s.source === "env" ? ' <span class="pill">Environment</span>' : ""}</label>
        ${s.help ? `<p class="setting-help">${esc(s.help)}</p>` : ""}
        ${err ? `<p class="field-error">${esc(err)}</p>` : warn ? `<p class="field-warning">${esc(warn)}</p>` : ""}
      </div>
      <div class="setting-control">
        ${settingInputHtml(s, typed)}
        ${s.source === "ui" ? `<button class="link-btn" type="button" data-reset="${esc(s.env)}" title="Remove the saved value and fall back to the environment or default">Reset</button>` : ""}
      </div>
    </div>`;
}

function settingsHtml(data, errors = {}, typed = {}) {
  const problems = data.status.problems;
  const advanced = data.groups.filter((g) => ADVANCED_GROUPS.includes(g.name))
    .flatMap((g) => g.settings).filter((s) => !PROVIDER_ENVS.has(s.env));
  const others = data.groups.filter((g) => !ADVANCED_GROUPS.includes(g.name) && !HIDDEN_GROUPS.includes(g.name));
  const names = ["AI provider", ...others.map((g) => g.name), "Plugins", "Library", "Appearance", "About"];
  if (!names.includes(settingsTab)) settingsTab = names[0];
  const hasLevel = (list) => list.some((s) => errors[s.env] || (s.problem && s.problem.level === "error")) ? "error"
    : list.some((s) => s.problem && s.problem.level === "warning") ? "warning" : "";
  const aiSettings = data.groups.filter((g) => ADVANCED_GROUPS.includes(g.name) || HIDDEN_GROUPS.includes(g.name)).flatMap((g) => g.settings);
  const badge = (name) => {
    const g = others.find((x) => x.name === name);
    const level = name === "AI provider" ? hasLevel(aiSettings) : g ? hasLevel(g.settings) : "";
    return level ? `<span class="nav-dot ${level}" aria-label="${level}"></span>` : "";
  };
  const alerts = problems.filter((p) => p.level !== "info");
  const section = (name, body) => `<section class="settings-section" data-section="${esc(name)}" ${settingsTab === name ? "" : "hidden"}><h3>${esc(name)}</h3>${body}</section>`;
  return `
    <div class="settings-panel">
      <header class="settings-head">
        <h2>Settings</h2>
        <label class="settings-search"><span aria-hidden="true">⌕</span><input id="settings-search" type="search" placeholder="Search settings" aria-label="Search settings" autocomplete="off" spellcheck="false"></label>
        <button class="btn close" data-action="close" aria-label="Close">✕</button>
      </header>
      <form id="settings-form" autocomplete="off">
        <div class="settings-body">
          <nav class="settings-nav" role="tablist" aria-label="Settings sections">
            ${names.map((n) => `<button type="button" role="tab" data-settings-tab="${esc(n)}" aria-selected="${n === settingsTab}">${esc(n)}${badge(n)}</button>`).join("")}
          </nav>
          <div class="settings-content">
            ${alerts.length ? `<details class="status-summary ${alerts.some((p) => p.level === "error") ? "error" : "warning"}" ${alerts.length <= 2 ? "open" : ""}>
              <summary>${alerts.length} thing${alerts.length === 1 ? "" : "s"} need${alerts.length === 1 ? "s" : ""} attention</summary>
              <ul>${alerts.map((p) => `<li>${esc(p.message)}${p.key ? ` <a href="#set-${esc(p.key)}" data-focus="set-${esc(p.key)}">Go to setting</a>` : ""}</li>`).join("")}</ul>
            </details>` : `<p class="ok-line">✓ Everything is set up.</p>`}
            ${section("AI provider", `${providerFormHtml(data, errors, typed)}
              <details class="advanced" ${hasLevel(advanced) ? "open" : ""}><summary>Advanced</summary>
                ${advanced.map((s) => settingRowHtml(s, errors, typed)).join("")}</details>`)}
            ${others.map((g) => section(g.name, `${g.settings.map((s) => settingRowHtml(s, errors, typed)).join("")}
              ${g.name === "Logging" ? logsHtml() : ""}
              ${(data.plugins || []).filter((p) => p.label === g.name).map((p) => `<div class="plugin-test"><button class="btn" type="button" data-test-plugin="${esc(p.id)}">Test connection</button><span class="test-result" data-test-result="${esc(p.id)}" role="status" aria-live="polite">Uses what is typed above, or the saved values.</span></div>`).join("")}
              ${g.name === "Access" && data.setup_code_required ? `<p class="setting-note">This server has no access token yet, so saving asks for the setup code printed in the server log (<code>docker compose logs magpie</code>). Setting an access token removes that step.</p>` : ""}`)).join("")}
            ${section("Plugins", pluginsAdminHtml(data))}
            ${section("Library", libraryHtml())}
            ${section("Appearance", appearanceHtml())}
            ${section("About", aboutHtml(data))}
            <p id="settings-none" class="setting-note" hidden></p>
            <p class="setting-note">Saved in <code>${esc(data.settings_file)}</code>; overrides environment variables. Changes apply immediately.</p>
          </div>
        </div>
        <footer class="settings-foot">
          <button class="btn" type="button" data-action="close">Cancel</button>
          <button class="btn primary" type="submit">Save changes</button>
        </footer>
      </form>
    </div>`;
}

function selectSettingsTab(name) {
  settingsTab = name;
  if (name === "Logging") loadLogs();
  document.querySelectorAll("[data-settings-tab],[data-load-models]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.settingsTab === name)));
  document.querySelectorAll(".settings-section").forEach((s) => { s.hidden = s.dataset.section !== name; });
  $(".settings-content")?.scrollTo({ top: 0 });
  autoCheckPlugin();
}

// ---- appearance (per device) --------------------------------------------------

const THEME_COLORS = { light: "#f6f5f2", dark: "#151412" };

function getTheme() {
  try { return localStorage.getItem("magpie.theme") || "system"; } catch { return "system"; }
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  else delete root.dataset.theme;
  // Browser/PWA chrome colour: forced themes override both media-specific values.
  document.querySelectorAll('meta[name="theme-color"]').forEach((m) => {
    const scheme = (m.media.match(/(light|dark)/) || [])[1] || "light";
    m.content = THEME_COLORS[theme === "light" || theme === "dark" ? theme : scheme];
  });
  installIcons(theme);
}

// The icon an installed app gets is read when it is added to the home screen, from the manifest and the touch
// icon, which can't adapt to the colour scheme by themselves: point them at the dark set when the app is dark.
const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)");
function installIcons(theme = getTheme()) {
  const dark = theme === "dark" || (theme !== "light" && !!prefersDark?.matches);
  const set = (selector, href) => { const el = document.querySelector(selector); if (el && el.getAttribute("href") !== href) el.setAttribute("href", href); };
  set('link[rel="manifest"]', dark ? "/static/manifest-dark.webmanifest" : "/static/manifest.webmanifest");
  set('link[rel="apple-touch-icon"]', dark ? "/static/icons/apple-touch-icon-dark.png" : "/static/icons/apple-touch-icon.png");
}
prefersDark?.addEventListener?.("change", () => installIcons());

function setTheme(theme) {
  try {
    if (theme === "system") localStorage.removeItem("magpie.theme");
    else localStorage.setItem("magpie.theme", theme);
  } catch {}
  applyTheme(theme);
  document.querySelectorAll(".segmented [data-theme-choice]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.themeChoice === theme)));
}

function appearanceHtml() {
  const current = getTheme();
  return `<div class="setting"><div class="setting-text"><label>Theme</label><p class="setting-help">This device only.</p></div>
    <div class="setting-control"><div class="segmented" role="group" aria-label="Theme">${
    [["system", "System"], ["light", "Light"], ["dark", "Dark"]].map(([value, label]) =>
      `<button type="button" data-theme-choice="${value}" aria-pressed="${value === current}">${label}</button>`).join("")
  }</div></div></div>`;
}

// ---- library-wide actions: refresh all metadata, re-analyze all -----------------------------

const bulkScopes = () => {
  const items = [...state.items.values()].filter((i) => i.status !== "processing" && !i.pending_upload && !i.batch_pending);
  return { all: items.length, check: items.filter(needsCheck).length, failed: items.filter((i) => i.status === "error").length };
};

function libraryHtml() {
  const n = bulkScopes();
  return `
    <p class="setting-help" style="margin-top:0">Run one of these on many items at once. Items that are being analyzed right now are left out.</p>
    <div class="setting"><div class="setting-text"><label for="bulk-scope">Which items</label></div>
      <div class="setting-control"><select id="bulk-scope">
        <option value="all">All items (${n.all})</option>
        <option value="check">Only items to check (${n.check})</option>
        <option value="failed">Only failed (${n.failed})</option></select></div></div>
    <div class="bulk-card">
      <h4>Refresh all metadata</h4>
      <p>Looks up posters, covers, ratings and related links again. No AI is asked, so it costs nothing, and nothing you edited or confirmed changes.</p>
      <button class="btn" type="button" data-bulk="refresh">⟳ Refresh metadata</button>
    </div>
    <div class="bulk-card">
      <h4>Re-analyze all</h4>
      <p>Identifies the items again with your AI provider, which costs money (Claude runs in a batch at half price, so results take a while).</p>
      <label class="check"><input type="checkbox" id="bulk-skip" checked> Leave out items I corrected or confirmed, because re-analyzing replaces them</label>
      <button class="btn" type="button" data-bulk="reanalyze">↻ Re-analyze…</button>
    </div>
    <div id="bulk-progress" class="bulk-progress" hidden aria-live="polite"></div>`;
}

// ---- conversation logs (Settings → Logging) -----------------------------------------------------------------------

const OP_LABEL = { analyze: "Analysis", reanalyze: "Re-analysis", correct: "Correction", bulk: "Bulk re-analysis", batch_result: "Claude batch result" };
let logsFilter = "";

// Logs hold what is in your screenshots, so they need the same access as settings: the setup code, if there's no token.
async function apiWithSetup(path, opts = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = { ...(opts.headers || {}) };
    if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
    try {
      return await api(path, { ...opts, headers });
    } catch (e) {
      let detail = {};
      try { detail = JSON.parse(e.message); } catch {}
      if (!(e instanceof HttpError && e.status === 403 && detail.code === "setup_code_required")) throw e;
      const code = prompt(`${setupCode ? "That setup code didn't match. " : ""}${detail.message}\n\nSetup code:`);
      if (!code) throw new Error("Cancelled");
      setupCode = code.trim();
    }
  }
  throw new Error("The setup code didn't match.");
}

async function fetchLogFile(id, format) {
  const token = await db.get("kv", "token");
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
  const res = await fetch(`/api/logs/${id}?format=${format}`, { headers, cache: "no-store" });
  if (!res.ok) throw new Error(res.statusText);
  return res.blob();
}

async function saveLogFile(id, format) {
  try {
    const blob = await fetchLogFile(id, format);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${id.replace("/", "_")}.${format === "md" ? "md" : "jsonl"}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (e) { toast(`Couldn't download: ${e.message}`); }
}

function logsHtml() {
  return `<div id="logs-box" class="logs-box"><p class="meta-line">Loading…</p></div>`;
}

const fmtBytes = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1e3 ? `${Math.round(n / 1e3)} kB` : `${n} B`;

async function loadLogs() {
  const box = $("#logs-box");
  if (!box) return;
  let res;
  try {
    res = await apiWithSetup(`/api/logs?limit=200${logsFilter ? `&operation=${encodeURIComponent(logsFilter)}` : ""}`);
  } catch (e) {
    box.innerHTML = `<p class="meta-line">${esc(e.message === "Cancelled" ? "Enter the setup code to see the logs." : `Couldn't load the logs: ${errorMessage(e)}`)} <button class="link-btn" type="button" data-logs-reload>Try again</button></p>`;
    return;
  }
  const rows = res.sessions.map((x) => {
    const when = new Date(x.started);
    const ok = x.outcome === "ok" ? "ok" : x.outcome === "queued_for_batch" || x.outcome === "running" ? "" : "bad";
    return `<button type="button" class="log-row" data-log-open="${esc(x.id)}">
      <span class="log-when">${esc(when.toLocaleDateString([], { month: "short", day: "numeric" }))} ${esc(when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</span>
      <span class="log-op">${esc(OP_LABEL[x.operation] || x.operation)}</span>
      <span class="log-title">${esc(x.title || x.item_id || "—")}</span>
      <span class="log-model">${esc(x.models.map((m) => m.replace(/^[^:]*:/, "")).join(", ") || "no model call")}${x.cost_usd ? ` · ${esc(formatUsd(x.cost_usd))}` : ""}</span>
      <span class="pill ${ok}">${esc(x.outcome.replace(/_/g, " "))}</span></button>`;
  }).join("");
  box.innerHTML = `
    <div class="logs-bar">
      <span class="meta-line">${res.usage.files} session${res.usage.files === 1 ? "" : "s"} · ${esc(fmtBytes(res.usage.bytes))} · ${res.retention_days ? `kept ${res.retention_days} days` : "kept until deleted"}</span>
      <select id="logs-op" aria-label="Operation"><option value="">All operations</option>${Object.entries(OP_LABEL).map(([k, v]) => `<option value="${k}" ${logsFilter === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
      <button class="btn small" type="button" data-logs-reload>↻ Refresh</button>
      ${res.usage.files ? `<button class="btn small" type="button" data-logs-clear>Delete all…</button>` : ""}
    </div>
    ${res.enabled ? "" : `<p class="setting-note">Logging is off. Turn on “Save conversations with the AI models” above, save, and each analysis from then on is recorded here.</p>`}
    ${rows ? `<div class="log-list">${rows}</div>` : (res.enabled ? `<p class="meta-line">Nothing recorded yet. Save a screenshot, or re-analyze one.</p>` : "")}
    <div id="log-detail" class="log-detail" hidden></div>`;
}

function logEventHtml(e) {
  const d = new Date(e.t), t = isNaN(d) ? "" : `${d.toLocaleTimeString([], { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`;
  const pre = (v) => `<pre>${esc(typeof v === "string" ? v : JSON.stringify(v, null, 1))}</pre>`;
  const rest = (skip) => Object.fromEntries(Object.entries(e).filter(([k]) => !["t", "event", ...skip].includes(k)));
  let title = e.event, body = "", open = false;
  if (e.event === "llm_request") {
    title = `→ ${e.provider} · ${e.model} (${e.mode})${e.attempt > 1 ? ` · attempt ${e.attempt}` : ""}`;
    body = `${e.system ? `<h5>System</h5>${pre(e.system)}` : ""}${(e.messages || []).map((m) => `<h5>${esc(m.role)}</h5>${
      typeof m.content === "string" ? pre(m.content) : (m.content || []).map((c) => c.type === "text" ? pre(c.text)
        : c.type === "image" ? `<p class="meta-line">🖼 image ${esc(c.media_type || "")} ${esc(c.size || "")} · ${esc(fmtBytes(c.bytes))} · ${esc(c.sha256 || "")}</p>` : pre(c)).join("")}`).join("")}
      <details><summary>parameters</summary>${pre(rest(["provider", "model", "mode", "system", "messages", "attempt"]))}</details>`;
  } else if (e.event === "llm_response") {
    title = `← ${e.provider} · ${e.model}${e.status ? ` · HTTP ${e.status}` : ""}${e.duration_ms != null ? ` · ${e.duration_ms} ms` : ""}${e.error ? " · error" : ""}`;
    body = `${e.text != null ? pre(e.text) : ""}${e.content != null ? pre(e.content) : ""}${e.error ? `<p class="meta-line">${esc(typeof e.error === "string" ? e.error : JSON.stringify(e.error))}</p>` : ""}
      <details><summary>usage and headers</summary>${pre(rest(["provider", "model", "text", "content", "status", "duration_ms", "error"]))}</details>`;
    open = true;
  } else if (e.event === "decision") {
    title = `decision · ${(e.what || "").replace(/_/g, " ")}`;
    body = pre(rest(["what"]));
    open = true;
  } else if (e.event === "web_search") {
    title = `web search · ${(e.queries || []).length} quer${(e.queries || []).length === 1 ? "y" : "ies"} · ${(e.sources || []).length} page${(e.sources || []).length === 1 ? "" : "s"} read`;
    body = `${(e.queries || []).map((q) => `<p>🔎 ${esc(q)}</p>`).join("")}${(e.sources || []).map((u) => `<p class="meta-line"><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a></p>`).join("")}`;
    open = true;
  } else if (e.event === "link_search") {
    title = e.found ? `looking for the article's address · found it` : `looking for the article's address · not found`;
    const how = (q) => Object.entries(q.engines || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
    body = `<p>${e.found ? `Using <a href="${esc(e.found)}" target="_blank" rel="noopener noreferrer">${esc(e.found)}</a>` : "No page checked out, so no link is shown."}</p>
      ${(e.queries || []).length ? `<h5>Searches</h5>${e.queries.map((q) => `<p class="meta-line">${esc(q.query)}<br>${esc(how(q))} · ${q.results} result${q.results === 1 ? "" : "s"}</p>`).join("")}` : ""}
      ${(e.checked || []).length ? `<h5>Pages checked</h5>${e.checked.map((c) => `<p class="meta-line"><a href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.title || c.url)}</a><br>${esc(c.verdict)} · from ${esc(c.from)}</p>`).join("")}` : `<p class="meta-line">No candidate pages were found at all: the search engines gave nothing (see above).</p>`}`;
    open = !e.found;
  } else if (e.event === "session_start" || e.event === "session_end") {
    title = e.event === "session_end" ? `end · ${e.outcome}${e.error ? ` · ${e.error}` : ""}` : "start";
    body = pre(rest([]));
  } else {
    body = pre(rest([]));
  }
  return `<details class="log-event ev-${esc(e.event)}" ${open ? "open" : ""}><summary><time>${esc(t)}</time> ${esc(title)}</summary>${body}</details>`;
}

async function openLog(id) {
  const box = $("#log-detail");
  if (!box) return;
  box.hidden = false;
  box.innerHTML = `<p class="meta-line">Loading…</p>`;
  try {
    const d = await apiWithSetup(`/api/logs/${id}`);
    box.innerHTML = `
      <div class="logs-bar"><h4>${esc(OP_LABEL[d.operation] || d.operation)} · ${esc(d.title || d.item_id || "")}</h4>
        <button class="btn small" type="button" data-log-save="${esc(id)}" data-format="md">⬇ Transcript (.md)</button>
        <button class="btn small" type="button" data-log-save="${esc(id)}" data-format="jsonl">⬇ Raw (.jsonl)</button>
        <button class="btn small" type="button" data-log-delete="${esc(id)}">Delete</button></div>
      ${d.events.map(logEventHtml).join("")}
      ${d.files.length ? `<p class="meta-line">Saved images: ${d.files.map((f) => `<a href="#" data-log-image="${esc(id)}/image/${esc(f)}">${esc(f.split(".").slice(-2).join("."))}</a>`).join(", ")}</p>` : ""}`;
    box.scrollIntoView({ block: "nearest" });
  } catch (e) { box.innerHTML = `<p class="meta-line">Couldn't open it: ${esc(errorMessage(e))}</p>`; }
}

async function showLogImage(path) {
  try {
    const token = await db.get("kv", "token");
    const headers = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
    const res = await fetch(`/api/logs/${path}`, { headers });
    if (!res.ok) throw new Error(res.statusText);
    window.open(URL.createObjectURL(await res.blob()), "_blank");
  } catch (e) { toast(`Couldn't open the image: ${e.message}`); }
}

// ---- debug view (server in debug mode) ---------------------------------------------------------------------------
// The history of conversations with the AI models: for each, the capture that went in, the conversation, the card that
// came out and the model that was used.

const logImageUrls = new Map();   // log image path -> object URL (the files need the access token, so <img src> can't fetch them)

async function logImageUrl(path) {
  if (logImageUrls.has(path)) return logImageUrls.get(path);
  const token = await db.get("kv", "token");
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
  const res = await fetch(`/api/logs/${path}`, { headers });
  if (!res.ok) throw new Error(res.statusText);
  const url = URL.createObjectURL(await res.blob());
  logImageUrls.set(path, url);
  return url;
}

// Put the picture in once it has loaded (the rows are rendered before their images arrive).
function fillLogImages(root) {
  root.querySelectorAll("img[data-log-img]").forEach(async (img) => {
    try { img.src = await logImageUrl(img.dataset.logImg); img.hidden = false; } catch { img.closest(".dbg-thumb")?.classList.add("none"); }
  });
}

const debugState = { op: "", item: "", id: null };

function modelsOf(x) {
  const used = (x.used || []).filter((u) => u !== "ocr" && u !== "rules" && u !== "link" && u !== "readability");
  return used.length ? used.join(" → ") : (x.models || []).join(", ") || "no model";
}

function debugRowHtml(x) {
  const when = new Date(x.started);
  const ok = x.outcome === "ok" ? "ok" : x.outcome === "queued_for_batch" || x.outcome === "running" ? "" : "bad";
  const input = x.input || {};
  const thumb = input.file ? `<img data-log-img="${esc(x.id)}/image/${esc(input.file)}" alt="" hidden>` : "";
  const sub = input.kind === "url" ? (input.source_url || "").replace(/^https?:\/\/(www\.)?/, "") : "screenshot";
  return `<button type="button" class="dbg-row ${debugState.id === x.id ? "active" : ""}" data-debug-open="${esc(x.id)}">
    <span class="dbg-thumb">${thumb}<i>${input.kind === "url" ? "🔗" : "🖼"}</i></span>
    <span class="dbg-main"><b>${esc((x.card && x.card.title) || x.title || sub || x.item_id || "—")}</b>
      <small>${esc(OP_LABEL[x.operation] || x.operation)} · ${esc(when.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</small>
      <small class="dbg-model">${esc(modelsOf(x))}${x.cost_usd ? ` · ${esc(formatUsd(x.cost_usd))}` : ""}</small></span>
    <span class="pill ${ok}">${esc(x.outcome.replace(/_/g, " "))}</span></button>`;
}

async function showDebug(opts = {}) {
  if (opts.item !== undefined) debugState.item = opts.item || "";
  const dlg = $("#detail");
  dlg.dataset.id = "";
  dlg.innerHTML = `<div class="debug-panel" data-view="list"><button class="btn close" data-action="close" aria-label="Close">✕</button>
    <header class="dbg-head"><h2>Debug · conversations</h2>
      <select id="dbg-op" aria-label="Operation"><option value="">All operations</option>${Object.entries(OP_LABEL).map(([k, v]) => `<option value="${k}" ${debugState.op === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
      ${debugState.item ? `<button class="btn small" type="button" data-debug-clear-item>Only this item ✕</button>` : ""}
      <button class="btn small" type="button" data-debug-refresh>↻</button></header>
    <div class="dbg-body"><div id="dbg-list" class="dbg-list"><p class="meta-line">Loading…</p></div>
      <div id="dbg-detail" class="dbg-detail"><p class="meta-line">Pick a conversation to see what went in, what was said and what came out.</p></div></div></div>`;
  if (!dlg.open) dlg.showModal();
  await loadDebugList();
  if (opts.open) openDebug(opts.open);
}

async function loadDebugList() {
  const list = $("#dbg-list");
  if (!list) return;
  try {
    const q = new URLSearchParams({ limit: "200" });
    if (debugState.op) q.set("operation", debugState.op);
    if (debugState.item) q.set("item", debugState.item);
    const res = await apiWithSetup(`/api/logs?${q}`);
    if (!res.debug && !res.enabled) { list.innerHTML = `<p class="meta-line">The server isn't recording conversations. Turn on Debug mode in Settings → Logging.</p>`; return; }
    list.innerHTML = res.sessions.length ? res.sessions.map(debugRowHtml).join("")
      : `<p class="meta-line">No conversations yet. Save a screenshot or a link, or re-analyze an item, and it shows up here.</p>`;
    fillLogImages(list);
  } catch (e) {
    list.innerHTML = `<p class="meta-line">${esc(e.message === "Cancelled" ? "Enter the setup code to see the history." : `Couldn't load it: ${errorMessage(e)}`)} <button class="link-btn" type="button" data-debug-refresh>Try again</button></p>`;
  }
}

function debugCardHtml(card) {
  if (!card) return `<p class="meta-line">No card was produced (it ended before a result).</p>`;
  const item = { ...card, image_url: card.image_url, status: card.status || "ready" };
  const facts = [["Category", card.category && typeName(card.category)], ["Confidence", card.confidence != null && `${card.confidence}%`],
    ["Verified", card.verified != null && (card.verified ? "yes" : "no")], ["Link", card.canonical_url], ["Image", card.image_url],
    ["Sources", (card.metadata?.sources || []).join(" → ")], ["Why", card.confidence_reason]].filter(([, v]) => v);
  return `<div class="dbg-card"><article class="card ${esc(item.status)}">${coverHtml(item)}
      <div class="card-text"><div class="title">${esc(cardTitle(item))}</div><div class="meta">${esc(cardMeta(item))}</div></div></article>
    <dl>${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${/^https?:/.test(String(v)) ? `<a href="${esc(v)}" target="_blank" rel="noopener noreferrer">${esc(v)}</a>` : esc(v)}</dd>`).join("")}</dl></div>
    ${card.summary ? `<p class="meta-line">${esc(card.summary)}</p>` : ""}
    <details><summary>Everything on the card</summary><pre>${esc(JSON.stringify(card, null, 1))}</pre></details>`;
}

async function openDebug(id) {
  debugState.id = id;
  document.querySelectorAll(".dbg-row").forEach((r) => r.classList.toggle("active", r.dataset.debugOpen === id));
  const panel = $(".debug-panel"), box = $("#dbg-detail");
  if (!box) return;
  panel.dataset.view = "detail";
  box.innerHTML = `<p class="meta-line">Loading…</p>`;
  let d;
  try { d = await apiWithSetup(`/api/logs/${id}`); } catch (e) { box.innerHTML = `<p class="meta-line">Couldn't open it: ${esc(errorMessage(e))}</p>`; return; }
  const input = d.input || {}, start = d.events.find((e) => e.event === "session_start") || {};
  const ocr = d.events.find((e) => e.event === "ocr");
  const reqs = d.events.filter((e) => e.event === "llm_request");
  const models = [...new Set(reqs.map((e) => `${PROVIDER_NAMES[e.provider] || e.provider} · ${e.model}`))];
  const live = state.items.has(d.item_id);
  box.innerHTML = `
    <div class="dbg-back"><button class="btn small" type="button" data-debug-back>← History</button>
      <b>${esc(OP_LABEL[d.operation] || d.operation)}</b> <span class="meta-line">${esc(new Date(d.started).toLocaleString())}</span>
      <span class="pill ${d.outcome === "ok" ? "ok" : d.outcome === "queued_for_batch" ? "" : "bad"}">${esc(d.outcome.replace(/_/g, " "))}</span>
      ${live ? `<button class="btn small" type="button" data-open-item="${esc(d.item_id)}">Open the item</button>` : ""}
      <button class="btn small" type="button" data-log-save="${esc(id)}" data-format="md">⬇ .md</button>
      <button class="btn small" type="button" data-log-save="${esc(id)}" data-format="jsonl">⬇ .jsonl</button></div>
    <section class="dbg-model-box"><h3>Model used</h3>
      <p><b>${esc(models.join("  →  ") || "no model call")}</b></p>
      <p class="meta-line">${d.used ? `Answer path: ${esc(d.used.join(" → "))}. ` : ""}${reqs.length} call${reqs.length === 1 ? "" : "s"}${d.duration_ms != null ? ` · ${(d.duration_ms / 1000).toFixed(1)} s` : ""}${d.cost_usd ? ` · ${esc(formatUsd(d.cost_usd))}` : ""}${start.escalate_below != null && start.analyzer === "hybrid" ? ` · hybrid, escalates below ${start.escalate_below}%` : ""}${d.error ? ` · ${esc(d.error)}` : ""}</p></section>
    <section><h3>Input</h3>
      ${input.kind === "url" ? `<p><a href="${esc(input.source_url)}" target="_blank" rel="noopener noreferrer">${esc(input.source_url)}</a></p>`
        : input.file ? `<img class="dbg-input" data-log-img="${esc(id)}/image/${esc(input.file)}" alt="The screenshot that was analyzed" hidden>`
        : `<p class="meta-line">The screenshot wasn't kept (turn on “Keep the screenshots in the logs”, or Debug mode).</p>`}
      ${input.note ? `<p class="meta-line">Your note: ${esc(input.note)}</p>` : ""}
      ${start.previous && start.previous.title && d.operation !== "analyze" ? `<p class="meta-line">Before: ${esc(start.previous.title)} (${esc(start.previous.category || "?")}, ${esc(start.previous.confidence ?? "?")}%)</p>` : ""}
      ${ocr && ocr.text ? `<details><summary>OCR text (${ocr.lines} lines)</summary><pre>${esc(ocr.text)}</pre></details>` : ""}</section>
    <section><h3>Conversation</h3>${d.events.filter((e) => !["card", "ocr"].includes(e.event)).map(logEventHtml).join("")}</section>
    <section><h3>Output card</h3>${debugCardHtml(d.card)}</section>`;
  fillLogImages(box);
  box.scrollTop = 0;
}

let bulkTimer = null;
const BULK_LABEL = { refresh: "Refreshing metadata", reanalyze: "Re-analyzing" };

async function pollBulk() {
  clearTimeout(bulkTimer);
  const box = $("#bulk-progress");
  if (!box) return;   // the settings dialog was closed
  let st;
  try { st = await api("/api/bulk", { timeout: 8000 }); } catch { return; }
  if (st.running) {
    const pct = st.total ? Math.round(st.done / st.total * 100) : 0;
    box.hidden = false;
    box.innerHTML = `<div class="row-between"><b>${BULK_LABEL[st.kind] || "Working"}: ${st.done} of ${st.total}${st.failed ? ` · ${st.failed} failed` : ""}</b>
      <button class="btn small" type="button" data-bulk-cancel>Stop</button></div><div class="gauge"><i style="width:${pct}%"></i></div>`;
    bulkTimer = setTimeout(pollBulk, 1500);
  } else if (st.total && st.finished && Date.now() / 1000 - st.finished < 600) {
    box.hidden = false;
    box.innerHTML = `<b>${st.cancelled ? "Stopped" : "Done"}: ${st.done} of ${st.total} ${st.kind === "refresh" ? "refreshed" : "sent for analysis"}${st.failed ? `, ${st.failed} failed` : ""}.</b>
      ${st.kind === "reanalyze" ? `<p class="meta-line" style="margin:4px 0 0">Claude batches can take up to an hour; the library updates by itself.</p>` : ""}`;
    requestSync();
    refreshCostPill(true);
  } else {
    box.hidden = true;
  }
}

async function startBulk(kind) {
  const scope = $("#bulk-scope").value, n = bulkScopes()[scope];
  if (!n) return toast("Nothing to do for that choice.");
  const skip = kind === "reanalyze" && $("#bulk-skip").checked;
  const per = state.usage?.per_screenshot_usd;
  const ok = kind === "refresh"
    ? confirm(`Refresh metadata for ${n} item${n > 1 ? "s" : ""}? It looks things up again and doesn't use your AI provider.`)
    : confirm(`Re-analyze ${n} item${n > 1 ? "s" : ""} with your AI provider?${per ? `\n\nThat costs about ${formatUsd(n * per)} at your average so far.` : "\n\nThis uses your AI provider and costs money."}${skip ? "\nItems you corrected or confirmed are skipped." : "\nItems you corrected or confirmed will be replaced."}`);
  if (!ok) return;
  const body = JSON.stringify({ scope, skip_confirmed: skip });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const headers = { "Content-Type": "application/json" };
      if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
      await api(`/api/bulk/${kind === "refresh" ? "refresh-metadata" : "reanalyze"}`, { method: "POST", headers, body });
      trackBulk();
      return pollBulk();
    } catch (e) {
      let detail = {};
      try { detail = JSON.parse(e.message); } catch {}
      if (e instanceof HttpError && e.status === 403 && detail.code === "setup_code_required") {
        const code = prompt(`${setupCode ? "That setup code didn't match. " : ""}${detail.message}\n\nSetup code:`);
        if (!code) return toast("Not started.");
        setupCode = code.trim();
        continue;
      }
      return toast(e instanceof HttpError && e.status === 409 ? "A library job is already running." : `Couldn't start: ${errorMessage(e)}`);
    }
  }
}

async function cancelBulk() {
  const headers = {};
  if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
  try { await api("/api/bulk/cancel", { method: "POST", headers }); toast("Stopping after the items in progress…"); } catch (e) { toast(`Couldn't stop: ${errorMessage(e)}`); }
}

// ---- search in settings: type part of a name, in any order ("tmdb key", "budget", "escal conf") --------

// A word matches a setting's name loosely (letters in order, close together: "mnthly bdget" finds "Monthly budget")
// and its description only as written, so a long description doesn't match every short word.
function settingMatches(query, name, help) {
  const n = fold(name), h = fold(help);
  return fold(query).split(/\s+/).filter(Boolean).every((token) => {
    const m = fuzzyOne(token, n);
    if (m) return token.length < 3 && !n.includes(token) ? false : (m.pos.at(-1) - m.pos[0] + 1) <= token.length * 2 + 1;
    return h.includes(token);
  });
}

function searchSettings(query) {
  const root = $(".settings-content");
  if (!root) return;
  const q = query.trim();
  const sections = [...root.querySelectorAll(".settings-section")];
  root.querySelectorAll(".search-hit").forEach((el) => el.classList.remove("search-hit", "search-miss"));
  root.querySelectorAll(".search-miss").forEach((el) => el.classList.remove("search-miss"));
  $("#settings-none").hidden = true;
  if (!q) {   // back to the normal, one-section view
    root.classList.remove("searching");
    selectSettingsTab(settingsTab);
    return;
  }
  root.classList.add("searching");
  document.querySelectorAll("[data-settings-tab]").forEach((b) => b.setAttribute("aria-selected", "false"));
  let total = 0;
  for (const sec of sections) {
    sec.hidden = false;
    let hits = 0;
    for (const row of sec.querySelectorAll(".setting, .bulk-card")) {
      const hidden = row.closest("[hidden]") !== sec && row.closest("[hidden]");   // e.g. the local-server block in non-hybrid mode
      const env = (row.querySelector("[id^='set-']")?.id || row.querySelector("[name]")?.name || "").replace(/^set-/, "");
      const name = `${row.querySelector("label, h4")?.textContent || ""} ${env} ${sec.dataset.section}`;
      const hit = !hidden && settingMatches(q, name, row.querySelector(".setting-help, p")?.textContent || "");
      row.classList.toggle("search-miss", !hit);
      if (hit) { hits++; row.closest("details")?.setAttribute("open", ""); }
    }
    // notes and the section heading only matter where something matched
    sec.classList.toggle("search-miss", !hits);
    total += hits;
  }
  const none = $("#settings-none");
  none.hidden = total > 0;
  none.textContent = `No setting matches “${q}”.`;
}

let settingsData = null;

async function showSettings(errors = {}, typed = {}) {
  const dlg = $("#detail");
  dlg.dataset.id = "";
  if (!settingsData || !Object.keys(errors).length) {
    dlg.innerHTML = `<div class="settings-panel"><header class="settings-head"><h2>Settings</h2><button class="btn close" data-action="close" aria-label="Close">✕</button></header><div class="settings-content"><section class="settings-section"><h3>Appearance</h3>${appearanceHtml()}</section><p class="setting-note server-state">Loading server settings…</p></div></div>`;
    if (!dlg.open) dlg.showModal();
    try {
      settingsData = await api("/api/settings");
    } catch (e) {
      const msg = e instanceof HttpError && e.status === 401
        ? "This server needs an access token first."
        : e instanceof HttpError && e.status === 404
          ? "This server is too old to change settings from here. Update it, or edit its .env file."
          : `Can't reach the server (${e.message}). Settings can only be changed while connected.`;
      dlg.querySelector(".server-state").textContent = msg;
      return;
    }
  }
  dlg.innerHTML = settingsHtml(settingsData, errors, typed);
  if (!dlg.open) dlg.showModal();
  pollBulk();   // a library job started earlier shows its progress
  if ($("#provider-form")) {
    const shown = settingsData.providers.find((x) => x.id === $("#ot-provider").value);
    $("#ot-provider").dataset.shown = shown.id;
    if (settingEntry(settingsData, shown.key_env)?.is_set || shown.id === "local") loadModels("ot", true);
    if ($('[data-block="local"]:not([hidden])') && $("#lc-url").value) loadModels("lc", true);
  }
  autoCheckPlugin();
  const firstBad = Object.keys(errors).map((env) => document.getElementById(focusIdFor(env))).find(Boolean);
  if (firstBad) { selectSettingsTab(firstBad.closest(".settings-section").dataset.section); firstBad.scrollIntoView({ block: "center" }); firstBad.focus(); }
}

async function saveSettings(form) {
  pendingProvider = form.elements.ot_provider?.value || null;
  const changes = providerChanges(form, settingsData);
  for (const group of settingsData.groups) {
    for (const s of group.settings) {
      if (PROVIDER_ENVS.has(s.env) || s.env in changes) continue;
      const el = form.elements[s.env];
      if (!el) continue;
      const value = el.value.trim();
      if (s.kind === "secret") {
        if (value) changes[s.env] = value;   // blank = keep the current secret
        continue;
      }
      const current = String("saved" in s ? s.saved : s.value ?? "");
      if (value !== current) changes[s.env] = value === "" ? null : value;
    }
  }
  if (!Object.keys(changes).length) { toast("No changes to save."); $("#detail").close(); return; }
  await putSettings(changes, { close: true });
}

// A key that was just saved is tested straight away, so a typo shows up now rather than on the next screenshot.
const KEY_PROVIDER = { ANTHROPIC_API_KEY: "claude", OPENAI_API_KEY: "openai", GEMINI_API_KEY: "gemini",
  OPENROUTER_API_KEY: "openrouter", GROQ_API_KEY: "groq", LOCAL_LLM_API_KEY: "local" };

async function verifyChangedKeys(changes) {
  const results = [];
  for (const env of Object.keys(changes)) {
    const p = KEY_PROVIDER[env] && changes[env] && settingsData.providers.find((x) => x.id === KEY_PROVIDER[env]);
    if (!p) continue;
    try {
      const res = await verifyKey(p);
      results.push({ ok: true, text: `✓ ${p.label}: the key works${res.limits ? " — " + limitsText(res.limits) : ""}.` });
    } catch (e) {
      results.push({ ok: false, text: `✗ ${p.label}: ${e.message}` });
    }
  }
  return results;
}

let setupCode = null;  // this server's setup code (only needed while it has no access token)

async function putSettings(changes, { close = false } = {}) {
  try {
    const headers = { "Content-Type": "application/json" };
    if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
    settingsData = await api("/api/settings", { method: "PUT", headers, body: JSON.stringify({ changes }) });
    pluginCatalog = null; pluginActions.clear(); pluginFetched.clear();   // a plugin may have just been set up
  } catch (e) {
    if (e instanceof HttpError && e.status === 403) {
      let detail = {};
      try { detail = JSON.parse(e.message); } catch {}
      if (detail.code === "setup_code_required") {
        const again = setupCode ? "That setup code didn't match. " : "";
        const code = prompt(`${again}${detail.message}\n\nSetup code:`);
        if (!code) return toast("Settings not saved.");
        setupCode = code.trim();
        return putSettings(changes, { close });
      }
    }
    if (e instanceof HttpError && e.status === 422) {
      let errors = {};
      try { errors = JSON.parse(e.message).errors || {}; } catch {}
      toast("Some values are invalid — see the highlighted fields.");
      return showSettings(Object.keys(errors).length ? errors : { _: e.message }, changes);
    }
    return toast(`Couldn't save settings: ${e.message}`);
  }
  const checks = await verifyChangedKeys(changes);
  // A new access token applies to this device too.
  // (several tokens may be listed: this device uses the first)
  const firstToken = (changes.MAGPIE_API_TOKEN || "").split(/[,\s]+/).find(Boolean);
  if (firstToken) await setToken(firstToken);
  state.server = settingsData.status;
  render();   // e.g. a new verification threshold changes which cards need checking
  renderServerStatus();
  renderSyncStatus();
  toast(["Settings saved.", ...(settingsData.notices || []), ...checks.map((c) => c.text)].join("  "));
  // Closing is for "Save changes". A key the provider rejected keeps the panel open, which shows which one.
  if (close && checks.every((c) => c.ok)) { $("#detail").close(); return; }
  showSettings();
}

function setSyncState(s, error = null) {
  state.sync = s;
  state.syncError = error;
  renderSyncStatus();
}

// ---- rendering -------------------------------------------------------------

function screenshotFor(item) {
  return blobUrls.get(item.id) || (item.image_file ? `/media/${encodeURIComponent(item.image_file)}` : null);
}
function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function searchBlob(item) {
  const parts = [item.title, item.subtitle, item.summary, item.note, item.category, item.source_platform, ...(item.tags || []), ...(item.related || []).map((r) => r.label)];
  const walk = (v) => {
    if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
    else if (typeof v === "string" && !v.startsWith("http")) parts.push(v);
    else if (typeof v === "number") parts.push(String(v));
  };
  walk(item.metadata || {});
  return fold(parts.filter(Boolean).join(" ").replace(/-/g, " ") + " " + (item.tags || []).join(" "));
}

// Ready items that no metadata source (TMDB, GitHub, ...) confirmed and the user hasn't corrected.
// Worked out here from the item's own fields (same rule as the server), so items saved before the rule existed or changed are judged too.
const VERIFYING_SOURCES = new Set(["github", "tmdb", "tmdb+omdb", "omdb", "openlibrary", "schema.org/Recipe", "npm", "huggingface"]);
const verifiedFrom = () => state.server?.verified_confidence ?? 90;   // the user's threshold (Settings → Verification)
const isVerified = (item) => !!item.corrected || !!item.confirmed || (item.confidence ?? 0) >= verifiedFrom() || (item.metadata?.sources || []).some((x) => VERIFYING_SOURCES.has(x));
const isUnverified = (item) => item.status === "ready" && !isVerified(item);
// A failed analysis needs the user too: retry it, or fix it by hand.
const needsCheck = (item) => item.status === "error" || item.needs_review || isUnverified(item);

// "#tag" words in the search box are tag filters, the rest is text.
function searchTerms() {
  const parts = fold(state.q).split(/[^\p{L}\p{N}#_+.-]+/u).filter(Boolean);
  return { words: parts.filter((w) => !w.startsWith("#")).flatMap((w) => w.split(/[^\p{L}\p{N}]+/u)).filter(Boolean),
           tags: [...state.tags, ...parts.filter((w) => w.startsWith("#") && w.length > 1).map((w) => w.slice(1))] };
}

function filteredItems() {
  const { words, tags } = searchTerms();
  return [...state.items.values()]
    .filter((item) => {
      if (state.tab !== "all" && tabOf(item) !== state.tab) return false;
      if (state.show === "check" && !needsCheck(item)) return false;
      if (tags.some((t) => !(item.tags || []).some((x) => x === t || x.startsWith(t)))) return false;
      if (!words.length) return true;
      const tokens = searchBlob(item).split(/[^\p{L}\p{N}]+/u);
      return words.every((w) => tokens.some((t) => t.startsWith(w)));
    })
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
}

function cardFacts(item) {
  const m = item.metadata || {};
  const facts = [];
  if (m.year && !["github_repo", "recipe"].includes(item.category)) facts.push(String(m.year));
  if (m.imdb_rating) facts.push(`★ ${String(m.imdb_rating).replace("/10", "")}`);
  else if (m.tmdb_rating) facts.push(`★ ${String(m.tmdb_rating).replace("/10", "")}`);
  if (m.stars != null) facts.push(`★ ${Number(m.stars).toLocaleString()}`);
  if (m.programming_language) facts.push(m.programming_language);
  if (m.total_time) facts.push(m.total_time);
  if (m.reading_time) facts.push(String(m.reading_time).replace(" read", ""));
  if (m.rating && item.category === "recipe") facts.push(`★ ${m.rating}`);
  return facts;
}

function formatUsd(v) {
  if (!v) return "$0";
  return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(v < 1 ? 3 : 2)}`;
}

// ---- model limits --------------------------------------------------------------
// What each provider/model in use has left, and a model that hit its limit with when it's back.

const LIMIT_KINDS = [["requests", "Requests"], ["requests_minute", "Requests"], ["tokens", "Tokens"], ["input_tokens", "Input tokens"], ["output_tokens", "Output tokens"]];
const REASON_TEXT = { "rate limit": "rate limit", "daily quota": "daily quota", credit: "credit or billing quota" };

function clockTime(iso) {
  const d = new Date(iso), today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString([], today ? { hour: "numeric", minute: "2-digit" } : { weekday: "short", hour: "numeric", minute: "2-digit" });
}

// "in 40s", "in 12 min", "at 14:05", "at Thu 09:00"
function untilText(iso) {
  const secs = (Date.parse(iso) - Date.now()) / 1000;
  if (!(secs > 0)) return "now";
  if (secs < 90) return `in ${Math.ceil(secs)}s`;
  if (secs < 3600) return `in ${Math.round(secs / 60)} min`;
  return `at ${clockTime(iso)}`;
}

const modelLabel = (r) => `${PROVIDER_NAMES[r.provider] || r.provider}${r.model ? ` · ${r.model.replace(/^models\//, "")}` : ""}`;
const isPaused = (r) => r.blocked_until && Date.parse(r.blocked_until) > Date.now();

function lowestBucket(r) {
  let best = null;
  for (const [k, label] of LIMIT_KINDS) {
    const b = r[k];
    if (b && b.limit && b.remaining != null && (!best || b.remaining / b.limit < best.b.remaining / best.b.limit)) best = { k, label, b };
  }
  return best;
}

function bucketPer(k, b) { return b.per ? ` per ${b.per}` : k === "requests_minute" ? " per minute" : ""; }

function limitRowHtml(r) {
  const paused = isPaused(r);
  const status = paused ? `<span class="pill bad" title="Paused until ${esc(clockTime(r.blocked_until))}">Limit reached · back ${esc(untilText(r.blocked_until))}</span>`
    : r.low ? `<span class="pill warn">Running low</span>` : `<span class="pill ok">OK</span>`;
  const bars = LIMIT_KINDS.filter(([k]) => r[k] && r[k].remaining != null).map(([k, label]) => {
    const b = r[k], share = b.limit ? Math.max(0, Math.min(1, b.remaining / b.limit)) : null;
    const n = (v) => Number(v).toLocaleString();
    return `<div class="limit-bucket"><span>${esc(label + bucketPer(k, b))}</span>
      ${share != null ? `<span class="bar2 ${share < 0.1 ? "low" : ""}"><i style="width:${(share * 100).toFixed(1)}%"></i></span>` : "<span></span>"}
      <b>${esc(n(b.remaining))}${b.limit ? ` of ${esc(n(b.limit))}` : ""} left</b>
      <small>${b.reset_at ? `resets ${esc(untilText(b.reset_at))}` : ""}</small></div>`;
  }).join("");
  const credit = r.credit && r.credit.remaining != null ? `<div class="meta-line">$${Number(r.credit.remaining).toFixed(2)} credit left${r.credit.limit ? ` of $${Number(r.credit.limit).toFixed(2)}` : ""}</div>` : "";
  const note = paused ? `<div class="meta-line">Hit its ${esc(REASON_TEXT[r.blocked_reason] || r.blocked_reason)}. Magpie doesn't call it until ${esc(clockTime(r.blocked_until))}; screenshots saved meanwhile are retried automatically then.</div>`
    : r.estimated ? `<div class="meta-line">Estimated from the provider's published free-tier limits and the requests Magpie made. On a paid tier? Set yours with MAGPIE_RATE_LIMITS.</div>` : "";
  return `<div class="limit-row ${paused ? "paused" : r.low ? "low" : ""}"><div class="limit-head"><b>${esc(modelLabel(r))}</b>${r.current ? ' <span class="pill">in use</span>' : ""} ${status}</div>
    ${bars || (credit ? "" : `<div class="meta-line">Nothing reported yet: it appears after the next analysis or a key test.</div>`)}${credit}${note}</div>`;
}

function limitsPanel(c) {
  const rows = Array.isArray(c.limits) ? c.limits : [];
  return `<h3>What's left</h3>${rows.length ? `<div class="limit-list">${rows.map(limitRowHtml).join("")}</div>`
    : `<p class="meta-line">Providers report their remaining rate limits (and, for OpenRouter, credit) with each answer. Nothing seen yet: it appears after the next analysis or a key test. Billing balances aren't available from any provider's API.</p>`}`;
}

// The header warning: a model in use that hit its limit (and when it's back), or is about to.
function renderLimitPill() {
  const b = $("#limit-pill");
  if (!b) return;
  const rows = (state.limits?.models || []).filter((r) => r.current);
  const paused = rows.find(isPaused);
  const low = !paused && rows.find((r) => r.low);
  if (!paused && !low) { b.hidden = true; return; }
  const waiting = state.limits?.waiting || 0;
  if (paused) {
    b.className = "pill-btn limit-pill paused";
    b.innerHTML = `<span aria-hidden="true">⏳</span><span class="lp-text">${esc(PROVIDER_NAMES[paused.provider] || paused.provider)}<span class="lp-mid"> limit · back</span> ${esc(untilText(paused.blocked_until))}</span>`;
    b.title = `${modelLabel(paused)} reached its ${REASON_TEXT[paused.blocked_reason] || paused.blocked_reason}. Available again ${untilText(paused.blocked_until)} (${clockTime(paused.blocked_until)}).`
      + (waiting ? ` ${waiting} item${waiting > 1 ? "s" : ""} will be retried automatically then.` : "") + " Click for details.";
  } else {
    const lb = lowestBucket(low);
    b.className = "pill-btn limit-pill low";
    b.innerHTML = `<span aria-hidden="true">⚠</span><span class="lp-text">${esc(PROVIDER_NAMES[low.provider] || low.provider)}: ${esc(Number(lb.b.remaining).toLocaleString())} ${esc(lb.label.toLowerCase())} left</span>`;
    b.title = `${modelLabel(low)} is close to its limit: ${lb.b.remaining} of ${lb.b.limit} ${lb.label.toLowerCase()}${bucketPer(lb.k, lb.b)} left${lb.b.reset_at ? `, resets ${untilText(lb.b.reset_at)}` : ""}. Click for details.`;
  }
  b.hidden = false;
}

let limitsAt = 0;
async function refreshLimits(force = false) {
  if (!force && Date.now() - limitsAt < 30000) return;
  limitsAt = Date.now();
  try { state.limits = await api("/api/limits", { timeout: 8000 }); } catch { return; }   // older server, or offline
  renderLimitPill();
}
// Keep the countdown current, and look again once a pause should be over.
setInterval(() => {
  renderLimitPill();
  const due = (state.limits?.models || []).some((r) => r.blocked_until && Date.parse(r.blocked_until) <= Date.now());
  if (due) refreshLimits(true);
}, 15000);

const PROVIDER_NAMES = { claude: "Claude", openai: "OpenAI", gemini: "Gemini", openrouter: "OpenRouter", groq: "Groq" };
const providerName = (c) => PROVIDER_NAMES[c.provider] || "Claude";

const USAGE_KINDS = [["local", "Your own model"], ["batch", "Claude, batched"], ["realtime", "Claude, real time"], ["hosted", "Other providers"]];

function usageKindOf(mode) { return ["local", "batch", "realtime", "hosted"].includes(mode) ? mode : "local"; }

function renderCostPill() {
  const b = $("#cost-btn"), r = state.usage;
  if (!r) { b.hidden = true; return; }
  const c = r.config;
  b.innerHTML = c.monthly_budget_usd
    ? `<span class="meter"><span style="width:${Math.min(100, c.month_spent_usd / c.monthly_budget_usd * 100)}%"></span></span>${esc(formatUsd(c.month_spent_usd))} of ${esc(formatUsd(c.monthly_budget_usd))}`
    : `${esc(formatUsd(c.month_spent_usd))} this month`;
  b.title = c.monthly_budget_usd ? "Spent this month of your budget. Click for the full breakdown." : "Spent this month. Click for the full breakdown.";
  b.hidden = false;
}

let costPillAt = 0;
async function refreshCostPill(force = false) {
  if (!force && Date.now() - costPillAt < 60000) return;
  costPillAt = Date.now();
  try { state.usage = await api("/api/usage?days=30", { timeout: 8000 }); renderCostPill(); } catch { /* older server, or offline: keep what we have */ }
}

async function downloadUsageCsv(days) {
  try {
    const token = await db.get("kv", "token");
    const res = await fetch(`/api/usage.csv?days=${days}`, { headers: token ? { Authorization: `Bearer ${token}` } : {}, cache: "no-store" });
    if (!res.ok) throw new Error(res.statusText);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = `magpie-usage-${days}d.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } catch (e) { toast(`Couldn't export: ${e.message}`); }
}

async function showUsage(days = 30) {
  const dlg = $("#detail");
  dlg.dataset.id = "";
  dlg.innerHTML = `<div class="usage-panel"><button class="btn close" data-action="close" aria-label="Close">✕</button><h2>Usage & cost</h2><p class="meta-line">Loading…</p></div>`;
  if (!dlg.open) dlg.showModal();
  let r;
  try { r = await api(`/api/usage?days=${days}`); } catch (e) {
    dlg.querySelector(".meta-line").textContent = `Needs a connection to the server (${e.message}).`;
    return;
  }
  state.usage = { ...r, config: r.config }; renderCostPill();
  const t = r.totals, c = r.config;
  // one column per day of the period, stacked by who answered
  const byDay = {};
  for (const x of r.by_day_mode) (byDay[x.day] ||= {})[usageKindOf(x.mode)] = x.cost_usd;
  const cols = Array.from({ length: days }, (_, i) => { const d = new Date(Date.now() - (days - 1 - i) * 864e5).toISOString().slice(0, 10); return [d, byDay[d] || {}]; });
  const max = Math.max(...cols.map(([, k]) => Object.values(k).reduce((a, b) => a + b, 0)), 0.0001);
  const dayShots = Object.fromEntries(r.by_day.map((d) => [d.day, d.screenshots]));
  const budget = c.monthly_budget_usd, spent = c.month_spent_usd;
  const projected = r.projected_30d_usd;
  const paidShare = Math.round((r.cloud_share ?? r.claude_share) * 100);
  const maxCat = Math.max(...r.by_category.map((x) => x.cost_usd), 0.0001);
  dlg.innerHTML = `
    <div class="usage-panel">
      <div class="panel-head"><h2>Usage & cost</h2>
        <div class="seg" role="group" aria-label="Period">${[7, 30, 90].map((d) => `<button type="button" data-usage-days="${d}" aria-pressed="${d === days}">${d} days</button>`).join("")}</div>
        <button class="btn close" data-action="close" aria-label="Close">✕</button></div>
      <div class="kpis">
        <div class="kpi"><b>${esc(formatUsd(t.cost_usd))}</b><span>total</span></div>
        <div class="kpi"><b>${esc(formatUsd(r.per_screenshot_usd))}</b><span>per screenshot</span></div>
        <div class="kpi"><b>${t.screenshots}</b><span>screenshots</span></div>
        <div class="kpi"><b>${paidShare}%</b><span>sent to ${esc(providerName(c))}</span></div>
        <div class="kpi"><b>${esc(formatUsd(r.saved_by_local_usd || 0))}</b><span>saved by answering on your own model</span></div>
      </div>
      ${budget ? `<div class="group"><h3>This month's budget</h3>
        <div class="gauge"><i style="width:${Math.min(100, spent / budget * 100)}%"></i><u style="left:${Math.min(100, projected / budget * 100)}%" title="Projected"></u></div>
        <div class="row-between"><span><b>${esc(formatUsd(spent))}</b> of ${esc(formatUsd(budget))} used</span><span class="meta-line">Projected ${esc(formatUsd(projected))} at this pace. Paid models pause at ${esc(formatUsd(budget))}.</span></div></div>`
        : `<p class="meta-line">Projected ${esc(formatUsd(projected))} over 30 days. Set a monthly budget under Settings → Spending limits to cap it.</p>`}
      ${r.by_day.length ? `<div><h3>Spend per day</h3><div class="chart" role="img" aria-label="Spend per day">${cols.map(([d, k]) => {
        const total = Object.values(k).reduce((a, b) => a + b, 0);
        return `<div class="col" title="${esc(d)}: ${esc(formatUsd(total))}, ${dayShots[d] || 0} screenshot(s)">${USAGE_KINDS.map(([kind]) => `<i class="k-${kind}" style="height:${(k[kind] || 0) / max * 100}%"></i>`).join("")}</div>`; }).join("")}</div>
        <div class="key">${USAGE_KINDS.map(([kind, label]) => `<span><i class="k-${kind}"></i>${esc(label)}</span>`).join("")}</div></div>` : ""}
      <div class="two">
        <div><h3>Where answers came from</h3><div class="funnel">
          <div><span>All screenshots</span><b>${t.screenshots}</b></div>
          <div class="local"><span>Answered on your own model</span><b>${Math.max(0, t.screenshots - r.paid_screenshots)}</b></div>
          <div class="paid"><span>Sent to a paid model</span><b>${r.paid_screenshots}</b></div></div></div>
        <div>${limitsPanel(c)}</div>
      </div>
      <div><h3>By model</h3><div class="tbl"><table class="usage-table"><thead><tr><th>Model</th><th>Runs</th><th>Avg</th><th>Total</th><th>Tokens in / out</th><th>Searches</th><th>Avg time</th></tr></thead><tbody>
        ${r.by_analyzer.map((a) => `<tr><td>${esc(a.analyzer)}${a.model ? ` <span class="meta-line">${esc(a.model)}</span>` : ""} <span class="meta-line">${esc(a.mode || "")}</span></td>
          <td>${a.runs}${a.failures ? ` <span class="meta-line">(${a.failures} failed)</span>` : ""}</td><td>${esc(formatUsd(a.avg_cost_usd))}</td><td>${esc(formatUsd(a.cost_usd))}</td>
          <td>${(a.input_tokens || 0).toLocaleString()} / ${(a.output_tokens || 0).toLocaleString()}</td><td>${a.web_searches || 0}</td>
          <td>${a.avg_duration_ms ? (a.avg_duration_ms / 1000).toFixed(1) + " s" : "—"}</td></tr>`).join("") || `<tr><td colspan="7" class="meta-line">No analyses yet.</td></tr>`}
      </tbody></table></div></div>
      <div class="two">
        <div><h3>By type</h3><table class="usage-table"><tbody>${r.by_category.map((x) => `<tr><td>${esc(x.category === "deleted" ? "Deleted items" : typeName(x.category))}</td><td>${esc(formatUsd(x.cost_usd))}</td>
          <td style="width:36%"><div class="bar2"><i style="width:${x.cost_usd / maxCat * 100}%"></i></div></td></tr>`).join("") || `<tr><td class="meta-line">Nothing yet.</td></tr>`}</tbody></table></div>
        <div><h3>Most expensive items</h3><table class="usage-table"><tbody>${r.top_items.map((x) => `<tr><td>${state.items.has(x.item_id)
          ? `<button class="link-btn" data-open-item="${esc(x.item_id)}">${esc(x.title || "Untitled")}</button>` : esc(x.title || "Deleted item")}<div class="meta-line">${esc(typeName(x.category))} · ${x.runs} run${x.runs > 1 ? "s" : ""}</div></td>
          <td>${esc(formatUsd(x.cost_usd))}</td></tr>`).join("") || `<tr><td class="meta-line">Nothing yet.</td></tr>`}</tbody></table></div>
      </div>
      <div class="row"><button class="btn" type="button" data-usage-csv="${days}">Export CSV</button><span class="meta-line">One row per model call, for your own spreadsheet. Costs use list prices.</span></div>
      <p class="meta-line">Settings: ${esc(c.analyzer)} · ${esc(c.provider_model || c.claude_model)}${c.provider && c.provider !== "claude" ? "" : ` · effort ${esc(c.effort)} · batch ${c.claude_batch ? "on" : "off"}`}${c.analyzer === "hybrid" ? ` · escalate below ${c.escalate_below}%` : ""}.</p>
    </div>`;
}

function cardTitle(item) {
  if (item.title) return item.title;
  if (item.batch_pending) return "Queued for analysis (batch)";
  if (item.status === "error" && item.retry_at) return "Waiting for the AI model's limit";
  return { queued: "Waiting to upload", processing: "Analyzing screenshot…", error: "Couldn't identify — open to retry" }[item.status] || "Untitled";
}

function render() {
  renderFilters();
  renderGrid();
  renderSyncStatus();
  const dlg = $("#detail");
  if (dlg.open && dlg.dataset.id) {  // item details (the usage panel has no id and doesn't re-render)
    const item = state.items.get(dlg.dataset.id);
    if (item) renderDetail(item); else dlg.close();
  }
}

function renderFilters() {
  const items = [...state.items.values()];
  const counts = { all: items.length };
  for (const i of items) counts[tabOf(i)] = (counts[tabOf(i)] || 0) + 1;
  if (state.tab !== "all" && !counts[state.tab]) state.tab = "all";
  $("#tabs").innerHTML = TABS.filter((t) => t.id === "all" || counts[t.id]).map((t) => `
    <button role="tab" data-tab="${t.id}" aria-selected="${state.tab === t.id}">${t.cats ? `<span class="tabicon t-${themeOf(t.cats[0])}">${typeIcon(themeOf(t.cats[0]))}</span>` : ""}${esc(t.label)}<span class="count">${counts[t.id] || 0}</span></button>`).join("");
  const check = items.filter(needsCheck).length;
  $("#check-count").textContent = check || "";
  document.querySelectorAll("[data-show]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.show === state.show)));
  document.querySelectorAll("[data-layout]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.layout === state.layout)));

  const tagCounts = {};
  for (const i of items) for (const t of i.tags || []) tagCounts[t] = (tagCounts[t] || 0) + 1;
  const f = fold($("#tag-filter").value || "").replace(/^#/, "");
  $("#tags").innerHTML = Object.entries(tagCounts).filter(([t]) => fold(t).includes(f))
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 80).map(([t, n]) => `
    <button class="chip ${state.tags.includes(t) ? "active" : ""}" type="button" data-tag="${esc(t)}" aria-pressed="${state.tags.includes(t)}">#${esc(t)} <span class="count">${n}</span></button>`).join("")
    || `<span class="hint">No tags yet. Add them from an item's details.</span>`;
  $("#tag-btn").innerHTML = `#<span class="lbl"> Tags</span>${state.tags.length ? `<span class="count tagn">${state.tags.length}</span>` : ""}`;
}

// The title set on a generated cover, like a book jacket: repos show the owner small above the name.
function coverTitleHtml(item) {
  const text = item.title || hostOf(item.source_url) || cardTitle(item);
  const repo = item.category === "github_repo" && /^[^/\s]+\/[^/\s]+$/.test(text) ? text.split("/") : null;
  const main = repo ? repo[1] : text;
  // Two lines at most. Short titles are set big; longer ones smaller so that they fit two lines (a very long one is
  // cut with an ellipsis, and the full title is right under the card). The longest word must fit a line unbroken
  // (bold ≈ 0.72em a letter, 86% of the cover's width available).
  const longest = Math.max(...main.split(/[\s/_-]+/).map((w) => w.length), 1);
  const fitTwoLines = (2 * 86) / (0.68 * (main.length + 3));
  const size = Math.max(6, Math.min(17, fitTwoLines, 86 / (0.72 * longest))).toFixed(1);
  const lines = 2;
  return `<span class="cover-title" style="--fs:${size}cqw;--lines:${lines}" aria-hidden="true">${repo ? `<small>${esc(repo[0])}/</small>${esc(repo[1])}` : esc(text)}</span>`;
}

// The picture area of a card: the real poster/cover/header when there is one, else a cover themed for the type.
// Pictures that failed to load in this browser (hotlink protection, gone): shown as the themed cover instead.
const brokenImages = new Set();
document.addEventListener("error", (e) => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || !img.classList.contains("cover-img")) return;
  brokenImages.add(img.getAttribute("src"));
  img.closest(".cover")?.classList.remove("has-img", "wide-img", "logo-img");
  img.remove();
}, true);

function coverHtml(item, { chip = true } = {}) {
  // a real poster, cover or header; your own screenshot lives under "Original"
  const pic = safeUrl(item.image_url) && !brokenImages.has(safeUrl(item.image_url)) ? safeUrl(item.image_url) : null;
  // only the site's logo (no picture of the article): shown whole on a tile, with the title still on the cover
  const logo = !!pic && item.metadata?.image_kind === "logo";
  const wide = !!pic && !logo && WIDE_CATEGORIES.has(item.category);
  const backdrop = wide && item.metadata?.image_opaque === true;   // a transparent logo keeps the plain themed cover
  const busy = ["queued", "processing"].includes(item.status) || item.batch_pending || item.pending_upload;
  const flag = item.status === "error" && item.retry_at ? `<span class="flag warn" title="The AI model hit its limit; retried automatically ${esc(untilText(item.retry_at))}">⏳</span>`
    : item.status === "error" ? `<span class="flag err" title="Analysis failed">!</span>`
    : item.needs_review ? `<span class="flag warn" title="Not sure (${esc(item.confidence)}%). ${esc(item.confidence_reason || "")}">!</span>`
    : isUnverified(item) ? `<span class="flag unv" title="No source such as TMDB or GitHub confirmed this">○</span>` : "";
  const label = item.status === "error" ? (item.retry_at ? "Waiting" : "Failed") : busy ? (item.batch_pending ? "Queued" : "Analyzing") : typeName(item.category);
  return `<div class="cover t-${themeOf(item.category)} ${pic ? (logo ? "logo-img" : wide ? `wide-img${backdrop ? " bd" : ""}` : "has-img") : ""} ${busy ? "busy" : ""}">${
    // the themed cover sits underneath, so it shows if the picture never loads
    `${typeIcon(themeOf(item.category), "glyph")}${coverTitleHtml(item)}`}${
    // no-referrer: many sites refuse images to pages on other sites but serve them without a Referer
    // wide pictures are shown whole, over a blurred, enlarged copy of themselves (the same URL, so no extra download)
    backdrop ? `<img class="cover-bd" src="${esc(pic)}" alt="" aria-hidden="true" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ""}${
    pic ? `<img class="cover-img" src="${esc(pic)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ""}${
    chip ? `<span class="typechip">${typeIcon(themeOf(item.category))}<span class="tlabel">${esc(label)}</span></span>` : ""}${flag}</div>`;
}

function cardMeta(item) {
  if (item.status === "error" && item.retry_at) return `Waiting for limit · retry ${untilText(item.retry_at)}`;
  if (item.status !== "ready") return item.status === "error" ? "Open to retry" : item.pending_upload ? "Saved on this device" : "Working on it…";
  return [typeName(item.category), ...cardFacts(item).slice(0, 2)].join(" · ");
}

// How many columns fit: up to 4 on a phone (a card needs room to be read), 8 on a wide screen.
const maxColumns = () => (window.innerWidth < 760 ? 4 : 8);
window.addEventListener("resize", () => { if (state.cols) renderGrid(); });

function renderGrid() {
  const items = filteredItems();
  $("#empty").hidden = items.length > 0;
  const filtered = state.q || state.tab !== "all" || state.show !== "all" || state.tags.length;
  $("#empty").textContent = filtered ? "Nothing matches. Clear the search or switch tabs." : "Nothing here yet. Tap + Add, or paste a screenshot or link.";
  const cols = Math.min(state.cols, maxColumns());
  $("#grid").className = `grid ${state.layout === "list" ? "list" : cols ? `cols${cols >= 6 ? " dense" : ""}` : ""}`;
  $("#grid").style.setProperty("--cols", cols || "");
  $("#cols-ctl").hidden = state.layout === "list";
  const pick = $("#cols-select");   // Auto, then 1 up to what fits this screen
  const max = maxColumns();
  const wanted = ["0", ...Array.from({ length: max }, (_, i) => String(i + 1))];
  if (pick.options.length !== wanted.length) pick.innerHTML = wanted.map((v) => `<option value="${v}">${v === "0" ? "Auto" : v}</option>`).join("");
  pick.value = String(cols);
  $("#grid").innerHTML = items.map((item) => `
    <article class="card ${esc(item.status)}" data-id="${esc(item.id)}" tabindex="0" role="button" aria-label="${esc(cardTitle(item))}">
      ${coverHtml(item)}
      <div class="card-text"><div class="title">${esc(cardTitle(item))}</div><div class="meta">${esc(cardMeta(item))}</div>${item.status === "ready" && item.usage?.model ? `<div class="by-model" title="Resolved by ${esc(item.usage.model)}">${esc(item.usage.model)}</div>` : ""}</div>
      ${isUnverified(item) ? `<button class="verify-btn" type="button" data-verify="${esc(item.id)}" title="Mark this identification as correct">✓ Verify</button>` : ""}
    </article>`).join("");

  const active = [];
  state.tags.forEach((t) => active.push(`<span class="chip active">#${esc(t)}<button data-clear-tag="${esc(t)}" aria-label="Remove ${esc(t)}">×</button></span>`));
  $("#active-filters").innerHTML = active.join("");
}

function renderSyncStatus() {
  const pending = state.ops.length;
  const el = $("#sync-status");
  const labels = {
    syncing: ["⟳", "Syncing…"],
    offline: ["⚡︎", pending ? `Offline · ${pending} waiting` : "Offline"],
    auth: ["🔒", "Token needed"],
    error: ["⚠", "Sync error"],
    idle: pending ? ["⟳", `${pending} waiting`] : ["✓", "Synced"],
  };
  const [icon, text] = labels[state.sync] || labels.idle;
  el.className = `sync-status ${state.sync}`;
  el.innerHTML = `<span aria-hidden="true">${icon}</span><span class="sync-text">${esc(text)}</span>`;

  const banner = $("#banner");
  const serverErrors = state.sync !== "offline" && state.server ? state.server.problems.filter((p) => p.level === "error") : [];
  let html = "";
  banner.classList.toggle("error", serverErrors.length > 0 && state.sync !== "auth");
  if (state.sync === "auth") {
    html = `This Magpie server needs an access token. <button class="btn" data-action="token">Enter token</button>`;
  } else if (serverErrors.length) {
    html = `<span class="banner-msg"><span aria-hidden="true">⛔</span> ${esc(serverErrors[0].message)}${
      serverErrors.length > 1 ? ` <span class="meta-line">(+${serverErrors.length - 1} more)</span>` : ""
    }</span> <button class="btn" data-action="settings">Open settings</button>`;
  } else if (state.sync === "offline" && pending) {
    html = `You're offline. ${pending} change${pending > 1 ? "s" : ""} will sync automatically when the server is reachable.`;
  } else if (state.sync === "error") {
    html = `Sync failed: ${esc(state.syncError)} <button class="btn" data-action="sync">Retry</button>`;
  } else if (showInstallHint()) {
    html = `Install Magpie: tap <b>Share</b> <span aria-hidden="true">⎋</span> then <b>Add to Home Screen</b>. <button class="btn" data-action="dismiss-install">Dismiss</button>`;
  }
  banner.innerHTML = html;
  banner.hidden = !html;
}

function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}
function showInstallHint() {
  const iOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  try { if (localStorage.getItem("magpie.installHintDismissed")) return false; } catch {}
  return iOS && !isStandalone();
}

function scoresHtml(m) {
  const scores = [
    ["IMDb", m.imdb_rating, m.imdb_votes ? `${m.imdb_votes} votes` : ""],
    ["Rotten Tomatoes", m.rotten_tomatoes],
    ["Metacritic", m.metacritic],
    ["TMDB", m.tmdb_rating],
    ["GitHub stars", m.stars != null ? Number(m.stars).toLocaleString() : null],
    ["Rating", m.rating, m.rating_count ? `${m.rating_count} ratings` : ""],
  ].filter(([, v]) => v != null && v !== "");
  if (!scores.length) return "";
  return `<div class="scores">${scores.map(([label, v, extra]) =>
    `<div class="score"><b>${esc(v)}</b><small>${esc(label)}${extra ? `<br>${esc(extra)}` : ""}</small></div>`).join("")}</div>`;
}

function statusHtml(item) {
  if (item.status === "error" && item.retry_at) return `<div class="error-box waiting">⏳ ${esc(item.error)} Next try ${esc(untilText(item.retry_at))}.</div>`;
  if (item.status === "error") return `<div class="error-box">Analysis failed: ${esc(item.error)}</div>`;
  if (item.pending_upload) return `<div class="meta-line">⏳ Saved on this device. It will be ${item.kind === "url" ? "looked up" : "uploaded and identified"} when the server is reachable.</div>`;
  if (item.batch_pending) return `<div class="meta-line">⏳ Queued for Claude batch processing (half price). Usually done within minutes to an hour, at most 24 h.</div>`;
  if (item.status === "processing") return `<div class="meta-line">Analyzing… this usually takes 20–60 seconds.</div>`;
  if (hasPendingOps(item.id)) return `<div class="meta-line">⟳ Changes waiting to sync</div>`;
  return "";
}

function confidenceHtml(item, { fixButton = true } = {}) {
  if (item.pending_upload || item.status !== "ready") return "";
  const c = item.confidence;
  const level = item.corrected ? "ok" : c == null ? "unknown" : c >= 85 ? "ok" : c >= 60 ? "mid" : "low";
  const label = item.corrected ? "Corrected by you" : c == null ? "Confidence unknown" : `${c}% sure`;
  const alts = (item.alternatives || []).filter((a) => a && a.title);
  return `
    <div class="confidence ${level}">
      <div class="confidence-head">
        <span class="confidence-label">${esc(label)}</span>
        ${c != null && !item.corrected ? `<span class="meter"><span style="width:${Math.max(4, Math.min(100, c))}%"></span></span>` : ""}
        ${fixButton ? `<button class="btn small" data-action="fix">${item.needs_review ? "Is this wrong? Fix it" : "Wrong? Fix it"}</button>` : ""}
      </div>
      ${item.confidence_reason && !item.corrected ? `<div class="meta-line">${esc(item.confidence_reason)}</div>` : ""}
      ${item.usage?.model ? `<div class="meta-line resolved-by">Resolved by ${esc(item.usage.model)}</div>` : ""}
      ${alts.length && !item.corrected ? `<div class="alternatives"><span class="meta-line">Did you mean:</span>
        ${alts.map((a, i) => `<button class="chip" data-alt="${i}" title="${esc(a.why || "")}">${esc(a.title)}${a.year ? ` (${esc(a.year)})` : ""} · ${esc((CATEGORY_LABELS[a.category] || a.category || "").replace(/^\S+ /, ""))}</button>`).join("")}
      </div>` : ""}
    </div>`;
}

function correctionFormHtml(item) {
  return `
    <form class="correct-form" id="correct-form">
      <h4>What is it really?</h4>
      <div class="form-grid">
        <label>Title <input name="title" value="${esc(item.title || "")}" autocomplete="off"></label>
        <label>Type <select name="category">
          ${Object.entries(CATEGORY_LABELS).map(([k, label]) => `<option value="${k}" ${k === item.category ? "selected" : ""}>${label}</option>`).join("")}
        </select></label>
        <label>Year <input name="year" inputmode="numeric" pattern="[0-9]{4}" value="${esc((item.metadata || {}).year || "")}"></label>
        <label>Link <input name="canonical_url" type="url" placeholder="IMDb, GitHub, recipe page…" value="${esc(item.canonical_url || "")}"></label>
      </div>
      <label>Or describe it <textarea name="hint" placeholder="e.g. “It's the 2019 remake, not the original” — Claude will look again"></textarea></label>
      <div class="actions">
        <button class="btn primary" type="submit">Save correction</button>
        <button class="btn" type="button" data-action="cancel-fix">Cancel</button>
      </div>
    </form>`;
}

function orderedFacts(item) {
  const m = item.metadata || {};
  const shown = Object.entries(m).filter(([k, v]) => {
    if (HIDDEN_META.has(k) || v == null || v === "") return false;
    if (Array.isArray(v)) return v.length > 0 && v.every((x) => typeof x !== "object");
    return typeof v !== "object";
  });
  const order = FACT_ORDER[item.category] || [];
  const rank = (k) => { const i = order.indexOf(k); return i < 0 ? order.length : i; };
  return shown.sort((a, b) => rank(a[0]) - rank(b[0]));
}

// Things connected to this item that are worth a look: the repository an article describes, an app's homepage, the company behind it.
const REL_KIND = { repo: "Repo", package: "Package", app: "App", paper: "Paper", company: "Company", docs: "Docs", video: "Video", reference: "Reference", site: "Site" };
function relatedHtml(item) {
  const rows = (item.related || []).filter((r) => safeUrl(r.url));
  if (!rows.length) return "";
  return `<section><h3>Related</h3><ul class="related">${rows.map((r) => `
    <li><a class="rel" href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener"${r.why ? ` title="${esc(r.why)}"` : ""}>
      <span class="rel-kind">${esc(REL_KIND[r.kind] || "Link")}</span>
      <span class="rel-main"><b>${esc(r.label)}</b><small>${esc(hostOf(r.url))}${r.why ? ` · ${esc(r.why)}` : ""}</small></span>
      <span class="rel-go" aria-hidden="true">↗</span></a></li>`).join("")}</ul></section>`;
}

function originalHtml(item) {
  const shot = screenshotFor(item);
  const link = item.kind === "url" ? safeUrl(item.source_url) : null;
  const when = item.created_at ? `Saved ${new Date(item.created_at).toLocaleDateString()}` : "";
  const from = item.source_platform && item.source_platform !== "other" ? `from ${item.source_platform}` : "";
  const note = [from, when].filter(Boolean).join(" · ");
  if (link) {
    return `<div class="orig"><a class="orig-link" href="${esc(link)}" target="_blank" rel="noopener"><b>${esc((hostOf(link)[0] || "·").toUpperCase())}</b></a>
      <div><div><b>Saved from a link</b></div><div class="meta-line">${esc(hostOf(link))}${note ? `<br>${esc(note)}` : ""}</div>
      <a class="btn small" href="${esc(link)}" target="_blank" rel="noopener">Open the link ↗</a></div></div>`;
  }
  if (shot) {
    return `<div class="orig"><a class="orig-shot" href="${esc(shot)}" target="_blank" rel="noopener" title="View full size"><img src="${esc(shot)}" alt="Your screenshot"></a>
      <div><div><b>Your screenshot</b></div><div class="meta-line">${esc(note)}</div><a class="btn small" href="${esc(shot)}" target="_blank" rel="noopener">View full size ↗</a></div></div>`;
  }
  return `<p class="meta-line" style="margin:0">The original isn't on this device${item.pending_upload ? "" : ". It opens once you're connected to the server."}</p>`;
}

function renderDetail(item) {
  const m = item.metadata || {};
  const canonical = safeUrl(item.canonical_url);
  const facts = orderedFacts(item);
  const links = (item.links || []).filter((l) => safeUrl(l.url));
  const metaLine = `<span class="eyebrow-type t-${themeOf(item.category)}">${typeIcon(themeOf(item.category))}</span>` + [typeName(item.category), m.year].filter(Boolean).map(esc).join(" · ");

  const dlg = $("#detail");
  dlg.dataset.id = item.id;
  if (fixing && fixing !== item.id) fixing = null;
  if (fixing && document.activeElement?.closest?.("#correct-form")) return;  // don't wipe a form being typed in
  const noteFocused = document.activeElement?.id === "note-edit";
  const noteValue = noteFocused ? document.activeElement.value : item.note || "";
  const about = [item.summary, m.description && m.description !== item.summary && m.description !== item.subtitle ? m.description : null].filter(Boolean);
  const notice = item.status !== "ready" || item.corrected ? "" : item.needs_review
    ? `<div class="notice warn"><span>Magpie isn't sure about this one. ${esc(item.confidence_reason || "")}</span></div>`
    : isUnverified(item) ? `<div class="notice"><span>No source such as TMDB or GitHub confirmed this. It may still be right.</span><button class="btn small" data-action="verify">✓ Mark as correct</button></div>` : "";
  dlg.innerHTML = `
    <div class="detail t-${themeOf(item.category)}" data-id="${esc(item.id)}" tabindex="-1" autofocus>
      <button class="btn close" data-action="close" aria-label="Close">✕</button>
      <div class="hero">
        <div class="hero-cover">${coverHtml(item, { chip: false })}</div>
        <div class="hero-text">
          <div class="eyebrow">${metaLine}</div>
          <h2>${esc(cardTitle(item))}</h2>
          ${item.subtitle ? `<div class="sub">${esc(item.subtitle)}</div>` : ""}
        </div>
      </div>
      ${scoresHtml(m)}
      ${statusHtml(item)}${notice}
      <div class="actions primary-actions">
        ${canonical ? `<a class="btn primary" href="${esc(canonical)}" target="_blank" rel="noopener">Open ${esc(hostOf(canonical) || "source")} ↗</a>` : ""}
        ${item.status === "ready" && fixing !== item.id ? `<button class="btn" data-action="fix">${item.needs_review ? "Is this wrong? Fix it" : "Wrong? Fix it"}</button>` : ""}
        <span class="plugin-slot">${pluginButtonsHtml(item.id)}</span>
        <details class="menu"><summary class="btn">More ▾</summary><div class="menu-list">
          ${item.pending_upload ? "" : `<button class="btn" data-action="refresh" title="Look up the poster, cover, ratings and links again, without re-analyzing">⟳ Refresh metadata</button>
          <button class="btn" data-action="reanalyze">↻ Re-analyze</button>`}
          <label class="menu-select">Type <select id="category-edit">
            ${Object.entries(CATEGORY_LABELS).map(([k, label]) => `<option value="${k}" ${k === item.category ? "selected" : ""}>${label}</option>`).join("")}
          </select></label>
          <button class="btn danger" data-action="delete">Delete</button>
        </div></details>
      </div>
      ${fixing === item.id ? `<section>${correctionFormHtml(item)}</section>` : ""}
      <section><h3>About</h3>${about.length ? about.map((p) => `<p>${esc(p)}</p>`).join("") : `<p class="meta-line">No description yet.${item.pending_upload ? "" : " Refresh metadata may find one."}</p>`}</section>
      ${facts.length ? `<section><h3>${esc(TYPE_BLOCK[item.category] || "Details")}</h3><dl class="facts-table">${facts.map(([k, v]) => `<dt>${esc(humanize(k))}</dt><dd>${
        typeof v === "string" && /^https?:/.test(v) && safeUrl(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(fmtValue(v))
      }</dd>`).join("")}</dl></section>` : ""}
      ${m.ingredients?.length ? `<section><h3>Ingredients</h3><ul>${m.ingredients.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></section>` : ""}
      ${m.instructions?.length ? `<section><h3>Steps</h3><ol>${m.instructions.map((i) => `<li>${esc(i)}</li>`).join("")}</ol></section>` : ""}
      ${links.length ? `<section><div class="links">${links.map((l) => `<a class="chip" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`).join("")}</div></section>` : ""}
      ${relatedHtml(item)}
      <section><h3>Original</h3>${originalHtml(item)}</section>
      ${item.status === "ready" && fixing !== item.id ? `<section><h3>How sure Magpie is</h3>${confidenceHtml(item, { fixButton: false })}</section>` : ""}
      <section><h3>Your note</h3>
        <textarea id="note-edit" placeholder="Why did you save this? Who recommended it?">${esc(noteValue)}</textarea>
        <div class="tag-editor">
          ${(item.tags || []).map((t) => `<span class="chip">#${esc(t)}<button data-remove-tag="${esc(t)}" aria-label="Remove tag">×</button></span>`).join("")}
          <input id="new-tag" placeholder="add tag ↵" enterkeyhint="done" autocapitalize="off">
        </div>
      </section>
      ${state.server?.debug ? `<p class="meta-line"><button class="btn small" type="button" data-debug-item="${esc(item.id)}">🐞 Conversations for this item</button></p>` : ""}
      <details class="provenance"><summary>Where this came from</summary>
        <p class="meta-line">${m.sources ? `Identified via ${esc(m.sources.join(" → "))}.` : "Source trail not recorded."}${m.found_by ? ` Repository found by ${esc(m.found_by.replace(/^GitHub search/, "a GitHub search"))}.` : ""}${
          item.usage?.runs ? ` Cost ${esc(formatUsd(item.usage.cost_usd))}${item.usage.web_searches ? `, ${item.usage.web_searches} web search${item.usage.web_searches > 1 ? "es" : ""}` : ""}.` : ""}</p>
        ${m.screenshot_text ? `<pre>${esc(m.screenshot_text)}</pre>` : ""}
      </details>
    </div>`;
  if (noteFocused) { const n = $("#note-edit"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }
  if (!dlg.open) dlg.showModal();
  loadPluginActions(item);
}

// ---- plugins (Radarr, Sonarr, ...): one button per integration that applies to the item ----

const pluginActions = new Map();   // item id -> [{id, label, action_label, state, message, url}]
const pluginFetched = new Map();   // item id -> when we last asked the server
let pluginCatalog = null;          // GET /api/plugins, remembered until settings change

const DOWNLOAD_ICON = { downloaded: "✓", downloading: "↓", pending: "⏳", missing: "⚠" };

// After it has been added: where the download stands (downloaded / downloading n% / pending / missing), with a link to it.
function downloadChipHtml(p) {
  const d = p.download, href = safeUrl(p.url);
  const percent = d.state === "downloading" ? Math.max(0, Math.min(100, Number(d.percent) || 0)) : null;
  const tip = [p.label, d.label, d.eta, d.detail].filter(Boolean).join(" · ");
  const count = d.total && d.state !== "downloaded" ? `<span class="dl-count">${esc(`${d.have ?? 0}/${d.total}`)}</span>` : "";
  const inner = `<span class="dl-name">${esc(p.label)}</span><span class="dl-icon" aria-hidden="true">${DOWNLOAD_ICON[d.state] || "•"}</span><span class="dl-label">${esc(d.label)}</span>${count}`;
  const cls = `btn plugin-added dl dl-${esc(d.state)}`, style = percent === null ? "" : ` style="--pct:${percent}%"`;
  return href ? `<a class="${cls}"${style} href="${esc(href)}" target="_blank" rel="noopener" title="${esc(tip)}" aria-label="${esc(tip)}">${inner}</a>`
    : `<span class="${cls}"${style} title="${esc(tip)}" aria-label="${esc(tip)}">${inner}</span>`;
}

function pluginButtonsHtml(id) {
  return (pluginActions.get(id) || []).map((p) => {
    if (p.state === "added") {
      if (p.download) return downloadChipHtml(p);
      const href = safeUrl(p.url);
      const text = `✓ In ${esc(p.label)}`;
      return href ? `<a class="btn plugin-added" href="${esc(href)}" target="_blank" rel="noopener">${text} ↗</a>` : `<span class="btn plugin-added">${text}</span>`;
    }
    if (p.state === "unavailable") return `<button class="btn" type="button" disabled title="${esc(p.message || "")}">${esc(p.action_label)}</button>`;
    return `<button class="btn" type="button" data-plugin="${esc(p.id)}">＋ ${esc(p.action_label)}</button>`;
  }).join("");
}

// While something is downloading or waiting, keep the chip current for as long as the card is open.
const activeDownload = (id) => (pluginActions.get(id) || []).map((p) => p.download?.state).filter((st) => st === "downloading" || st === "pending");
let pluginTimer = null;
function schedulePluginRefresh(id) {
  clearTimeout(pluginTimer);
  const active = activeDownload(id);
  if (!active.length) return;
  pluginTimer = setTimeout(async () => {
    const dlg = $("#detail");
    if (!dlg.open || dlg.dataset.id !== id) return;   // closed: stops; opening the card again starts it again
    if (navigator.onLine && document.visibilityState === "visible") {
      try { pluginActions.set(id, await api(`/api/items/${encodeURIComponent(id)}/plugins`)); pluginFetched.set(id, Date.now()); } catch {}
    }
    showPluginActions(id);   // draws it and schedules the next refresh
  }, active.includes("downloading") ? 6000 : 20000);
}

function showPluginActions(id) {
  const dlg = $("#detail");
  const slot = dlg.dataset.id === id ? dlg.querySelector(".plugin-slot") : null;
  if (slot) slot.innerHTML = pluginButtonsHtml(id);
  schedulePluginRefresh(id);
}

async function loadPluginActions(item) {
  if (!navigator.onLine || item.status !== "ready" || item.pending_upload) return;
  if (Date.now() - (pluginFetched.get(item.id) || 0) < (activeDownload(item.id).length ? 4000 : 60000)) {   // the card re-renders on every poll
    schedulePluginRefresh(item.id);
    return;
  }
  pluginFetched.set(item.id, Date.now());
  try {
    pluginCatalog ||= await api("/api/plugins");
    if (!pluginCatalog.some((p) => p.configured && p.categories.includes(item.category))) return;
    pluginActions.set(item.id, await api(`/api/items/${encodeURIComponent(item.id)}/plugins`));
    showPluginActions(item.id);
  } catch {
    pluginFetched.delete(item.id);   // offline or the server is old: try again next time
  }
}

async function runPlugin(pluginId, itemId, btn) {
  const idle = btn.textContent;
  btn.disabled = true; btn.textContent = "Adding…";
  try {
    const res = await api(`/api/items/${encodeURIComponent(itemId)}/plugins/${encodeURIComponent(pluginId)}`, { method: "POST" });
    pluginActions.set(itemId, (pluginActions.get(itemId) || []).map((p) => p.id === pluginId ? { ...p, ...res } : p));
    showPluginActions(itemId);
    toast(`✓ ${res.message}`);
  } catch (e) {
    btn.disabled = false; btn.textContent = idle;
    toast(`✗ ${errorMessage(e)}`);
  }
}

// Material Design icons, inline so they work offline and without calling out to a font host.
const MATERIAL_ICONS = {
  screenshot_monitor: "M20 3H4c-1.1 0-2 .9-2 2v11c0 1.1.9 2 2 2h3l-1 1v2h12v-2l-1-1h3c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 13H4V5h16v11zM6.5 7.5H9V6H5v4h1.5zM19 12h-1.5v2.5H15V16h4z",
  smart_toy: "M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3zM7.5 11.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5S9.83 13 9 13s-1.5-.67-1.5-1.5zM16 17H8v-2h8v2zm-1-4c-.83 0-1.5-.67-1.5-1.5S14.17 10 15 10s1.5.67 1.5 1.5S15.83 13 15 13z",
  local_offer: "M21.41 11.58l-9-9C12.05 2.22 11.55 2 11 2H4c-1.1 0-2 .9-2 2v7c0 .55.22 1.05.59 1.42l9 9c.36.36.86.58 1.41.58.55 0 1.05-.22 1.41-.59l7-7c.37-.36.59-.86.59-1.41 0-.55-.23-1.06-.59-1.42zM5.5 7C4.67 7 4 6.33 4 5.5S4.67 4 5.5 4 7 4.67 7 5.5 6.33 7 5.5 7z",
  storage: "M2 20h20v-4H2v4zm2-3h2v2H4v-2zM2 4v4h20V4H2zm4 3H4V5h2v2zm-4 7h20v-4H2v4zm2-3h2v2H4v-2z",
  link: "M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z",
  search: "M15.5 14h-.79l-.28-.27A6.471 6.471 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z",
};

// Settings → About: the one place that describes Magpie (and says which version is running).
function aboutHtml(data) {
  const v = data.status?.version;
  const icon = (path) => `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="${path}"/></svg>`;
  const feature = (name, text) => `<div class="feature-item">${icon(MATERIAL_ICONS[name])}<span>${text}</span></div>`;
  return `<div class="about">
    <div class="logo-badge"><img src="/static/icons/logo-96.png" alt="" width="56" height="56"></div>
    <h4>Magpie</h4>
    <p class="version">${v && v !== "dev" ? `Version ${esc(v)}` : "Development build"}</p>
    <p class="about-desc">Save screenshots of recommendations. Magpie identifies them and looks up the details.</p>
    <div class="features-mini">
      ${feature("screenshot_monitor", "Screenshot capture")}${feature("smart_toy", "AI identification")}${feature("local_offer", "Auto tagging")}
      ${feature("storage", "Offline library")}${feature("link", "Rich metadata")}${feature("search", "Smart search")}
    </div>
    <p class="about-links"><a href="https://github.com/de-Bat/Magpie" target="_blank" rel="noopener">GitHub</a><span class="separator">•</span><a href="https://github.com/de-Bat/Magpie/issues" target="_blank" rel="noopener">Report an issue</a></p>
  </div>`;
}

// Settings → Plugins: what is installed, and installing more from a git repository or a single file.
function pluginsAdminHtml(data) {
  const admin = data.plugin_admin || { allowed: false, env: "MAGPIE_ALLOW_PLUGIN_INSTALL", sources: [] };
  const row = (title, sub, buttons = "") => `<div class="plugin-row"><div class="plugin-info"><strong>${title}</strong><div class="setting-help">${sub}</div></div><div class="plugin-btns">${buttons}</div></div>`;
  const builtIn = (data.plugins || []).filter((p) => p.source === "built-in")
    .map((p) => row(esc(p.label), `Built in · ${p.configured ? "set up" : `not set up yet (see the ${esc(p.label)} tab)`}`)).join("");
  const sources = admin.sources.map((src) => row(
    esc(src.url ? src.url.replace(/^https:\/\//, "") : `${src.id.replace(/^file-/, "")}.py`),
    src.error ? `<span class="field-error">${esc(src.error)}</span>`
      : `${src.type === "git" ? "Git repository" : "File"}${src.ref ? ` (${esc(src.ref)})` : ""} · ${src.plugins.length ? esc(src.plugins.join(", ")) : "no plugins"}`,
    `${src.type === "git" || src.url ? `<button class="btn small" type="button" data-plugin-update="${esc(src.id)}">Update</button>` : ""}<button class="btn small danger" type="button" data-plugin-remove="${esc(src.id)}">Remove</button>`)).join("");
  const install = admin.allowed ? `
    <div class="plugin-install">
      <label for="plugin-git">Install from a git repository <span class="setting-help">Every plugin in it is added.</span></label>
      <div class="plugin-form"><input id="plugin-git" type="url" placeholder="https://github.com/owner/repo" autocomplete="off" spellcheck="false"><input id="plugin-ref" placeholder="branch or tag (optional)" autocomplete="off" spellcheck="false"><button class="btn" type="button" data-plugin-install="git">Install</button></div>
      <label for="plugin-url">Install a single file</label>
      <div class="plugin-form"><input id="plugin-url" type="url" placeholder="https://…/plugin.py" autocomplete="off" spellcheck="false"><button class="btn" type="button" data-plugin-install="url">Install</button><label class="btn">Upload .py<input id="plugin-upload" type="file" accept=".py,text/x-python" hidden></label></div>
      <p class="setting-note">Plugins run code on this server with its full access. Only install ones you trust.</p>
    </div>`
    : `<p class="setting-note">Installing plugins from a git repository or a file runs their code on this server, so it is off. To turn it on, set <code>${esc(admin.env)}=true</code> in the server's environment (the <code>.env</code> file) and restart.</p>`;
  return `<div class="plugin-list">${builtIn}${sources}</div>${install}`;
}

async function pluginAdminCall(btn, path, opts, done) {
  const idle = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = "Working…"; }
  try {
    settingsData = await apiWithSetup(path, { timeout: 150000, ...opts });
    pluginCatalog = null; pluginActions.clear(); pluginFetched.clear();
    settingsTab = "Plugins";
    toast(`✓ ${done}`);
    await showSettings();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = idle; }
    toast(`✗ ${errorMessage(e)}`);
  }
}

function installPlugin(kind, btn) {
  const git = kind === "git";
  const body = git ? { git: $("#plugin-git").value.trim(), ref: $("#plugin-ref").value.trim() || null } : { url: $("#plugin-url").value.trim() };
  const from = git ? body.git : body.url;
  if (!from) return toast(git ? "Enter the repository's URL." : "Enter the link to a .py file.");
  if (!confirm(`This runs code from ${from} on your server. Only continue if you trust it.`)) return;
  return pluginAdminCall(btn, "/api/plugins/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, "Installed.");
}

document.addEventListener("change", (e) => {
  if (e.target.id !== "plugin-upload" || !e.target.files[0]) return;
  const file = e.target.files[0];
  e.target.value = "";
  if (!confirm(`This runs the code in ${file.name} on your server. Only continue if you trust it.`)) return;
  const body = new FormData();
  body.append("file", file);
  pluginAdminCall(null, "/api/plugins/upload", { method: "POST", body }, `Installed ${file.name}.`);
});

// Settings → Radarr / Sonarr: check the typed (or saved) URL and key, show the result next to the button, and offer the
// server's root folders and quality profiles as dropdowns.
function showTestResult(pluginId, state, text, detail = "") {
  const el = document.querySelector(`[data-test-result="${pluginId}"]`);
  if (!el) return;
  el.className = `test-result ${state}`;
  el.dataset.checked = state === "ok" || state === "err" ? "1" : "";
  const icon = state === "ok" ? "✓" : state === "err" ? "✗" : "";
  el.innerHTML = `${icon ? `<span class="test-icon" aria-hidden="true">${icon}</span>` : ""}<span>${esc(text)}${detail ? ` <span class="test-detail">${esc(detail)}</span>` : ""}</span>`;
}

const trimSlash = (v) => String(v || "").replace(/[\\/]+$/, "");
const sizeText = (bytes) => (bytes >= 1e12 ? `${(bytes / 1e12).toFixed(1)} TB` : `${Math.round(bytes / 1e9)} GB`);

// A text box becomes a dropdown once the server has told us what it offers; with no answer it stays a text box.
function fillPluginOptions(pluginId, res) {
  const form = $("#settings-form");
  const env = pluginId.toUpperCase();
  const fields = [
    [`${env}_ROOT_FOLDER`, res.root_folders || [], (v) => `${v}${res.free_space?.[v] ? ` · ${sizeText(res.free_space[v])} free` : ""}`,
      (list, cur) => list.find((v) => trimSlash(v) === trimSlash(cur))],
    [`${env}_QUALITY_PROFILE`, res.quality_profiles || [], (v) => v,
      (list, cur) => list.find((v) => v.toLowerCase() === cur.toLowerCase() || String(res.quality_profile_ids?.[v]) === cur)],
  ];
  for (const [name, list, label, match] of fields) {
    const el = form.elements[name];
    if (!el || !list.length) continue;
    const current = el.value.trim();
    const sel = document.createElement("select");
    sel.id = el.id; sel.name = name;
    const add = (value, text, selected = false) => sel.append(Object.assign(document.createElement("option"), { value, textContent: text, selected }));
    add("", `Default: ${label(list[0])}`, !current);
    const found = current ? match(list, current) : null;
    list.forEach((v) => add(v === found ? current : v, label(v), v === found));   // a match keeps what is saved: opening and saving changes nothing
    if (current && !found) add(current, `${current} (not found on the server)`, true);
    el.replaceWith(sel);
  }
}

let testSeq = 0;
async function checkPlugin(pluginId, { typed }) {
  const form = $("#settings-form");
  if (!form) return;
  const env = pluginId.toUpperCase();
  const seq = ++testSeq;
  showTestResult(pluginId, "busy", "Testing…");
  try {
    const body = JSON.stringify({ url: typed ? form.elements[`${env}_URL`]?.value.trim() || null : null,
                                  key: typed ? form.elements[`${env}_API_KEY`]?.value.trim() || null : null });
    const path = `/api/plugins/${encodeURIComponent(pluginId)}/test`;
    const headers = { "Content-Type": "application/json" };
    if (setupCode) headers["X-Magpie-Setup-Code"] = setupCode;
    // Pressing the button may ask for the setup code; opening the tab never interrupts with a prompt.
    const res = typed ? await apiWithSetup(path, { method: "POST", headers, body }) : await api(path, { method: "POST", headers, body });
    if (seq !== testSeq) return;
    fillPluginOptions(pluginId, res);
    const n = (list, one) => `${list.length} ${one}${list.length === 1 ? "" : "s"}`;
    showTestResult(pluginId, "ok", res.message, `${n(res.root_folders || [], "root folder")} · ${n(res.quality_profiles || [], "quality profile")}`);
  } catch (e) {
    if (seq !== testSeq) return;
    if (!typed && e instanceof HttpError && e.status === 403) return showTestResult(pluginId, "idle", "Press Test connection to load the options.");
    showTestResult(pluginId, "err", errorMessage(e));
  }
}

async function testPlugin(pluginId, btn) {
  const idle = btn.textContent;
  btn.disabled = true; btn.textContent = "Testing…";
  try { await checkPlugin(pluginId, { typed: true }); } finally { btn.disabled = false; btn.textContent = idle; }
  document.querySelector(`[data-test-result="${pluginId}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });   // the panel may be scrolled
}

// Opening a configured plugin's tab checks it by itself, which also fills the dropdowns.
function autoCheckPlugin() {
  const plugin = settingsData?.plugins?.find((p) => p.label === settingsTab && p.configured);
  const result = plugin && document.querySelector(`[data-test-result="${plugin.id}"]`);
  if (result && !result.dataset.checked) checkPlugin(plugin.id, { typed: false });
}

// ---- events ----------------------------------------------------------------

$("#file-input").addEventListener("change", (e) => {
  const note = $("#note").value.trim();
  $("#note").value = "";
  addScreenshots(e.target.files, note);
  e.target.value = "";
});

document.addEventListener("paste", (e) => {
  if (e.target.matches("input, textarea")) return;
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addScreenshots(files, $("#note").value.trim()); $("#note").value = ""; return; }
  const text = e.clipboardData?.getData("text/plain");
  if (looksLikeUrl(text)) { e.preventDefault(); addLink(text, $("#note").value.trim()); $("#note").value = ""; }
});

$("#find-form").addEventListener("submit", (e) => { e.preventDefault(); runFind($("#find-input").value); });

$("#link-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = $("#link-input").value;
  if (!value.trim()) return;
  addLink(value, $("#note").value.trim());
  $("#link-input").value = "";
  $("#note").value = "";
});

const veil = $("#dropveil");
["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); veil.hidden = false; }));
["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "drop" || !e.relatedTarget) veil.hidden = true; }));
document.addEventListener("drop", (e) => {
  if (e.dataTransfer?.files?.length) { addScreenshots(e.dataTransfer.files, $("#note").value.trim()); $("#note").value = ""; return; }
  const link = (e.dataTransfer?.getData("text/uri-list") || e.dataTransfer?.getData("text/plain") || "").split("\n")[0];
  if (looksLikeUrl(link)) { addLink(link, $("#note").value.trim()); $("#note").value = ""; }
});

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); renderGrid(); }, 120);
});
$("#tag-filter").addEventListener("input", renderFilters);
$("#detail").addEventListener("input", (e) => { if (e.target.id === "settings-search") searchSettings(e.target.value); });
$("#detail").addEventListener("change", (e) => {
  if (e.target.id === "logs-op") { logsFilter = e.target.value; loadLogs(); }
  if (e.target.id === "dbg-op") { debugState.op = e.target.value; loadDebugList(); }
});
$("#detail").addEventListener("keydown", (e) => {
  if (e.target.id !== "settings-search") return;
  if (e.key === "Enter") e.preventDefault();   // not "save settings"
  if (e.key === "Escape" && e.target.value) { e.preventDefault(); e.stopPropagation(); e.target.value = ""; searchSettings(""); }
});
$("#cols-select").addEventListener("change", (e) => {
  state.cols = Number(e.target.value);
  try { localStorage.setItem("magpie.columns", String(state.cols)); } catch {}
  renderGrid();
});

// Keyboard: / searches, N adds, Esc closes the tag list, arrows move between cards.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#add-menu").hidden) { setAddMenu(false); $("#fab-more").focus(); }
  if (e.key === "Escape") { $("#tag-pop").hidden = true; $("#tag-btn").setAttribute("aria-expanded", "false"); }
  if (e.target.matches("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey || document.querySelector("dialog[open]")) return;
  if (e.key === "/") { e.preventDefault(); $("#search").focus(); return; }
  if (e.key.toLowerCase() === "n") { e.preventDefault(); setAddMenu(true); return; }
  const card = e.target.closest?.(".card");
  if (!card) return;
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); card.click(); return; }
  const cards = [...document.querySelectorAll("#grid .card")], i = cards.indexOf(card);
  const rect = card.getBoundingClientRect();
  let next = null;
  if (e.key === "ArrowRight") next = cards[i + 1];
  else if (e.key === "ArrowLeft") next = cards[i - 1];
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const dir = e.key === "ArrowDown" ? 1 : -1;
    next = (dir > 0 ? cards.slice(i + 1) : cards.slice(0, i).reverse()).find((c) => {
      const r = c.getBoundingClientRect();
      return dir * (r.top - rect.top) > 4 && Math.abs(r.left - rect.left) < rect.width / 2;
    });
  }
  if (next) { e.preventDefault(); next.focus(); }
});

$("#sync-status").addEventListener("click", () => (state.sync === "auth" ? askToken() : requestSync()));

async function askToken() {
  const token = prompt("Access token for this Magpie server (MAGPIE_API_TOKEN):");
  if (token === null) return;
  await setToken(token);
  requestSync();
}

document.addEventListener("click", async (e) => {
  if (!e.target.closest("#add-menu, #fab-more")) setAddMenu(false);   // click elsewhere closes the add menu
  if (!e.target.closest("#tag-pop, #tag-btn")) $("#tag-pop").hidden = true;   // click elsewhere closes the tags popover
  const t = e.target.closest("[data-debug-open],[data-debug-back],[data-debug-refresh],[data-debug-clear-item],[data-debug-item],[data-bulk],[data-bulk-cancel],[data-log-open],[data-log-save],[data-log-delete],[data-log-image],[data-logs-reload],[data-logs-clear],[data-verify],[data-tab],[data-show],[data-layout],#tag-btn,[data-usage-days],[data-usage-csv],[data-open-item],[data-tag],[data-clear-tag],.card,[data-action],[data-remove-tag],[data-alt],[data-reset],[data-focus],[data-theme-choice],[data-settings-tab],[data-test-key],[data-load-models],[data-plugin],[data-test-plugin],[data-plugin-install],[data-plugin-update],[data-plugin-remove],[data-activity-dismiss],[data-activity-retry],[data-find],[data-find-pick],[data-find-typed]");
  if (!t) return;
  if (t.dataset.themeChoice !== undefined) return setTheme(t.dataset.themeChoice);
  if (t.dataset.loadModels !== undefined) return loadModels(t.dataset.loadModels);
  if (t.dataset.testKey !== undefined) return testKey(t.dataset.testKey);
  if (t.dataset.activityDismiss !== undefined) return activityDismiss(t.dataset.activityDismiss);
  if (t.dataset.activityRetry !== undefined) { activityDismiss(`analyze:${t.dataset.activityRetry}`); activityDismiss(`reanalyze:${t.dataset.activityRetry}`); return reanalyzeItem(t.dataset.activityRetry); }
  if (t.dataset.testPlugin !== undefined) return testPlugin(t.dataset.testPlugin, t);
  if (t.dataset.pluginInstall !== undefined) return installPlugin(t.dataset.pluginInstall, t);
  if (t.dataset.pluginUpdate !== undefined) return pluginAdminCall(t, `/api/plugins/sources/${encodeURIComponent(t.dataset.pluginUpdate)}/update`, { method: "POST" }, "Updated.");
  if (t.dataset.pluginRemove !== undefined) {
    if (!confirm("Remove this plugin source? Its settings are removed too.")) return;
    return pluginAdminCall(t, `/api/plugins/sources/${encodeURIComponent(t.dataset.pluginRemove)}`, { method: "DELETE" }, "Removed.");
  }
  if (t.dataset.plugin !== undefined) return runPlugin(t.dataset.plugin, t.closest(".detail").dataset.id, t);
  if (t.dataset.settingsTab !== undefined) return selectSettingsTab(t.dataset.settingsTab);
  if (t.dataset.reset !== undefined) {
    return putSettings({ [t.dataset.reset]: null });
  }
  if (t.dataset.focus !== undefined) {
    e.preventDefault();
    const field = document.getElementById(focusIdFor(t.dataset.focus.replace(/^set-/, "")));
    field?.closest("details")?.setAttribute("open", "");   // e.g. an Advanced setting
    const section = field?.closest(".settings-section");
    if (section) selectSettingsTab(section.dataset.section);
    field?.scrollIntoView({ block: "center" });
    return field?.focus();
  }
  if (t.dataset.verify !== undefined) return verifyItem(t.dataset.verify);
  if (t.dataset.debugOpen !== undefined) return openDebug(t.dataset.debugOpen);
  if (t.dataset.debugBack !== undefined) { $(".debug-panel").dataset.view = "list"; return; }
  if (t.dataset.debugRefresh !== undefined) return loadDebugList();
  if (t.dataset.debugClearItem !== undefined) return showDebug({ item: "" });
  if (t.dataset.debugItem !== undefined) return showDebug({ item: t.dataset.debugItem });
  if (t.dataset.logOpen !== undefined) return openLog(t.dataset.logOpen);
  if (t.dataset.logSave !== undefined) return saveLogFile(t.dataset.logSave, t.dataset.format);
  if (t.dataset.logImage !== undefined) { e.preventDefault(); return showLogImage(t.dataset.logImage); }
  if (t.dataset.logsReload !== undefined) return loadLogs();
  if (t.dataset.logDelete !== undefined) {
    if (!confirm("Delete this log?")) return;
    try { await apiWithSetup(`/api/logs/${t.dataset.logDelete}`, { method: "DELETE" }); } catch (err) { return toast(`Couldn't delete: ${errorMessage(err)}`); }
    return loadLogs();
  }
  if (t.dataset.logsClear !== undefined) {
    if (!confirm("Delete every saved conversation log? This can't be undone.")) return;
    try { await apiWithSetup("/api/logs", { method: "DELETE" }); } catch (err) { return toast(`Couldn't delete: ${errorMessage(err)}`); }
    return loadLogs();
  }
  if (t.dataset.find !== undefined) return openFind(t.dataset.find);
  if (t.dataset.findPick !== undefined) return addEntry(findResults[Number(t.dataset.findPick)]);
  if (t.dataset.findTyped !== undefined) return addEntry({ category: findKind, title: t.dataset.findTyped });
  if (t.dataset.bulk !== undefined) return startBulk(t.dataset.bulk);
  if (t.dataset.bulkCancel !== undefined) return cancelBulk();
  if (t.dataset.tab !== undefined) {
    state.tab = t.dataset.tab;
  } else if (t.dataset.show !== undefined) {
    state.show = t.dataset.show;
  } else if (t.dataset.layout !== undefined) {
    state.layout = t.dataset.layout;
    try { localStorage.setItem("magpie.layout", state.layout); } catch {}
  } else if (t.id === "tag-btn") {
    const open = $("#tag-pop").hidden;
    $("#tag-pop").hidden = !open;
    t.setAttribute("aria-expanded", String(open));
    if (open) $("#tag-filter").focus();
    return;
  } else if (t.dataset.usageDays !== undefined) {
    return showUsage(Number(t.dataset.usageDays));
  } else if (t.dataset.usageCsv !== undefined) {
    return downloadUsageCsv(Number(t.dataset.usageCsv));
  } else if (t.dataset.openItem !== undefined) {
    const item = state.items.get(t.dataset.openItem);
    if (item) renderDetail(item);
    return;
  } else if (t.dataset.tag !== undefined) {
    state.tags = state.tags.includes(t.dataset.tag) ? state.tags.filter((x) => x !== t.dataset.tag) : [...state.tags, t.dataset.tag];
  } else if (t.dataset.clearTag !== undefined) {
    state.tags = state.tags.filter((x) => x !== t.dataset.clearTag);
  } else if (t.classList.contains("card")) {
    const item = state.items.get(t.dataset.id);
    if (item) renderDetail(item);
    return;
  } else {
    const id = t.closest(".detail")?.dataset.id;
    const item = id && state.items.get(id);
    if (t.dataset.alt !== undefined && item) {
      const a = (item.alternatives || [])[Number(t.dataset.alt)];
      if (a) return correctItem(id, { title: a.title, category: a.category, year: a.year, canonical_url: a.canonical_url });
    }
    if (t.dataset.removeTag !== undefined && item) {
      return editItem(id, { tags: (item.tags || []).filter((x) => x !== t.dataset.removeTag) });
    }
    switch (t.dataset.action) {
      case "close": return $("#detail").close();
      case "verify": return verifyItem(id);
      case "fix": fixing = id; renderDetail(item); return $("#correct-form input[name=title]")?.focus();
      case "cancel-fix": fixing = null; return renderDetail(item);
      case "refresh": return refreshItemMetadata(id);
      case "reanalyze": return reanalyzeItem(id);
      case "delete":
        if (!confirm("Delete this item?")) return;
        $("#detail").close();
        return deleteItem(id);
      case "token": return askToken();
      case "usage": return showUsage();
      case "add": return setAddMenu($("#add-menu").hidden);
      case "add-upload": setAddMenu(false); return $("#add-dialog").showModal();
      case "close-find": return $("#find-dialog").close();
      case "close-add": return $("#add-dialog").close();
      case "settings": return showSettings();
      case "debug": return showDebug();
      case "sync": return requestSync();
      case "dismiss-install":
        try { localStorage.setItem("magpie.installHintDismissed", "1"); } catch {}
        return renderSyncStatus();
    }
    return;
  }
  render();
});

$("#detail").addEventListener("keydown", (e) => {
  if (e.target.id !== "new-tag" || e.key !== "Enter") return;
  e.preventDefault();
  const value = e.target.value.trim();
  const id = e.target.closest(".detail").dataset.id;
  const item = state.items.get(id);
  if (value && item) editItem(id, { tags: [...(item.tags || []), ...value.split(",")] });
});

$("#detail").addEventListener("change", (e) => {
  const id = e.target.closest(".detail")?.dataset.id;
  const item = id && state.items.get(id);
  if (!item) return;
  if (e.target.id === "note-edit" && e.target.value !== (item.note || "")) editItem(id, { note: e.target.value });
  if (e.target.id === "category-edit") editItem(id, { category: e.target.value });
});

$("#detail").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
$("#detail").addEventListener("change", (e) => { 
  if (e.target.id === "ot-provider") providerSwitch(e.target);
  if (e.target.id === "set-MAGPIE_ANALYZER") applyMode(e.target.value);
});
$("#detail").addEventListener("close", () => { fixing = null; });

$("#detail").addEventListener("input", (e) => {
  const plugin = settingsData?.plugins?.find((p) => e.target.name === `${p.id.toUpperCase()}_URL` || e.target.name === `${p.id.toUpperCase()}_API_KEY`);
  if (plugin) { testSeq++; showTestResult(plugin.id, "idle", "Changed. Press Test connection to check it."); }
});

$("#detail").addEventListener("submit", (e) => {
  if (e.target.id === "settings-form") {
    e.preventDefault();
    return saveSettings(e.target);
  }
  if (e.target.id !== "correct-form") return;
  e.preventDefault();
  const id = e.target.closest(".detail").dataset.id;
  const item = state.items.get(id);
  const f = Object.fromEntries(new FormData(e.target));
  // Send only what the user changed (plus any description).
  const correction = {};
  if (f.title.trim() && f.title.trim() !== (item.title || "")) correction.title = f.title.trim();
  if (f.category && f.category !== item.category) correction.category = f.category;
  if (f.year && Number(f.year) !== Number((item.metadata || {}).year)) correction.year = Number(f.year);
  if (f.canonical_url.trim() && f.canonical_url.trim() !== (item.canonical_url || "")) correction.canonical_url = f.canonical_url.trim();
  if (f.hint.trim()) correction.hint = f.hint.trim();
  fixing = null;
  correctItem(id, correction);
});

// Sync triggers. iOS has no Background Sync API, so sync whenever the app is in view.
window.addEventListener("online", requestSync);
window.addEventListener("offline", () => setSyncState("offline"));
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") requestSync(); });
setInterval(() => { if (document.visibilityState === "visible" && (state.ops.length || state.sync !== "idle")) requestSync(); }, 30000);
setInterval(pollChanges, 8000);  // captures added on other devices show up, with their current status

// ---- boot ------------------------------------------------------------------

async function boot() {
  applyTheme(getTheme());
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("Service worker not registered:", e));
  }
  // Tokens saved by earlier versions live only in the cookie; carry them over.
  const cookieToken = document.cookie.split("; ").find((c) => c.startsWith("magpie_token=") || c.startsWith("keeper_token="));
  if (cookieToken && !(await db.get("kv", "token"))) await setToken(decodeURIComponent(cookieToken.split("=")[1]));

  await loadLocal();
  for (const item of state.items.values()) if (isBusy(item)) trackItem(undefined, item);   // still working from before
  trackBulk();   // a library-wide job that is already running
  render();
  if (new URLSearchParams(location.search).has("shared")) history.replaceState(null, "", "/");
  requestSync();
  refreshCostPill(true);
  refreshLimits(true);
}

boot().catch((e) => { console.error(e); toast(`Couldn't open the offline library: ${e.message}`); });
