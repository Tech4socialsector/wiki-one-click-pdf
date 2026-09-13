import base64
import functools
import io
import json
import os
import re
import time
from urllib.parse import unquote, urlparse

import frappe
import markdown2
from bs4 import BeautifulSoup
from frappe import _
from pypdf import PdfReader, PdfWriter

# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGES
# ─────────────────────────────────────────────────────────────────────────────

LANGUAGES = {
    "en": "English", "kn": "Kannada", "ta": "Tamil", "hi": "Hindi",
    "te": "Telugu", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati",
    "ml": "Malayalam", "ur": "Urdu", "pa": "Punjabi", "or": "Odia",
    "as": "Assamese", "sa": "Sanskrit", "gom": "Konkani", "doi": "Dogri",
    "mai": "Maithili", "mni-mtei": "Meitei", "ne": "Nepali",
    "sat": "Santali", "sd": "Sindhi", "tcy": "Tulu",
}

# Expected Unicode block per language, used only as a cheap post-translation
# sanity check (see _validate_script) — catches an LLM translator drifting
# into the wrong script mid-string, which is a real failure mode we observed.
SCRIPT_RANGES = {
    "hi": (0x0900, 0x097F), "mr": (0x0900, 0x097F), "ne": (0x0900, 0x097F),
    "sa": (0x0900, 0x097F), "doi": (0x0900, 0x097F), "mai": (0x0900, 0x097F),
    "gom": (0x0900, 0x097F),
    "kn": (0x0C80, 0x0CFF), "tcy": (0x0C80, 0x0CFF),
    "ta": (0x0B80, 0x0BFF),
    "te": (0x0C00, 0x0C7F),
    "bn": (0x0980, 0x09FF), "as": (0x0980, 0x09FF),
    "gu": (0x0A80, 0x0AFF),
    "ml": (0x0D00, 0x0D7F),
    "ur": (0x0600, 0x06FF), "sd": (0x0600, 0x06FF),
    "pa": (0x0A00, 0x0A7F),
    "or": (0x0B00, 0x0B7F),
    "mni-mtei": (0xABC0, 0xABFF),
    "sat": (0x1C50, 0x1C7F),
}


def get_normalized_lang(lang):
    if not lang or lang == "en":
        return "en"
    lang = str(lang).lower().strip()
    if lang not in LANGUAGES:
        base = lang.split("-")[0]
        if base in LANGUAGES:
            lang = base
    if lang not in LANGUAGES:
        LANGUAGES[lang] = lang
    return lang


def _validate_script(text, lang_code):
    """Flag (not block) translated text containing characters outside the
    target script's expected Unicode block — a cheap tripwire for an LLM
    translator emitting the wrong language/script mid-string."""
    rng = SCRIPT_RANGES.get(lang_code)
    if not rng or not text:
        return
    lo, hi = rng
    bad = [ch for ch in text if ord(ch) > 0x2000 and not (lo <= ord(ch) <= hi)]
    if bad:
        frappe.logger().warning(
            f"Wiki PDF: translation to {lang_code!r} has out-of-script characters "
            f"{bad[:5]!r} in {text[:60]!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TRANSLATION PROVIDERS
# ─────────────────────────────────────────────────────────────────────────────

class Translator:
    """Interface every translation provider implements."""

    def translate_batch(self, texts, target_lang):
        raise NotImplementedError


class GoogleTranslateProvider(Translator):
    """Default provider — a dedicated MT API, not an LLM, so it doesn't
    improvise/drift into the wrong script the way a generative model can."""

    def __init__(self):
        from google.api_core.client_options import ClientOptions
        from google.cloud import translate_v2 as translate

        api_key = frappe.conf.get("google_translate_api_key")
        if not api_key:
            raise ValueError(
                "Google Translate API key not configured. "
                "Run: bench --site <site> set-config google_translate_api_key <key>"
            )
        self._client = translate.Client(client_options=ClientOptions(api_key=api_key))

    def translate_batch(self, texts, target_lang):
        if not texts:
            return []
        results = self._client.translate(texts, target_language=target_lang, format_="text")
        if isinstance(results, dict):
            results = [results]
        return [r["translatedText"] for r in results]


PROGRAM_CONTEXT = (
    "The Creche Guidelines provide a common and practical reference for planning, setting up "
    "and operating creches. They are designed to maintain consistent practices across key areas "
    "such as child safety, nutrition, health, hygiene, growth monitoring, feeding, community "
    "engagement and medical referrals in creche program.\n\n"
    "The guidelines translate the Creche program's approach and objectives into clear, "
    "actionable guidance for day-to-day implementation. They support teams in understanding "
    "what needs to be done, how it should be done and when it should be done.\n\n"
    "The overall purpose is to ensure that every creche provides a safe, nurturing and "
    "responsive environment where children aged 7 months to 3 years receive appropriate care, "
    "nutrition and support for healthy growth and development."
)


def _build_batch_prompt(texts, lang_name):
    texts_json = json.dumps(texts, ensure_ascii=False)
    return (
        f"You are translating a childcare/creche protocol guide for anganwadi workers in India.\n\n"
        f"Program context:\n{PROGRAM_CONTEXT}\n\n"
        f"Translate each text in this JSON array to {lang_name}.\n"
        f"Rules:\n"
        f"- Return ONLY a valid JSON array with exactly {len(texts)} elements in the same order\n"
        f"- Keep proper nouns like ICDS, VHSND, Anganwadi, creche unchanged\n"
        f"- Use simple language that field workers understand\n"
        f"- Respect local nuance and cultural context rather than translating literally\n"
        f"- If translating a word or phrase would spoil or obscure its meaning, leave it in "
        f"English rather than force a translation\n"
        f"- No explanations, no markdown formatting, just the JSON array\n\n"
        f"{texts_json}"
    )


def _parse_batch_response(raw, expected_len):
    result = raw.strip()
    if result.startswith("```"):
        result = result.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    translated = json.loads(result)
    if isinstance(translated, list) and len(translated) == expected_len:
        return [str(t) for t in translated]
    raise ValueError(f"Count mismatch: expected {expected_len}, got {len(translated)}")


class _LLMTranslator(Translator):
    """Shared JSON-batch-prompt + retry logic for LLM-based providers.
    Kept as alternates, not the default — an LLM can drift into the wrong
    script, which is the exact bug _validate_script exists to catch."""

    def _call(self, prompt):
        raise NotImplementedError

    def translate_batch(self, texts, target_lang):
        if not texts:
            return []
        lang_name = LANGUAGES.get(target_lang, target_lang)
        prompt = _build_batch_prompt(texts, lang_name)
        for attempt in range(3):
            try:
                raw = self._call(prompt)
                return _parse_batch_response(raw, len(texts))
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                else:
                    frappe.log_error(
                        f"{self.__class__.__name__} batch translation failed (lang={target_lang}): {e}",
                        "Translation Error",
                    )
        return texts  # fallback: return originals on all failures


class GeminiProvider(_LLMTranslator):
    def __init__(self):
        import google.generativeai as genai

        api_key = frappe.conf.get("gemini_api_key")
        if not api_key:
            raise ValueError(
                "Gemini API key not configured. Run: bench --site <site> set-config gemini_api_key <key>"
            )
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(frappe.conf.get("gemini_model") or "gemini-3.6-flash")

    def _call(self, prompt):
        response = self._model.generate_content(
            prompt, generation_config={"temperature": 0.1, "max_output_tokens": 4096}
        )
        return response.text


class ClaudeProvider(_LLMTranslator):
    def __init__(self):
        import anthropic

        api_key = frappe.conf.get("anthropic_api_key")
        if not api_key:
            raise ValueError(
                "Anthropic API key not configured. Run: bench --site <site> set-config anthropic_api_key <key>"
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = frappe.conf.get("claude_model") or "claude-haiku-4-5-20251001"

    def _call(self, prompt):
        # temperature omitted -- the anthropic package installed on this
        # server's messages.create() rejects it as an unexpected kwarg.
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


class GroqProvider(_LLMTranslator):
    """Kept as an optional provider (not default) for continuity with the
    old implementation. Config key is now correctly named groq_api_key —
    the previous code confusingly read gemini_api_key and fed it to Groq."""

    def __init__(self):
        from groq import Groq

        api_key = frappe.conf.get("groq_api_key")
        if not api_key:
            raise ValueError(
                "Groq API key not configured. Run: bench --site <site> set-config groq_api_key <key>"
            )
        self._client = Groq(api_key=api_key)
        self._model = frappe.conf.get("groq_model") or "llama-3.3-70b-versatile"

    def _call(self, prompt):
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.1,
        )
        return response.choices[0].message.content


_PROVIDERS = {
    "google": GoogleTranslateProvider,
    "gemini": GeminiProvider,
    "claude": ClaudeProvider,
    "groq": GroqProvider,
}

_translator_instance = None
_translator_provider_name = None


def get_translator():
    """Returns a cached Translator instance for the site-configured provider
    (wiki_pdf_translation_provider, default "google")."""
    global _translator_instance, _translator_provider_name
    provider_name = (frappe.conf.get("wiki_pdf_translation_provider") or "google").lower()
    if _translator_instance is None or _translator_provider_name != provider_name:
        provider_cls = _PROVIDERS.get(provider_name)
        if not provider_cls:
            frappe.throw(f"Unknown wiki_pdf_translation_provider: {provider_name!r}")
        _translator_instance = provider_cls()
        _translator_provider_name = provider_name
    return _translator_instance


def translate_text(text, lang="en"):
    lang_code = get_normalized_lang(lang)
    if not text or lang_code == "en":
        return text
    try:
        result = get_translator().translate_batch([text], lang_code)
        translated = result[0] if result else text
        _validate_script(translated, lang_code)
        return translated
    except Exception as e:
        frappe.logger().warning(f"Translation failed for lang {lang_code}: {e}")
        return text


def translate_html(html_content, lang="en"):
    """Translates text nodes in HTML, batched through the configured provider."""
    lang_code = get_normalized_lang(lang)
    if not html_content or lang_code == "en":
        return html_content

    try:
        soup = BeautifulSoup(html_content, "html.parser")

        nodes_to_translate = []
        texts_to_translate = []
        for node in soup.find_all(string=True):
            if node.parent.name in ["style", "script", "head", "title", "meta", "[document]"]:
                continue
            text = str(node).strip()
            if text and not text.isnumeric():
                nodes_to_translate.append(node)
                texts_to_translate.append(str(node))

        if not texts_to_translate:
            return html_content

        # Batch: max 50 items or 3000 chars per batch
        BATCH_SIZE = 50
        MAX_CHARS = 3000
        batches = []
        cur_nodes, cur_texts, cur_chars = [], [], 0
        for node, text in zip(nodes_to_translate, texts_to_translate):
            if (len(cur_texts) >= BATCH_SIZE or cur_chars + len(text) > MAX_CHARS) and cur_texts:
                batches.append((cur_nodes, cur_texts))
                cur_nodes, cur_texts, cur_chars = [], [], 0
            cur_nodes.append(node)
            cur_texts.append(text)
            cur_chars += len(text)
        if cur_texts:
            batches.append((cur_nodes, cur_texts))

        translator = get_translator()
        for batch_nodes, batch_texts in batches:
            translated = translator.translate_batch(batch_texts, lang_code)
            for node, t in zip(batch_nodes, translated):
                _validate_script(t, lang_code)
                node.replace_with(t)
            time.sleep(0.3)  # stay within provider rate limits

        return str(soup)

    except Exception as e:
        frappe.log_error(f"translate_html failed (lang={lang_code}): {e}", "Translation Error")
        return html_content


def _safe_translate(text, lang):
    """Translate a short text string (titles/labels) using the configured provider."""
    if not text or lang == "en":
        return text
    return translate_text(text, lang)


# ─────────────────────────────────────────────────────────────────────────────
# MARKDOWN / HTML CONTENT PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

_MD_EXTRAS = ["tables", "fenced-code-blocks", "strike", "cuddled-lists", "break-on-newline", "header-ids", "footnotes"]


def _md_to_html(text):
    """Robust markdown to HTML conversion with table-hiding to avoid markdown2 crashes."""
    if not text:
        return ""
    try:
        return markdown2.markdown(text, extras=_MD_EXTRAS)
    except AssertionError:
        # markdown2 failed (likely a large HTML table) — hide tables, parse the
        # rest, then re-insert.
        tables = []

        def _hide(match):
            tables.append(match.group(0))
            return f"\n\nPROTECTEDTABLE{len(tables) - 1}\n\n"

        hidden_md = re.sub(r"(<table[^>]*>.*?</table>)", _hide, text, flags=re.DOTALL | re.IGNORECASE)
        try:
            html = markdown2.markdown(hidden_md, extras=_MD_EXTRAS)
            for i, table_html in enumerate(tables):
                placeholder = f"PROTECTEDTABLE{i}"
                html = html.replace(f"<p>{placeholder}</p>", table_html)
                html = html.replace(placeholder, table_html)
            return html
        except Exception:
            return f"<pre>{frappe.utils.escape_html(text)}</pre>"
    except Exception as e:
        frappe.log_error(f"Markdown parsing error: {str(e)}", "Wiki PDF Markdown Error")
        return f"<div>Error parsing content: {frappe.utils.escape_html(text[:100])}...</div>"


def _inline_images(html):
    """Ensure images work in PDF by resolving local paths to file:// paths."""
    if not html:
        return html
    from frappe.core.doctype.file.utils import find_file_by_url

    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue

        fallback = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

        try:
            real_src = unquote(src).strip()

            resolved_path = None
            if not real_src.startswith("http") or real_src.startswith(frappe.utils.get_url()):
                url_path = urlparse(real_src).path
                file_doc = find_file_by_url(url_path)
                if file_doc and os.path.exists(file_doc.get_full_path()):
                    resolved_path = os.path.abspath(file_doc.get_full_path())

            if not resolved_path and real_src.startswith("/files/"):
                fname = real_src.split("/")[-1]
                path = frappe.get_site_path("public", "files", fname)
                if os.path.exists(path):
                    resolved_path = os.path.abspath(path)
                else:
                    alt_path = os.path.join(frappe.get_site_path(), "public", "files", fname)
                    if os.path.exists(alt_path):
                        resolved_path = os.path.abspath(alt_path)

            if resolved_path:
                img["src"] = f"file://{resolved_path}"
            else:
                if not real_src.startswith("/") and not real_src.startswith("http"):
                    test_path = frappe.get_site_path("public", real_src)
                    if os.path.exists(test_path):
                        img["src"] = f"file://{os.path.abspath(test_path)}"

        except Exception as e:
            frappe.log_error(f"Image resolution error for {src}: {str(e)}", "Wiki PDF Image Error")
            img["src"] = fallback
    return str(soup)


def _split_tables(html, max_rows=25):
    """Splits large tables into groups of `max_rows` rows so page-break-inside:avoid works."""

    def _get_thead(table_html):
        m = re.search(r"(<thead[^>]*>.*?</thead>)", table_html, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
        first = re.search(r"(<tr[^>]*>.*?</tr>)", table_html, re.DOTALL | re.IGNORECASE)
        return f"<thead>{first.group(1)}</thead>" if first else ""

    def _get_colgroup(table_html):
        m = re.search(r"(<colgroup[^>]*>.*?</colgroup>)", table_html, re.DOTALL | re.IGNORECASE)
        return m.group(1) if m else ""

    def _get_tbody_rows(table_html):
        tbody = re.search(r"<tbody[^>]*>(.*?)</tbody>", table_html, re.DOTALL | re.IGNORECASE)
        src = tbody.group(1) if tbody else table_html
        return re.findall(r"<tr[^>]*>.*?</tr>", src, re.DOTALL | re.IGNORECASE)

    TABLE_STYLE = "width:100%;border-collapse:collapse;table-layout:fixed;font-size:10pt;margin:0;page-break-inside:avoid !important;"

    def process_table(match):
        table_html = match.group(0)
        thead, colgroup, rows = _get_thead(table_html), _get_colgroup(table_html), _get_tbody_rows(table_html)
        if len(rows) <= max_rows:
            return re.sub(r"<table([^>]*)>", lambda m: f'<table{m.group(1)} style="{TABLE_STYLE}">', table_html, 1, re.IGNORECASE)

        chunks = [rows[i : i + max_rows] for i in range(0, len(rows), max_rows)]
        parts = []
        for idx, chunk in enumerate(chunks):
            continued = f'<div style="font-size:9pt;color:#555;text-align:right;margin-top:4pt;">(continued...)</div>' if idx > 0 else ""
            parts.append(f'{continued}<table style="{TABLE_STYLE}">{colgroup}{thead}<tbody>{"".join(chunk)}</tbody></table>')
        return '\n<div style="margin:4pt 0;"></div>\n'.join(parts)

    return re.sub(r"<table[^>]*>.*?</table>", process_table, html, flags=re.DOTALL | re.IGNORECASE)


def _clean_for_pdf(html):
    def replace_media(match):
        src = re.search(r'src=["\']([^"\']+)["\']', match.group(0))
        if src:
            url = src.group(1)
            if "youtube.com/embed/" in url:
                video_id = url.split("embed/")[1].split("?")[0]
                url = f"https://www.youtube.com/watch?v={video_id}"
            return f'<div style="border:1px solid #ccc;background:#f9f9f9;padding:6pt 10pt;margin:6pt 0;"><a href="{url}">Watch Video: {url}</a></div>'
        return match.group(0)

    html = re.sub(r"<(iframe|video)[^>]*>.*?</\1>", replace_media, html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<details[^>]*>", '<div style="display:block;margin:6pt 0;">', html, flags=re.IGNORECASE)
    html = re.sub(r"</details>", "</div>", html, flags=re.IGNORECASE)
    html = re.sub(r"<summary[^>]*>", '<span style="font-weight:bold;">', html, flags=re.IGNORECASE)
    html = re.sub(r"</summary>", "</span><br>", html, flags=re.IGNORECASE)
    return _split_tables(html)


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

PDF_CSS = """
@page { size: A4; margin: 15mm 18mm; }
body { font-family: 'Noto Sans Telugu', 'Noto Sans Kannada', 'Noto Sans Tamil', 'Noto Sans Devanagari', 'Noto Sans Bengali', 'Noto Sans Malayalam', 'Noto Sans Gujarati', 'Noto Sans Gurmukhi', 'Noto Sans Oriya', 'Noto Sans Arabic', 'Noto Sans Meetei Mayek', 'Noto Sans Ol Chiki', Georgia, serif; font-size: 11pt; line-height: 1.4; color: #111; margin: 0; padding: 0; }
h1.group-name { font-size: 22pt; font-weight: bold; border-bottom: 2px solid #333; padding-bottom: 4pt; margin-bottom: 14pt; page-break-after: avoid !important; }
h1.page-title, h2.page-title { color: #1a52a0; font-size: 22pt; font-weight: bold; margin-bottom: 12pt; page-break-after: avoid !important; }
h1 { font-size: 18pt; color: #222; margin-top: 14pt; margin-bottom: 6pt; page-break-after: avoid !important; }
h2 { font-size: 16pt; color: #222; margin-top: 14pt; margin-bottom: 6pt; page-break-after: avoid !important; }
h3 { font-size: 14pt; color: #222; margin-top: 12pt; margin-bottom: 4pt; page-break-after: avoid !important; }
h4 { font-size: 12pt; color: #222; margin-top: 10pt; margin-bottom: 4pt; page-break-after: avoid !important; }
p { margin: 4pt 0; }
img { max-width: 100%; height: auto; display: block; margin: 8pt 0; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0; table-layout: fixed; font-size: 10pt; page-break-inside: auto; }
thead { display: table-header-group !important; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #aaa; padding: 4pt 6pt; vertical-align: top; word-break: break-word; line-height: 1.2; }
th { background-color: #eee; font-weight: bold; text-align: left; }
blockquote { border: 1px solid #bbb; border-left: 4pt solid #555; background: #f7f7f7; padding: 8pt 14pt; margin: 8pt 0; page-break-inside: avoid; }
pre, code { background: #f4f4f4; font-family: monospace; border-radius: 3px; }
pre { padding: 8pt; border: 1px solid #ddd; white-space: pre-wrap; margin: 6pt 0; page-break-inside: avoid; }
"""

TOC_TITLES = {
    "en": "Table of Contents", "kn": "ವಿಷಯ ಸೂಚಿ", "ta": "உள்ளடக்கம்", "hi": "विषय-सूची",
    "te": "విషయ సూచిక", "mr": "अनुक्रमणिका", "bn": "সূচিপত্র", "gu": "સામગ્રી સૂચિ",
    "ml": "ഉള്ളടക്കം", "ur": "فہرست مضامین", "pa": "ਸਮੱਗਰੀ ਸੂਚੀ", "or": "ବିଷୟ ସୂଚୀ",
    "as": "বিষয়বস্তুৰ তালিকা", "sa": "विषयसूची", "gom": "विषय सूची", "doi": "विषय-सूची",
    "mai": "विषय-सूची", "mni-mtei": "ꯋꯥꯈꯜ ꯑꯣꯏꯕꯒꯤ ꯄꯨꯛꯊꯣꯀꯄꯥ", "ne": "सामग्री तालिका",
    "sat": "ᱥᱟᱹᱦᱩᱛ ᱛᱟᱞᱤᱠᱟ", "sd": "مواد جي فهرست", "tcy": "ಪರಿವಿಡಿ",
}

TOC_STYLE = """
<style>
    body { font-family: 'Noto Sans Telugu', 'Noto Sans Kannada', 'Noto Sans Tamil', 'Noto Sans Devanagari', 'Noto Sans Bengali', 'Noto Sans Malayalam', 'Noto Sans Gujarati', 'Noto Sans Gurmukhi', 'Noto Sans Oriya', 'Noto Sans Arabic', 'Noto Sans Meetei Mayek', 'Noto Sans Ol Chiki', Georgia, serif; padding: 20mm; margin: 0; color: #111; }
    h1 { font-size: 24pt; font-weight: bold; border-bottom: 2px solid #333; padding-bottom: 10px; margin-bottom: 30px; }
    .toc-container { width: 100%; }
    .toc-item { clear: both; overflow: hidden; margin-bottom: 12pt; line-height: 1.2; }
    .toc-title { float: left; white-space: nowrap; padding-right: 5px; }
    .toc-page { float: right; white-space: nowrap; padding-left: 5px; font-weight: bold; color: #1a52a0; }
    .toc-line { overflow: hidden; border-bottom: 1px solid #999; height: 1.0em; }
    .level-0 .toc-title { font-weight: bold; font-size: 13pt; }
    .level-1 { padding-left: 25px; }
    .level-1 .toc-title { font-size: 11pt; color: #444; }
</style>
"""


@functools.lru_cache(maxsize=1)
def _embedded_font_face_css():
    """Base64-embeds bundled fonts via @font-face rather than relying on the
    server having them installed system-wide.

    We tried relying on the server's installed fonts instead (Kannada/etc.
    already work that way) -- but a manually `fc-cache`-installed font in
    ~/.fonts does NOT survive a Frappe Cloud redeploy (each deploy is a
    fresh container), so WeasyPrint silently fell back to the bitmap
    "unifont" for Odia after the next deploy. Embedding the font directly
    in the generated HTML is redeploy-proof since the .ttf files are
    committed to the app itself."""
    font_dir = os.path.join(os.path.dirname(__file__), "public", "fonts")
    faces = []
    for family, weight, fname in [
        ("Noto Sans Oriya", "normal", "NotoSansOriya-Regular.ttf"),
        ("Noto Sans Oriya", "bold", "NotoSansOriya-Bold.ttf"),
        # Meetei Mayek (Manipuri) and Ol Chiki (Santali) have no system font at
        # all on this server (confirmed via fc-list :lang=mni / :lang=sat --
        # unlike Odia, this isn't a redeploy-persistence issue, there's simply
        # no font installed). Only a Regular weight is available upstream.
        ("Noto Sans Meetei Mayek", "normal", "NotoSansMeeteiMayek-Regular.ttf"),
        ("Noto Sans Ol Chiki", "normal", "NotoSansOlChiki-Regular.ttf"),
    ]:
        path = os.path.join(font_dir, fname)
        try:
            with open(path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
            faces.append(
                f"@font-face {{ font-family: '{family}'; font-weight: {weight}; "
                f"src: url(data:font/truetype;base64,{encoded}) format('truetype'); }}"
            )
        except FileNotFoundError:
            frappe.logger().warning(f"Wiki PDF: font file missing: {path}")
    return "\n".join(faces)


def _wrap(body):
    return f"<html><head><meta charset='UTF-8'><style>{_embedded_font_face_css()}\n{PDF_CSS}</style></head><body>{body}</body></html>"


# ─────────────────────────────────────────────────────────────────────────────
# RENDERING (WeasyPrint)
# ─────────────────────────────────────────────────────────────────────────────

def _render_pdf(html):
    """Render an HTML string to PDF bytes via WeasyPrint.

    Replaces the old wkhtmltopdf/pdfkit call — wkhtmltopdf's patched-Qt
    WebKit could not correctly shape several Indic scripts (Kannada, Odia)
    regardless of font availability; WeasyPrint's Pango/HarfBuzz-based
    shaping renders them correctly (confirmed on the production server)."""
    from weasyprint import HTML

    return HTML(string=html, base_url=frappe.get_site_path()).write_pdf()


def _add_page_numbers(pdf_bin, skip_first=False, skip_last=False, skip_count=1):
    """Adds page numbers using a single render for all pages (avoids per-page overhead)."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bin))
        total_pages = len(reader.pages)

        footer_divs = []
        for i in range(total_pages):
            page_break = "page-break-after:always;" if i < total_pages - 1 else ""
            if (skip_first and i < skip_count) or (skip_last and i == total_pages - 1):
                footer_divs.append(f'<div style="{page_break}width:210mm;height:20mm;"></div>')
            else:
                page_num = i + 1
                footer_divs.append(
                    f'<div style="{page_break}width:210mm;height:20mm;position:relative;font-family:Georgia,serif;font-size:10pt;">'
                    f'<div style="position:absolute;bottom:2mm;width:100%;text-align:center;">{page_num}</div>'
                    f'</div>'
                )

        all_footers_html = (
            "<html><head><meta charset='UTF-8'>"
            "<style>@page{size:210mm 20mm;margin:0;}body{margin:0;padding:0;background:transparent !important;}</style>"
            "</head><body>" + "".join(footer_divs) + "</body></html>"
        )

        footer_pdf_bin = _render_pdf(all_footers_html)
        if not footer_pdf_bin:
            return pdf_bin

        footer_reader = PdfReader(io.BytesIO(footer_pdf_bin))
        writer = PdfWriter()

        for i in range(total_pages):
            content_page = reader.pages[i]
            is_skipped = (skip_first and i < skip_count) or (skip_last and i == total_pages - 1)
            if not is_skipped and i < len(footer_reader.pages):
                content_page.merge_page(footer_reader.pages[i])
            writer.add_page(content_page)

        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as e:
        frappe.log_error(f"Page numbering error: {str(e)}", "Wiki PDF Error")
        return pdf_bin


def _compress_pdf_gs(pdf_bin, label=""):
    """Compress PDF using Ghostscript (font subsetting + image recompression).
    Falls back to original bytes if gs is unavailable or compression makes it larger."""
    import subprocess
    import tempfile

    tmp_in = tmp_out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bin)
            tmp_in = f.name
        tmp_out = tmp_in + "_compressed.pdf"
        result = subprocess.run(
            [
                "gs", "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.4",
                "-dPDFSETTINGS=/ebook",
                "-dEmbedAllFonts=true",
                "-dSubsetFonts=true",
                "-dCompressFonts=true",
                "-dNOPAUSE", "-dQUIET", "-dBATCH",
                f"-sOutputFile={tmp_out}", tmp_in,
            ],
            capture_output=True,
            timeout=300,
        )
        if result.returncode == 0 and os.path.exists(tmp_out):
            with open(tmp_out, "rb") as f:
                compressed = f.read()
            before_kb, after_kb = len(pdf_bin) // 1024, len(compressed) // 1024
            frappe.logger().info(f"Wiki PDF {label}: gs {before_kb} KB -> {after_kb} KB")
            return compressed if len(compressed) < len(pdf_bin) else pdf_bin
        frappe.logger().warning(
            f"Wiki PDF: gs failed (rc={result.returncode}): {result.stderr.decode(errors='replace')[:200]}"
        )
    except Exception:
        frappe.logger().warning(f"Wiki PDF: gs compression skipped for {label}")
    finally:
        for p in (tmp_in, tmp_out):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass
    return pdf_bin


# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────

def _save_pdf_to_cache(cache_fname, pdf_bin):
    """Compress, write PDF to disk, and create/update a File record."""
    pdf_bin = _compress_pdf_gs(pdf_bin, label=cache_fname)

    file_path = os.path.join(frappe.get_site_path("public", "files"), cache_fname)
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(pdf_bin)
    os.replace(tmp_path, file_path)

    try:
        frappe.db.sql("SELECT 1")
    except Exception:
        frappe.db.connect()

    file_url = f"/files/{cache_fname}"
    file_size = len(pdf_bin)
    user = frappe.session.user or "Administrator"
    now = frappe.utils.now_datetime()

    existing = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if existing:
        frappe.db.set_value("File", existing, {"file_name": cache_fname, "file_size": file_size})
    else:
        frappe.db.sql(
            """
            INSERT INTO `tabFile`
                (name, owner, creation, modified, modified_by,
                 file_name, file_url, file_size, is_private, folder)
            VALUES
                (%(name)s, %(user)s, %(now)s, %(now)s, %(user)s,
                 %(file_name)s, %(file_url)s, %(file_size)s, 0, 'Home/Attachments')
            """,
            {
                "name": frappe.generate_hash(length=10),
                "user": user,
                "now": now,
                "file_name": cache_fname,
                "file_url": file_url,
                "file_size": file_size,
            },
        )
    frappe.db.commit()


def _load_pdf_from_cache(cache_fname):
    file_path = os.path.join(frappe.get_site_path("public", "files"), cache_fname)
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            return f.read()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PDF ASSEMBLY (cover, content, TOC, merge)
# ─────────────────────────────────────────────────────────────────────────────

def _post_process_pdf(main_html, groups, lang_code="en"):
    """Generates PDF with manual TOC post-processing."""
    anchor_html = []
    for g_idx, group in enumerate(groups):
        g_id = f"GTOC-{g_idx}"
        group["anchor"] = g_id
        gb = 'style="page-break-before:always;"' if g_idx > 0 else ""

        parts = [f"<div {gb}>"]
        parts.append(f'<span style="color:#ffffff;font-size:1pt;line-height:0;">{g_id}</span>')

        if group["label"]:
            parts.append(f'<h1 class="group-name">{group["label"]}</h1>')

        for p_idx, page in enumerate(group["pages"]):
            p_id = f"PTOC-{g_idx}-{p_idx}"
            page["anchor"] = p_id
            p_div = f'<span style="color:#ffffff;font-size:1pt;line-height:0;">{p_id}</span>'
            pb = 'style="page-break-before:always;"' if (p_idx > 0 or (g_idx > 0 and not group["label"])) else ""
            tag = "h2" if group["label"] else "h1"
            title_with_num = f"{page['number']} {page['title']}" if page.get("number") else page["title"]
            parts.append(f'<div {pb}>{p_div}<{tag} class="page-title">{title_with_num}</{tag}>{page["content_html"]}</div>')

        parts.append("</div>")
        anchor_html.append("\n".join(parts))

    full_body = "\n".join(anchor_html)
    content_html = _inline_images(_wrap(full_body))

    # Cover / back-cover image pages
    def _cover_page_html(mime, encoded):
        return f"""
        <html><head><meta charset='UTF-8'><style>
            @page {{ size: A4; margin: 0; }}
            html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background-color: white; }}
            table {{ width: 100%; height: 100%; border-collapse: collapse; }}
            td {{ margin: 0; padding: 0; vertical-align: middle; text-align: center; }}
            img {{ width: 100%; height: auto; display: block; margin: 0; }}
        </style></head>
        <body><table><tr><td><img src="data:{mime};base64,{encoded}"></td></tr></table></body>
        </html>
        """

    cover_pdf_bin = None
    f_name = frappe.db.get_value("File", {"file_url": "/files/CrecheFrontpage.jpg"}, "name")
    if not f_name:
        f_name = frappe.db.get_value("File", {"file_name": ["like", "%CrecheFrontpage%"]}, "name")
    if f_name:
        f_doc = frappe.get_doc("File", f_name)
        content = f_doc.get_content()
        if content:
            encoded = base64.b64encode(content).decode()
            mime = "image/jpeg" if f_doc.file_name.lower().endswith((".jpg", ".jpeg")) else "image/png"
            cover_pdf_bin = _render_pdf(_cover_page_html(mime, encoded))

    back_cover_pdf_bin = None
    b_name = frappe.db.get_value("File", {"file_url": "/files/crechebackpage.jpg"}, "name")
    if not b_name:
        b_name = frappe.db.get_value("File", {"file_name": ["like", "%crechebackpage%"]}, "name")
    if b_name:
        b_doc = frappe.get_doc("File", b_name)
        b_content = b_doc.get_content()
        if b_content:
            b_encoded = base64.b64encode(b_content).decode()
            b_mime = "image/jpeg" if b_doc.file_name.lower().endswith((".jpg", ".jpeg")) else "image/png"
            back_cover_pdf_bin = _render_pdf(_cover_page_html(b_mime, b_encoded))

    # Content
    content_pdf = _render_pdf(content_html)
    if not content_pdf:
        frappe.log_error("Content PDF generation failed (empty result)")
        return b""

    # Locate anchors for page numbers
    reader = PdfReader(io.BytesIO(content_pdf))
    page_map = {}
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        matches = re.findall(r"[GP]TOC[\s]*-[\s]*\d+(?:[\s]*-[\s]*\d+)?", text)
        for m in matches:
            key = re.sub(r"\s+", "", m)
            if key not in page_map:
                page_map[key] = i + 1

    if not page_map:
        frappe.log_error("PDF manual indexing failed: No anchors found in content PDF")

    def build_toc(shift=0):
        toc_lines = [f'<h1>{TOC_TITLES.get(lang_code, "Table of Contents")}</h1><div class="toc-container">']
        for g_idx, group in enumerate(groups):
            if group["label"]:
                p_num = page_map.get(group["anchor"], 1) + shift
                title = f"{group['number']}. {group['label']}"
                toc_lines.append(f'<div class="toc-item level-0"><span class="toc-page">{p_num}</span><span class="toc-title">{title}</span><div class="toc-line"></div></div>')
            for p_idx, page in enumerate(group["pages"]):
                p_num = page_map.get(page["anchor"], 1) + shift
                title = f"{page['number']} {page['title']}" if page.get("number") else page["title"]
                level = "level-1" if group["label"] else "level-0"
                toc_lines.append(f'<div class="toc-item {level}"><span class="toc-page">{p_num}</span><span class="toc-title">{title}</span><div class="toc-line"></div></div>')
        toc_lines.append("</div>")
        return f"<html><head><meta charset='UTF-8'><style>{_embedded_font_face_css()}</style>{TOC_STYLE}</head><body>{''.join(toc_lines)}</body></html>"

    # Pass 1: estimate TOC size, pass 2: final TOC with correct shift
    toc_pdf = _render_pdf(build_toc(0))
    toc_page_count = len(PdfReader(io.BytesIO(toc_pdf)).pages)

    skip_c = 1 if cover_pdf_bin else 0
    shift_amount = skip_c + toc_page_count
    toc_pdf = _render_pdf(build_toc(shift_amount))
    toc_reader = PdfReader(io.BytesIO(toc_pdf))

    # Merge
    writer = PdfWriter()

    if cover_pdf_bin:
        cover_reader = PdfReader(io.BytesIO(cover_pdf_bin))
        for page in cover_reader.pages:
            writer.add_page(page)

    for page in toc_reader.pages:
        writer.add_page(page)

    for page in reader.pages:
        writer.add_page(page)

    if back_cover_pdf_bin:
        back_reader = PdfReader(io.BytesIO(back_cover_pdf_bin))
        for page in back_reader.pages:
            writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)

    return _add_page_numbers(output.getvalue(), skip_first=True, skip_last=True, skip_count=skip_c)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENDPOINTS — same names/signatures as the live wiki_pdf.pdf module
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def check_wiki_pdf_status(lang="en"):
    """Lightweight check — returns {ready, url} without streaming the file through Python."""
    lang_code = get_normalized_lang(lang)
    cache_fname = f"WikiPDF_DailyCache_{lang_code}.pdf"
    file_path = os.path.join(frappe.get_site_path("public", "files"), cache_fname)

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        return {"ready": True, "url": f"/files/{cache_fname}"}

    try:
        from wiki_pdf.tasks import _enqueue_language
        _enqueue_language(lang, lang_code)
    except Exception:
        pass

    return {"ready": False, "url": None}


@frappe.whitelist(allow_guest=True)
def download_wiki_pdf(page_name=None, route=None, lang="en"):
    try:
        lang_code = get_normalized_lang(lang)
        pdf_bin = _load_pdf_from_cache(f"WikiPDF_DailyCache_{lang_code}.pdf")
        if pdf_bin:
            frappe.local.response.filename = "Creche_Guideline.pdf"
            frappe.local.response.filecontent = pdf_bin
            frappe.local.response.type = "download"
            return

        try:
            from wiki_pdf.tasks import _enqueue_language
            _enqueue_language(lang, lang_code)
        except Exception:
            pass

        frappe.throw(
            "The PDF for this language is being prepared in the background. "
            "Please try again in a few minutes."
        )

    except frappe.exceptions.ValidationError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Wiki PDF Error")
        frappe.throw(f"Error: {str(e)}")


@frappe.whitelist(allow_guest=True)
def download_full_wiki_space(wiki_space, lang="en"):
    """Download entire space by wiki_space route name."""
    try:
        root_name = frappe.get_doc("Wiki Page", {"route": wiki_space}, ignore_permissions=True).name

        lang_code = get_normalized_lang(lang)
        fallback_space_cache_fname = f"WikiPDF_SpaceCached_{root_name.replace(' ', '_')}_{lang_code}.pdf"
        for cache_fname in [f"WikiPDF_DailyCache_{lang_code}.pdf", fallback_space_cache_fname]:
            file_doc_name = frappe.db.get_value("File", {"file_name": cache_fname}, "name")
            if file_doc_name:
                cached_file = frappe.get_doc("File", file_doc_name)
                frappe.local.response.filename = "Creche_Guideline.pdf"
                frappe.local.response.filecontent = cached_file.get_content()
                frappe.local.response.type = "download"
                return

        all_pages = frappe.get_all(
            "Wiki Page",
            filters={"published": 1},
            fields=["name", "title", "content", "parent_wiki_page"],
            order_by="creation asc",
            ignore_permissions=True,
            limit=0,
        )
        pages = [p for p in all_pages if p.name == root_name or p.parent_wiki_page == root_name]

        if not pages:
            frappe.throw(_("No content found to generate PDF"))

        processed_pages = []
        for p in pages:
            raw_html = _md_to_html(p.content or "")
            if lang and lang != "en":
                translated_html = translate_html(raw_html, lang)
                translated_title = translate_text(p.title, lang)
            else:
                translated_html = raw_html
                translated_title = p.title

            processed_pages.append({"title": translated_title, "content_html": _clean_for_pdf(translated_html)})

        pdf_bin = _post_process_pdf(None, [{"label": None, "pages": processed_pages}])

        try:
            _save_pdf_to_cache(fallback_space_cache_fname, pdf_bin)
        except Exception:
            pass

        frappe.local.response.filename = "Creche_Guideline.pdf"
        frappe.local.response.filecontent = pdf_bin
        frappe.local.response.type = "download"

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Wiki Full Space PDF Error")
        frappe.throw(f"Error: {str(e)}")
