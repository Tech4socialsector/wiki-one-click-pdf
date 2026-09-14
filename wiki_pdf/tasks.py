import frappe
import time
import os
import glob
import json


@frappe.whitelist()
def clear_pdf_cache():
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not allowed")
    pattern = os.path.join(frappe.get_site_path("public", "files"), "WikiPDF_DailyCache_*.pdf")
    deleted = []
    for f in glob.glob(pattern):
        os.remove(f)
        deleted.append(os.path.basename(f))
    return deleted


@frappe.whitelist()
def trigger_pdf_generation():
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not allowed")
    from wiki_pdf.pdf import get_normalized_lang
    cleared = []
    for lang in TARGET_LANGUAGES:
        lang_code = get_normalized_lang(lang)
        frappe.cache().delete_value(f"wiki_pdf_active_{lang_code}")
        cleared.append(lang_code)
    generate_weekly_translated_pdfs()
    return f"Cleared locks and enqueued jobs for: {cleared}"

TARGET_LANGUAGES = [
    "en", "kn", "ta", "hi", "te", "mr", "bn", "gu", "ml", "ur", "pa",
    "or", "as", "sa", "gom", "doi", "mai", "mni-Mtei", "ne",
    "sat", "sd", "tcy"
]

# Bump this whenever the translation prompt or provider changes meaningfully
# (e.g. new program-context instructions, a different default model) so that
# every per-page cache entry is treated as stale and re-translated once,
# rather than silently keeping translations built under the old prompt.
TRANSLATION_CACHE_VERSION = 2


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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        frappe.logger().warning(f"Wiki PDF: could not write translation cache at {path}.")


def _enqueue_language(lang, lang_code):
    """Enqueue PDF generation for one language.
    Uses a Redis key as the deduplication lock — faster and race-condition-free
    compared to querying tabRQ Job. TTL matches the job timeout (2 hours).
    Returns True if enqueued, False if already active.
    """
    redis_key = f"wiki_pdf_active_{lang_code}"
    if frappe.cache().get_value(redis_key):
        return False
    frappe.enqueue(
        "wiki_pdf.tasks.generate_pdf_for_single_language",
        lang=lang,
        queue="long",
        timeout=7200,
        job_name=f"wiki_pdf_generate_{lang_code}",
    )
    frappe.cache().set_value(redis_key, True, expires_in_sec=7200)
    return True


def generate_pdf_for_single_language(lang):
    """
    Generates and caches the PDF for one language.
    Called as a separate background job per language so:
    - Each language runs independently (one failure doesn't kill others)
    - The DB connection is fresh per job (avoids MySQL gone-away after long translation)
    """
    from wiki_pdf.pdf import (
        _md_to_html, _clean_for_pdf, _post_process_pdf,
        translate_html, get_normalized_lang, _save_pdf_to_cache
    )

    lang_code = get_normalized_lang(lang)
    cache_fname = f"WikiPDF_DailyCache_{lang_code}.pdf"
    frappe.logger().info(f"Wiki PDF: Starting generation for lang={lang_code}")

    try:
        sidebar_parents = frappe.get_all("Wiki Group Item", fields=["parent"], limit=1)
        if not sidebar_parents:
            frappe.logger().warning("Wiki PDF: No Wiki Group Items found.")
            return

        sidebar_parent = sidebar_parents[0].parent
        sidebar = frappe.get_all(
            "Wiki Group Item",
            filters={"parent": sidebar_parent},
            fields=["wiki_page", "parent_label"],
            order_by="idx asc",
            ignore_permissions=True,
            limit=0,
        )

        p_names = [s.wiki_page for s in sidebar if s.wiki_page]
        if not p_names:
            frappe.logger().warning("Wiki PDF: No wiki pages found in sidebar.")
            return

        p_map = {
            p.name: p for p in frappe.get_all(
                "Wiki Page",
                filters={"name": ["in", p_names]},
                fields=["name", "title", "content", "modified"],
                ignore_permissions=True,
                limit=0,
            )
        }

        translation_cache = _load_translation_cache(lang_code)
        cache_hits = 0
        cache_misses = 0

        groups = []
        group_counter = 1
        ref_counter = 1

        try:
            for s in sidebar:
                if s.wiki_page not in p_map:
                    continue
                p = p_map[s.wiki_page]
                label = s.parent_label or ""

                if not groups or groups[-1]["raw_label"] != label:
                    translated_label = _safe_translate(label, lang_code) if label else label
                    groups.append({
                        "raw_label": label,
                        "label": translated_label,
                        "number": group_counter,
                        "anchor": f"GTOC-{group_counter}",
                        "pages": []
                    })
                    group_counter += 1
                    ref_counter = 1

                modified_str = str(p.modified)
                cached = translation_cache.get(p.name)
                if (
                    cached
                    and cached.get("version") == TRANSLATION_CACHE_VERSION
                    and cached.get("modified") == modified_str
                ):
                    translated_title = cached["title"]
                    cleaned_content = cached["content_html"]
                    cache_hits += 1
                else:
                    raw_html = _md_to_html(p.content or "")
                    translated_html = translate_html(raw_html, lang_code)
                    translated_title = _safe_translate(p.title, lang_code)
                    cleaned_content = _clean_for_pdf(translated_html)
                    translation_cache[p.name] = {
                        "version": TRANSLATION_CACHE_VERSION,
                        "modified": modified_str,
                        "title": translated_title,
                        "content_html": cleaned_content,
                    }
                    cache_misses += 1
                    time.sleep(0.5)

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

        frappe.logger().info(
            f"Wiki PDF: lang={lang_code} translation cache hits={cache_hits} misses={cache_misses}"
        )

        if not groups or not any(g["pages"] for g in groups):
            frappe.logger().warning(f"Wiki PDF: No content for lang={lang_code}. Skipping.")
            return

        # Reconnect DB — translation can take 10-20 min causing MySQL to drop the idle connection
        frappe.db.connect()

        pdf_bin = _post_process_pdf(None, groups, lang_code=lang_code)
        if not pdf_bin:
            frappe.logger().warning(f"Wiki PDF: Empty PDF for lang={lang_code}")
            return

        _save_pdf_to_cache(cache_fname, pdf_bin)
        frappe.logger().info(f"Wiki PDF: Saved {cache_fname} successfully.")

    except Exception:
        # Use frappe.logger() instead of frappe.log_error so we don't depend on a live DB connection
        frappe.logger().error(f"Wiki PDF generation failed for lang={lang_code}: {frappe.get_traceback()}")
    finally:
        # Always clear the Redis lock so the next trigger can re-enqueue if needed
        try:
            frappe.cache().delete_value(f"wiki_pdf_active_{lang_code}")
        except Exception:
            pass


def generate_weekly_translated_pdfs():
    """
    Frappe weekly scheduled task — the sole trigger for regenerating translated
    PDFs (replaces the old on-save trigger, which re-translated all 22
    languages after every Wiki Page edit; that made LLM translation cost scale
    with edit frequency instead of being a predictable weekly cost).
    Enqueues one background job per language so each runs independently.
    """
    frappe.logger().info("Wiki PDF: Enqueueing per-language PDF generation jobs...")
    for lang in TARGET_LANGUAGES:
        from wiki_pdf.pdf import get_normalized_lang
        lang_code = get_normalized_lang(lang)
        _enqueue_language(lang, lang_code)
    frappe.logger().info(f"Wiki PDF: Enqueued {len(TARGET_LANGUAGES)} language jobs.")


def ensure_pdf_caches_exist():
    """
    Called on login (on_login hook).
    Only runs for System Manager / Administrator.
    Checks disk for missing language PDFs and enqueues generation for each missing one.
    """
    import os
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        return
    try:
        from wiki_pdf.pdf import get_normalized_lang
        missing = []
        for lang in TARGET_LANGUAGES:
            lang_code = get_normalized_lang(lang)
            cache_fname = f"WikiPDF_DailyCache_{lang_code}.pdf"
            file_path = os.path.join(frappe.get_site_path("public", "files"), cache_fname)
            if not os.path.exists(file_path):
                missing.append(lang)

        if missing:
            frappe.logger().info(f"Wiki PDF: Missing PDFs for {[get_normalized_lang(l) for l in missing]}. Enqueueing...")
            for lang in missing:
                from wiki_pdf.pdf import get_normalized_lang as gnl
                _enqueue_language(lang, gnl(lang))
        else:
            frappe.logger().info("Wiki PDF: All language PDFs already cached.")
    except Exception as e:
        frappe.logger().warning(f"Wiki PDF startup check failed: {e}")


def _safe_translate(text, lang, retries=3):
    """Translate a short text string using Groq LLM."""
    from wiki_pdf.pdf import translate_text
    if not text or lang == "en":
        return text
    return translate_text(text, lang)
