"""Background generation of the translated "Creche Guideline" PDFs.

Freshness model
---------------
* The *source fingerprint* is a hash of the sidebar structure plus every
  included page's title and content hash (and TRANSLATION_CACHE_VERSION).
  A language's PDF is up to date only when the fingerprint it was built from
  (Wiki PDF Build.built_fingerprint) equals the current one.
* Each build records when it read the wiki content (snapshot time). A build
  may only replace the published PDF if its snapshot is newer, so a slow
  older build can never overwrite a newer PDF.
* At most one build per language is queued/running (atomic Redis lock).
  Requests that arrive meanwhile set a "rerun" flag, and exactly one
  follow-up build runs when the current one finishes.
* Translation is cached per page and language, keyed on a hash of the
  page's title + content, so only changed pages are sent to the translator.
  Pages whose translation failed are never cached.
"""

import glob
import hashlib
import json
import os
import pickle
import time

import frappe
from frappe.utils import get_datetime, now_datetime

TARGET_LANGUAGES = [
    "en", "kn", "ta", "hi", "te", "mr", "bn", "gu", "ml", "ur", "pa",
    "or", "as", "sa", "gom", "doi", "mai", "mni-Mtei", "ne",
    "sat", "sd", "tcy"
]

# Bump this whenever the translation prompt or provider changes meaningfully
# (e.g. new program-context instructions, a different default model) so that
# every per-page cache entry is treated as stale and re-translated once,
# rather than silently keeping translations built under the old prompt.
# It is also part of the source fingerprint, so bumping it marks every
# language PDF as outdated.
TRANSLATION_CACHE_VERSION = 2

# Bump if the fingerprint's inputs change shape.
FINGERPRINT_SCHEMA = 1

BUILD_DOCTYPE = "Wiki PDF Build"
FINGERPRINT_CACHE_KEY = "wiki_pdf_source_fingerprint"
FINGERPRINT_CACHE_TTL = 3600  # safety net for edits that bypass doc hooks

# The lock is taken when a build is queued (long TTL, covers queue wait) and
# refreshed with a short TTL while the build runs, so a crashed worker frees
# it within LOCK_TTL_RUNNING instead of hours.
LOCK_TTL_QUEUED = 3 * 3600
LOCK_TTL_RUNNING = 15 * 60
PUBLISH_LOCK_TTL = 120

# A first build translates every page; with a slow LLM provider that can take
# hours, and a build killed by the timeout publishes nothing.
JOB_TIMEOUT = 6 * 3600

# After this many failed builds for the same content, stop retrying
# automatically on every download (the Friday job / admin can still retry).
MAX_ATTEMPTS = 3

LABELS_CACHE_KEY = "__labels__"


def _pdf_filename(lang_code):
    # Name kept from the old daily job for compatibility with existing links.
    return f"WikiPDF_DailyCache_{lang_code}.pdf"


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def clear_pdf_cache():
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not allowed")
    pattern = os.path.join(frappe.get_site_path("public", "files"), "WikiPDF_DailyCache_*.pdf")
    deleted = []
    for f in glob.glob(pattern):
        os.remove(f)
        deleted.append(os.path.basename(f))
    # The deleted files no longer exist, so their build records are outdated.
    frappe.db.sql(f"UPDATE `tab{BUILD_DOCTYPE}` SET status = 'Outdated', file_url = NULL")
    frappe.db.commit()
    return deleted


@frappe.whitelist()
def trigger_pdf_generation(clear_locks=False):
    """Admin: rebuild every language now. Unchanged pages are served from the
    translation cache, so this only pays for pages that actually changed.
    `clear_locks` force-releases stuck locks (only if no build is running)."""
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not allowed")
    results = {}
    for lang in TARGET_LANGUAGES:
        lang_code = resolve_lang(lang)
        if frappe.utils.sbool(clear_locks):
            frappe.cache().delete(_lock_key(lang_code))
        results[lang_code] = request_build(lang_code, force=True)
    return results


@frappe.whitelist()
def rebuild_language(lang):
    """Admin: rebuild one language's PDF now (the "Rebuild This Language"
    button). Unchanged pages come from the translation cache."""
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not allowed")
    return request_build(lang, force=True)


# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGES
# ─────────────────────────────────────────────────────────────────────────────

def _target_codes():
    from wiki_pdf.pdf import get_normalized_lang
    return [get_normalized_lang(l) for l in TARGET_LANGUAGES]


def resolve_lang(lang):
    """Normalize a language code and make sure it's one we build PDFs for.
    Also accepts a bare prefix (the download button sends "mni" for
    "mni-Mtei"). Rejecting unknown codes stops arbitrary requests from
    creating build records or paying for translation into random languages."""
    from wiki_pdf.pdf import get_normalized_lang

    codes = _target_codes()
    lang_code = get_normalized_lang(lang)
    if lang_code in codes:
        return lang_code
    base = str(lang or "").lower().split("-")[0]
    for code in codes:
        if code.split("-")[0] == base:
            return code
    frappe.throw(f"PDF is not available for language: {lang}")


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE CONTENT + FINGERPRINT
# ─────────────────────────────────────────────────────────────────────────────

def _load_sidebar():
    sidebar_parents = frappe.get_all("Wiki Group Item", fields=["parent"], limit=1)
    if not sidebar_parents:
        return []
    return frappe.get_all(
        "Wiki Group Item",
        filters={"parent": sidebar_parents[0].parent},
        fields=["wiki_page", "parent_label"],
        order_by="idx asc",
        ignore_permissions=True,
        limit=0,
    )


def _load_source(with_content=False):
    """Returns (sidebar, pages_by_name, fingerprint). Content hashes are
    computed by the database in the same SELECT as the content, so the
    fingerprint always describes exactly the content that was read."""
    sidebar = _load_sidebar()
    names = tuple({s.wiki_page for s in sidebar if s.wiki_page})
    pages = {}
    if names:
        content_col = ", content" if with_content else ""
        rows = frappe.db.sql(
            f"""
            SELECT name, title, modified,
                   LOWER(SHA2(COALESCE(content, ''), 256)) AS content_sha{content_col}
            FROM `tabWiki Page`
            WHERE name IN %(names)s
            """,
            {"names": names},
            as_dict=True,
        )
        pages = {p.name: p for p in rows}
    return sidebar, pages, _fingerprint(sidebar, pages)


def _fingerprint(sidebar, pages):
    items = []
    for s in sidebar:
        p = pages.get(s.wiki_page)
        if p:
            items.append([s.parent_label or "", p.name, p.title or "", p.content_sha])
    payload = json.dumps(
        {"schema": FINGERPRINT_SCHEMA, "tcv": TRANSLATION_CACHE_VERSION, "items": items},
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_current_fingerprint(use_cache=True):
    if use_cache:
        fp = frappe.cache().get_value(FINGERPRINT_CACHE_KEY)
        if fp:
            return fp
    fp = _load_source()[2]
    frappe.cache().set_value(FINGERPRINT_CACHE_KEY, fp, expires_in_sec=FINGERPRINT_CACHE_TTL)
    return fp


def _page_source_hash(page):
    return hashlib.sha256(
        json.dumps([page.title or "", page.content_sha], ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# BUILD RECORDS
# ─────────────────────────────────────────────────────────────────────────────

_BUILD_FIELDS = [
    "name", "status", "build_number", "built_fingerprint", "built_snapshot_at",
    "target_fingerprint", "file_url", "generated_at", "pages_failed", "attempts",
    "started_at", "job_id",
]


def _db_ping():
    """Long translations can outlive the MySQL idle timeout; reconnect if needed."""
    try:
        frappe.db.sql("SELECT 1")
    except Exception:
        frappe.db.connect()


def _get_build(lang_code):
    rec = frappe.db.get_value(BUILD_DOCTYPE, lang_code, _BUILD_FIELDS, as_dict=True)
    if rec:
        return rec
    try:
        frappe.get_doc({
            "doctype": BUILD_DOCTYPE,
            "language": lang_code,
            "status": "Outdated",
            # Adopt a PDF built before build records existed, so it can still be
            # offered as the previous version while the first build runs. Its
            # source is unknown (no fingerprint), so it's treated as outdated.
            "file_url": f"/files/{_pdf_filename(lang_code)}" if _pdf_exists(lang_code) else None,
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
    return frappe.db.get_value(BUILD_DOCTYPE, lang_code, _BUILD_FIELDS, as_dict=True)


def _set_build(lang_code, **values):
    frappe.db.set_value(BUILD_DOCTYPE, lang_code, values, update_modified=False)
    frappe.db.commit()


def _pdf_exists(lang_code):
    path = os.path.join(frappe.get_site_path("public", "files"), _pdf_filename(lang_code))
    return os.path.exists(path) and os.path.getsize(path) > 0


def _freshness(rec, fp, lang_code):
    """'current', 'stale' (needs a build) or 'failed' (retries exhausted)."""
    retries_exhausted = (rec.attempts or 0) >= MAX_ATTEMPTS and rec.target_fingerprint == fp
    if rec.status == "Failed" or rec.pages_failed:
        return "failed" if retries_exhausted else "stale"
    if not _pdf_exists(lang_code) or rec.built_fingerprint != fp:
        return "stale"
    return "current"


# ─────────────────────────────────────────────────────────────────────────────
# LOCKING
# ─────────────────────────────────────────────────────────────────────────────

def _lock_key(lang_code):
    # Same key the old code used (via set_value), so existing admin snippets
    # like frappe.cache().get_value(f"wiki_pdf_active_{lang}") keep working.
    return frappe.cache().make_key(f"wiki_pdf_active_{lang_code}")


def _rerun_key(lang_code):
    return f"wiki_pdf_rerun_{lang_code}"


def _try_lock(lang_code):
    # SET NX is atomic: two simultaneous requests can't both get the lock.
    return bool(frappe.cache().set(_lock_key(lang_code), pickle.dumps(True), nx=True, ex=LOCK_TTL_QUEUED))


def _refresh_lock(lang_code):
    frappe.cache().set(_lock_key(lang_code), pickle.dumps(True), ex=LOCK_TTL_RUNNING)


def _release_lock(lang_code):
    frappe.cache().delete(_lock_key(lang_code))


def _lock_held(lang_code):
    # Not cache().exists(): Frappe's exists() adds the site prefix again, and
    # _lock_key is already prefixed, so it would never find the lock.
    return frappe.cache().get(_lock_key(lang_code)) is not None


# ─────────────────────────────────────────────────────────────────────────────
# REQUESTING BUILDS
# ─────────────────────────────────────────────────────────────────────────────

def _move_to_front(job_id):
    """Move an already-queued build to the front of the long queue, so a user
    waiting on a download isn't stuck behind a bulk (Friday/admin) run."""
    from frappe.utils.background_jobs import get_queue

    try:
        q = get_queue("long")
        job_ids = q.job_ids
        if not job_id or job_id not in job_ids or job_ids[0] == job_id:
            return
        job = q.fetch_job(job_id)
        q.remove(job_id)
        q.enqueue_job(job, at_front=True)
    except Exception:
        frappe.logger().warning(f"Wiki PDF: could not move job {job_id} to the front: {frappe.get_traceback()}")


def _job_alive(job_id):
    """True if the build's RQ job is still queued or running. Unknown counts
    as alive, so a lock is never cleared on a guess."""
    if not job_id:
        return False
    try:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        from frappe.utils.background_jobs import get_redis_conn

        try:
            status = Job.fetch(job_id, connection=get_redis_conn()).get_status()
        except NoSuchJobError:
            return False
        status = getattr(status, "value", status)
        return status in ("queued", "started", "deferred", "scheduled")
    except Exception:
        return True


def _queue_position(job_id):
    """1-based position of a queued build in the long queue, or None."""
    from frappe.utils.background_jobs import get_queue

    try:
        job_ids = get_queue("long").job_ids
        return job_ids.index(job_id) + 1 if job_id in job_ids else None
    except Exception:
        return None


# While someone is waiting on a download for a language, its "waiting" flag is
# kept alive by the page's polling (every 10s); it expires shortly after they
# stop. Background builds for other languages step aside for it (see
# _should_yield), so a user never waits behind a bulk Friday/admin run.
WAITING_TTL = 60


def _waiting_key(lang_code):
    return f"wiki_pdf_waiting_{lang_code}"


def _mark_waiting(lang_code):
    frappe.cache().set_value(_waiting_key(lang_code), True, expires_in_sec=WAITING_TTL)


def _should_yield(lang_code):
    """True if this build should pause for another language someone is
    waiting on. Never yields a build someone is waiting on itself, and only
    for builds that are really sitting in the queue (not already running on
    another worker), so builds can't bounce each other around."""
    if frappe.cache().get_value(_waiting_key(lang_code)):
        return False
    waiting = [c for c in _target_codes() if c != lang_code and frappe.cache().get_value(_waiting_key(c))]
    if not waiting:
        return False
    from frappe.utils.background_jobs import get_queue

    queued_job_ids = set(get_queue("long").job_ids)
    job_ids = frappe.get_all(
        BUILD_DOCTYPE, filters={"name": ["in", waiting], "status": "Queued"}, pluck="job_id"
    )
    return any(j in queued_job_ids for j in job_ids)


# Live progress of a running build, kept in Redis (no DB writes per page) for
# the "45/79 · ~20 min left" display.
PROGRESS_TTL = 6 * 3600
# Used for the estimate until the build has timed its own first page.
DEFAULT_SECONDS_PER_PAGE = 35
# Creating the PDF (render + compress) for the full guide takes ~40s per wiki
# page on Frappe Cloud. Used until a language has a measured time.
DEFAULT_RENDER_SECONDS_PER_PAGE = 40


def _render_time_key(lang_code):
    return f"wiki_pdf_render_seconds_{lang_code}"


def _progress_key(lang_code):
    return f"wiki_pdf_progress_{lang_code}"


def _set_progress(lang_code, progress):
    frappe.cache().set_value(
        _progress_key(lang_code), dict(progress, updated=time.time()), expires_in_sec=PROGRESS_TTL
    )


def _clear_progress(lang_code):
    frappe.cache().delete_value(_progress_key(lang_code))


def get_build_progress(lang_code):
    """{done, total, to_translate, translated, phase, eta_seconds} for a
    running build, or None. The estimate uses this build's own average time
    per translated page (pages reused from the cache take no time)."""
    p = frappe.cache().get_value(_progress_key(lang_code))
    if not p:
        return None
    # How long this language's last PDF creation took, else a per-page guess.
    render_seconds = frappe.cache().get_value(_render_time_key(lang_code)) or (
        p["total"] * DEFAULT_RENDER_SECONDS_PER_PAGE
    )
    if p["phase"] == "rendering":
        eta = render_seconds - max(0.0, time.time() - p.get("rendering_started", time.time()))
    else:
        remaining = max(0, p["to_translate"] - p["translated"])
        per_page = p["translate_seconds"] / p["translated"] if p["translated"] else DEFAULT_SECONDS_PER_PAGE
        # Progress is saved between pages; count down through the current one
        # so the estimate doesn't jump back up on every check.
        since_update = max(0.0, time.time() - p.get("updated", time.time()))
        eta = max(remaining * per_page - since_update, 0) + render_seconds
    eta = max(eta, 15)
    return {
        "done": p["done"],
        "total": p["total"],
        "to_translate": p["to_translate"],
        "translated": p["translated"],
        "phase": p["phase"],
        "eta_seconds": int(eta),
    }


class _StopForMaintenance(Exception):
    """Raised inside a build when the site goes into maintenance mode."""


def _in_maintenance():
    """True while the site is in maintenance mode, e.g. during a Frappe Cloud
    update. Read from site_config.json each time, because a running job
    keeps the config it started with."""
    try:
        return bool(frappe.get_site_config().get("maintenance_mode"))
    except Exception:
        return False


class _YieldToWaitingBuild(Exception):
    """Raised inside a build to pause it for a language a user is waiting on."""


def _enqueue_build(lang_code, at_front=False):
    return frappe.enqueue(
        "wiki_pdf.tasks.generate_pdf_for_single_language",
        lang=lang_code,
        queue="long",
        timeout=JOB_TIMEOUT,
        job_name=f"wiki_pdf_generate_{lang_code}",
        at_front=at_front,
    )


def request_build(lang, force=False, priority=False):
    """Queue a build for one language if its PDF is outdated.
    `priority` means someone is waiting for it: it goes to the front of the
    queue, and background builds of other languages pause for it.
    Returns "current", "failed", "queued" or "running"."""
    lang_code = resolve_lang(lang)
    rec = _get_build(lang_code)
    fp = get_current_fingerprint()

    if not force:
        freshness = _freshness(rec, fp, lang_code)
        if freshness != "stale":
            return freshness

    if priority:
        _mark_waiting(lang_code)

    # A lock left behind by a killed job (stopped, timed out, worker restarted)
    # would otherwise block new builds until it expires.
    if _lock_held(lang_code) and not _job_alive(rec.job_id):
        frappe.logger().info(f"Wiki PDF: lang={lang_code} clearing lock of dead job {rec.job_id}.")
        _release_lock(lang_code)

    if _try_lock(lang_code):
        try:
            job = _enqueue_build(lang_code, at_front=priority)
        except Exception:
            _release_lock(lang_code)
            raise
        _set_build(lang_code, status="Queued", target_fingerprint=fp, job_id=getattr(job, "id", None))
        return "queued"

    # A build is already queued/running.
    if priority and rec.status == "Queued":
        _move_to_front(rec.job_id)
    # If it isn't working from the current content, ask for exactly one
    # follow-up build when it finishes.
    if force or rec.target_fingerprint != fp:
        frappe.cache().set_value(_rerun_key(lang_code), True, expires_in_sec=LOCK_TTL_QUEUED)
    return "running"


def get_pdf_state(lang):
    """Freshness of one language's PDF for the download UI. Queues an update
    when the PDF is outdated (deduplicated, so it's safe to poll)."""
    lang_code = resolve_lang(lang)
    result = request_build(lang_code, priority=True)
    rec = _get_build(lang_code)
    has_pdf = bool(rec.file_url) and _pdf_exists(lang_code)

    building = rec.status in ("Queued", "Updating") and _lock_held(lang_code)
    if result == "current":
        status = "Up to Date"
    elif building:
        # A build is running (e.g. a manual rebuild after retries ran out):
        # show it, rather than "Failed" from the previous attempts.
        status = rec.status
    elif result == "failed":
        status = "Failed"
    else:
        status = "Queued"

    from wiki_pdf.pdf import LANGUAGES

    generated_at = get_datetime(rec.generated_at) if rec.generated_at else None
    return {
        "language": lang_code,
        "language_name": LANGUAGES.get(lang_code, lang_code),
        "generated_label": generated_at.strftime("%d %b %Y, %I:%M %p") if generated_at else None,
        "status": status,
        "has_pdf": has_pdf,
        # ?v= busts browser/CDN caches, since the file name never changes
        "file_url": f"{rec.file_url}?v={rec.build_number}" if has_pdf else None,
        "build_number": rec.build_number,
        "generated_at": rec.generated_at,
        # For the "Waiting in queue (#2)" / "Updating... 1:20" button text
        "queue_position": _queue_position(rec.job_id) if status == "Queued" else None,
        "progress": get_build_progress(lang_code) if status == "Updating" else None,
        "elapsed_seconds": (
            int((now_datetime() - get_datetime(rec.started_at)).total_seconds())
            if status == "Updating" and rec.started_at else None
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TRANSLATION CACHE
# ─────────────────────────────────────────────────────────────────────────────

def _translation_cache_path(lang_code):
    return frappe.get_site_path("private", "files", f"wiki_pdf_translation_cache_{lang_code}.json")


def _load_translation_cache(lang_code):
    path = _translation_cache_path(lang_code)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        frappe.logger().warning(f"Wiki PDF: could not read translation cache at {path}, starting fresh.")
        return {}


def _save_translation_cache(lang_code, cache):
    path = _translation_cache_path(lang_code)
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        frappe.logger().warning(f"Wiki PDF: could not write translation cache at {path}.")


def _looks_untranslated(cached, lang_code):
    """True if a cached "translation" is mostly Latin text for a non-Latin
    target script — left over from the old silent English fallback."""
    from wiki_pdf.pdf import SCRIPT_RANGES

    rng = SCRIPT_RANGES.get(lang_code)
    if not rng:
        return False
    text = frappe.utils.strip_html(cached.get("content_html") or "")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    native = sum(1 for ch in text if rng[0] <= ord(ch) <= rng[1])
    return latin > 200 and native < latin * 0.2


def _cache_hit(cached, page, source_hash, lang_code):
    if not cached or cached.get("version") != TRANSLATION_CACHE_VERSION:
        return False
    if cached.get("source_hash"):
        # Written by this code, which never caches failed translations.
        return cached["source_hash"] == source_hash
    # Entries written before source hashes existed are keyed on `modified`, and
    # may hold English from the old silent fallback.
    return cached.get("modified") == str(page.modified) and not _looks_untranslated(cached, lang_code)


def _translator_id():
    provider = (frappe.conf.get("wiki_pdf_translation_provider") or "google").lower()
    return f"{provider}:{frappe.conf.get(f'{provider}_model') or 'default'}"


def _translate_label(label, lang_code, labels_cache, stats):
    from wiki_pdf.pdf import _safe_translate

    if not label or lang_code == "en":
        return label
    cached = labels_cache.get(label)
    if cached and cached.get("version") == TRANSLATION_CACHE_VERSION:
        return cached["text"]
    label_stats = {}
    translated = _safe_translate(label, lang_code, stats=label_stats)
    if label_stats.get("failed"):
        stats["failed_labels"] += 1
        stats["error"] = stats.get("error") or label_stats.get("error")
    else:
        labels_cache[label] = {"version": TRANSLATION_CACHE_VERSION, "text": translated}
    return translated


# ─────────────────────────────────────────────────────────────────────────────
# THE BUILD JOB
# ─────────────────────────────────────────────────────────────────────────────

def generate_pdf_for_single_language(lang):
    """Builds and publishes the PDF for one language (runs as a background job).
    Only pages whose content changed since their cached translation are sent
    to the translator; the whole PDF is then re-rendered from cached HTML."""
    from wiki_pdf.pdf import (
        _clean_for_pdf, _compress_pdf_gs, _md_to_html, _post_process_pdf,
        _safe_translate, translate_html,
    )

    lang_code = resolve_lang(lang)
    cache_fname = _pdf_filename(lang_code)
    _refresh_lock(lang_code)
    snapshot_at = now_datetime()
    requeued = False
    _skip_followup = False
    frappe.logger().info(f"Wiki PDF: Starting generation for lang={lang_code}")

    try:
        _get_build(lang_code)
        if _in_maintenance():
            raise _StopForMaintenance()
        _set_build(lang_code, status="Updating", started_at=snapshot_at)

        sidebar, pages, fp = _load_source(with_content=True)
        _set_build(lang_code, target_fingerprint=fp)
        if not pages:
            raise Exception("No wiki pages found in the sidebar.")

        translation_cache = _load_translation_cache(lang_code)
        labels_cache = translation_cache.setdefault(LABELS_CACHE_KEY, {})
        translator_id = _translator_id() if lang_code != "en" else "none"
        stats = {"translated": 0, "reused": 0, "failed": 0, "untranslated": 0, "failed_labels": 0}

        translate_started = time.time()
        page_rows = [pages[s.wiki_page] for s in sidebar if s.wiki_page in pages]
        progress = {
            "total": len(page_rows),
            "done": 0,
            "to_translate": sum(
                1 for p in page_rows
                if not _cache_hit(translation_cache.get(p.name), p, _page_source_hash(p), lang_code)
            ),
            "translated": 0,
            "translate_seconds": 0.0,
            "phase": "translating",
        }
        _set_progress(lang_code, progress)

        groups = []
        group_counter = 1
        ref_counter = 1

        try:
            for s in sidebar:
                p = pages.get(s.wiki_page)
                if not p:
                    continue
                label = s.parent_label or ""

                if not groups or groups[-1]["raw_label"] != label:
                    groups.append({
                        "raw_label": label,
                        "label": _translate_label(label, lang_code, labels_cache, stats),
                        "number": group_counter,
                        "anchor": f"GTOC-{group_counter}",
                        "pages": []
                    })
                    group_counter += 1
                    ref_counter = 1

                source_hash = _page_source_hash(p)
                cached = translation_cache.get(p.name)
                if _cache_hit(cached, p, source_hash, lang_code):
                    cached["source_hash"] = source_hash  # upgrades legacy entries in place
                    translated_title = cached["title"]
                    cleaned_content = cached["content_html"]
                    stats["reused"] += 1
                else:
                    page_started = time.time()
                    page_stats = {}
                    raw_html = _md_to_html(p.content or "")
                    translated_html = translate_html(raw_html, lang_code, stats=page_stats)
                    translated_title = _safe_translate(p.title, lang_code, stats=page_stats)
                    cleaned_content = _clean_for_pdf(translated_html)

                    if page_stats.get("failed"):
                        stats["error"] = stats.get("error") or page_stats.get("error")
                        # Don't cache a partial/English result. Show the last good
                        # translation if there is one; the page is retried next build.
                        stats["failed"] += 1
                        if cached and cached.get("content_html"):
                            translated_title = cached["title"]
                            cleaned_content = cached["content_html"]
                        else:
                            # No translation at all: this page would be (partly) English.
                            stats["untranslated"] += 1
                    else:
                        translation_cache[p.name] = {
                            "version": TRANSLATION_CACHE_VERSION,
                            "source_hash": source_hash,
                            "modified": str(p.modified),
                            "translator": translator_id,
                            "title": translated_title,
                            "content_html": cleaned_content,
                        }
                        stats["translated"] += 1
                        # Save as we go: if the job is killed (timeout, deploy,
                        # restart), the translations already paid for are kept.
                        _save_translation_cache(lang_code, translation_cache)
                    time.sleep(0.5)
                    progress["translated"] += 1
                    progress["translate_seconds"] += time.time() - page_started

                _refresh_lock(lang_code)
                progress["done"] += 1
                _set_progress(lang_code, progress)
                # Between pages: step aside if a user is waiting on another
                # language. Translated pages are already cached, so nothing is lost.
                try:
                    _db_ping()
                    must_yield = _should_yield(lang_code)
                except Exception:
                    must_yield = False
                if must_yield:
                    raise _YieldToWaitingBuild()
                # A site update waits (about 5 minutes) for background jobs to
                # finish, then fails. Stop between pages so it can go ahead;
                # translated pages are already cached.
                if _in_maintenance():
                    raise _StopForMaintenance()
                full_number = f"{groups[-1]['number']}.{ref_counter}"
                groups[-1]["pages"].append({
                    "number": full_number,
                    "title": translated_title,
                    "anchor": f"PTOC-{full_number.replace('.', '-')}",
                    "content_html": cleaned_content
                })
                ref_counter += 1
        finally:
            # Persist whatever got translated even if the loop above raises partway
            # through, so a crash doesn't throw away real translation cost already spent.
            _save_translation_cache(lang_code, translation_cache)

        frappe.logger().info(f"Wiki PDF: lang={lang_code} {stats}")

        if not any(g["pages"] for g in groups):
            raise Exception("No content to render.")

        # Never replace an existing PDF with one where pages fell back to English
        # (e.g. the translator is down or the API key is wrong). Keep the previous
        # PDF and fail, so the problem is visible and the pages are retried.
        if stats["untranslated"] and _pdf_exists(lang_code):
            raise Exception(
                f"{stats['untranslated']} of {stats['translated'] + stats['reused'] + stats['failed']} "
                "page(s) could not be translated; the previous PDF was kept. "
                f"Reason: {stats.get('error') or 'see Error Log > Translation Error'}"
            )

        stats["timings"] = {"translate": time.time() - translate_started}
        progress["phase"] = "rendering"
        progress["rendering_started"] = render_started = time.time()
        _set_progress(lang_code, progress)
        _db_ping()
        pdf_bin = _post_process_pdf(None, groups, lang_code=lang_code)
        if not pdf_bin:
            raise Exception("PDF rendering returned an empty file.")
        stats["timings"]["create"] = time.time() - render_started
        compress_started = time.time()
        pdf_bin = _compress_pdf_gs(pdf_bin, label=cache_fname)
        stats["timings"]["compress"] = time.time() - compress_started
        _refresh_lock(lang_code)
        # Remember how long creating the PDF took, for the next estimate.
        frappe.cache().set_value(_render_time_key(lang_code), int(time.time() - render_started))

        _publish(lang_code, cache_fname, pdf_bin, fp, snapshot_at, stats)

    except _StopForMaintenance:
        # Not a failure: leave it Outdated so the next download, the Friday job
        # or a manual rebuild resumes it (from the translation cache).
        frappe.logger().info(f"Wiki PDF: lang={lang_code} stopped for site maintenance.")
        try:
            _set_build(lang_code, status="Outdated")
        except Exception:
            pass
        _skip_followup = True

    except _YieldToWaitingBuild:
        # Re-queue at the back, still holding the lock, so it resumes (from the
        # translation cache) after the build the user is waiting on.
        frappe.logger().info(f"Wiki PDF: lang={lang_code} paused for a waiting download; re-queued.")
        try:
            job = _enqueue_build(lang_code)
            frappe.cache().set(_lock_key(lang_code), pickle.dumps(True), ex=LOCK_TTL_QUEUED)
            _set_build(lang_code, status="Queued", job_id=getattr(job, "id", None))
            requeued = True
        except Exception:
            frappe.logger().error(f"Wiki PDF: could not re-queue lang={lang_code}: {frappe.get_traceback()}")
            _set_build(lang_code, status="Outdated")

    except Exception as e:
        # Keep the previously published PDF; record the failure for retries/UI.
        frappe.logger().error(f"Wiki PDF generation failed for lang={lang_code}: {frappe.get_traceback()}")
        try:
            _db_ping()
            attempts = (frappe.db.get_value(BUILD_DOCTYPE, lang_code, "attempts") or 0) + 1
            last_error = f"{e}\n\n{frappe.get_traceback()}"[:4000]
            _set_build(lang_code, status="Failed", attempts=attempts, last_error=last_error)
        except Exception:
            pass
    finally:
        # Always clear the lock so the next trigger can re-enqueue if needed
        # (unless this build re-queued itself and still owns it).
        try:
            _clear_progress(lang_code)
            if not requeued:
                _release_lock(lang_code)
                if not _skip_followup:
                    _run_followup_if_requested(lang_code)
        except Exception:
            frappe.logger().error(f"Wiki PDF: follow-up check failed for lang={lang_code}: {frappe.get_traceback()}")


def _publish(lang_code, cache_fname, pdf_bin, fp, snapshot_at, stats):
    """Makes this build the published PDF, unless a build that read newer
    content has already published. Serialized per language so the file and
    its record can't be written by two builds at once."""
    from wiki_pdf.pdf import _write_pdf_file

    cache = frappe.cache()
    lock = cache.make_key(f"wiki_pdf_publish_{lang_code}")
    for _ in range(PUBLISH_LOCK_TTL):
        if cache.set(lock, 1, nx=True, ex=PUBLISH_LOCK_TTL):
            break
        time.sleep(1)
    else:
        raise Exception("Timed out waiting for the publish lock.")

    try:
        _db_ping()
        cur = frappe.db.get_value(
            BUILD_DOCTYPE, lang_code,
            ["built_snapshot_at", "built_fingerprint", "build_number", "attempts", "pages_failed"],
            as_dict=True,
        )
        current_fp = get_current_fingerprint(use_cache=False)

        if cur.built_snapshot_at and get_datetime(cur.built_snapshot_at) >= snapshot_at:
            reason = f"a newer build ({cur.built_snapshot_at}) is already published"
        elif (
            fp != current_fp
            and cur.built_fingerprint == current_fp
            and not cur.pages_failed
            and _pdf_exists(lang_code)
        ):
            # Content changed back while this build ran (e.g. an edit was undone):
            # the published PDF already matches the latest content; this one doesn't.
            reason = "the published PDF already matches the latest content"
        else:
            reason = None
        if reason:
            frappe.logger().info(f"Wiki PDF: lang={lang_code} build from {snapshot_at} discarded; {reason}.")
            _settle_status(lang_code, current_fp)
            return False

        _write_pdf_file(cache_fname, pdf_bin)

        complete = not stats["failed"] and not stats["failed_labels"]
        # Content may have changed while this build ran: publish it (it's newer
        # than what was there), but don't call it up to date.
        still_current = current_fp == fp
        status = "Up to Date" if complete and still_current else "Outdated"
        errors = []
        if stats["failed"]:
            errors.append(
                f"{stats['failed']} page(s) failed to translate and will be retried "
                "(their last good translation is used if one exists)."
            )
        if stats["failed_labels"]:
            errors.append(f"{stats['failed_labels']} sidebar label(s) failed to translate.")
        if stats.get("error"):
            errors.append(f"Reason: {stats['error']}")

        _set_build(
            lang_code,
            status=status,
            build_number=(cur.build_number or 0) + 1,
            built_fingerprint=fp,
            built_snapshot_at=snapshot_at,
            file_url=f"/files/{cache_fname}",
            file_size=len(pdf_bin),
            generated_at=now_datetime(),
            pages_total=stats["translated"] + stats["reused"] + stats["failed"],
            pages_translated=stats["translated"],
            pages_reused=stats["reused"],
            pages_failed=stats["failed"],
            attempts=0 if complete else (cur.attempts or 0) + 1,
            last_error=" ".join(errors) or None,
            build_timings=_format_timings(stats),
        )
        frappe.logger().info(f"Wiki PDF: Published {cache_fname} (status={status}).")
        return True
    finally:
        cache.delete(lock)


def _format_timings(stats):
    """E.g. 'Translate 12 min 30s (3 page(s) translated) · Create PDF 2 min 10s · Compress 18s'."""
    t = stats.get("timings") or {}

    def fmt(secs):
        secs = int(secs or 0)
        return f"{secs // 60} min {secs % 60}s" if secs >= 60 else f"{secs}s"

    parts = []
    if "translate" in t:
        parts.append(f"Translate {fmt(t['translate'])} ({stats.get('translated', 0)} page(s) translated)")
    if "create" in t:
        parts.append(f"Create PDF {fmt(t['create'])}")
    if "compress" in t:
        parts.append(f"Compress {fmt(t['compress'])}")
    return " · ".join(parts) or None


def _settle_status(lang_code, fp):
    """Reset a record left in Updating by a discarded build to its real state."""
    rec = _get_build(lang_code)
    freshness = _freshness(rec, fp, lang_code)
    _set_build(lang_code, status={"current": "Up to Date", "failed": "Failed"}.get(freshness, "Outdated"))


def _run_followup_if_requested(lang_code):
    if frappe.cache().get_value(_rerun_key(lang_code)):
        frappe.cache().delete_value(_rerun_key(lang_code))
        request_build(lang_code)  # no-op if the PDF is already current


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER + DOC HOOKS
# ─────────────────────────────────────────────────────────────────────────────

def generate_weekly_translated_pdfs():
    """Friday scheduled job: queues a build only for languages whose PDF is
    outdated (or failed and still has retries left). If nothing changed
    since the last builds, nothing runs."""
    results = {}
    for lang in TARGET_LANGUAGES:
        try:
            results[lang] = request_build(lang)
        except Exception:
            frappe.logger().error(f"Wiki PDF: weekly check failed for lang={lang}: {frappe.get_traceback()}")
    frappe.logger().info(f"Wiki PDF: weekly check {results}")
    return results


def on_wiki_content_change(doc, method=None):
    """Wiki Page / Wiki Space hook. Only marks PDFs outdated (no build is
    started, so edits don't trigger translation cost by themselves). Must
    never break saving a wiki page."""
    try:
        frappe.cache().delete_value(FINGERPRINT_CACHE_KEY)
        # Clear again after commit: a request between the delete above and the
        # commit could re-cache the pre-edit fingerprint.
        frappe.db.after_commit.add(_mark_outdated_after_commit)
    except Exception:
        frappe.logger().error(f"Wiki PDF: content-change hook failed: {frappe.get_traceback()}")


def _mark_outdated_after_commit():
    try:
        fp = get_current_fingerprint(use_cache=False)
        frappe.db.sql(
            f"""UPDATE `tab{BUILD_DOCTYPE}` SET status = 'Outdated'
                WHERE status = 'Up to Date' AND IFNULL(built_fingerprint, '') != %s""",
            fp,
        )
        frappe.db.commit()
    except Exception:
        frappe.logger().error(f"Wiki PDF: marking PDFs outdated failed: {frappe.get_traceback()}")


def ensure_pdf_caches_exist():
    """Queue builds for any language whose PDF is missing or outdated."""
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        return
    try:
        generate_weekly_translated_pdfs()
    except Exception as e:
        frappe.logger().warning(f"Wiki PDF startup check failed: {e}")


def _safe_translate(text, lang, retries=3):
    """Kept for backwards compatibility with old console snippets."""
    from wiki_pdf.pdf import translate_text
    if not text or lang == "en":
        return text
    return translate_text(text, lang)
