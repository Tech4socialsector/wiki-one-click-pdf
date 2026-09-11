import frappe
import pdfkit
import markdown2
import base64
import functools
import os
import re
import tempfile
import io
import urllib.request
from bs4 import BeautifulSoup
from pypdf import PdfReader, PdfWriter
from frappe.core.doctype.file.utils import find_file_by_url
from urllib.parse import urlparse, unquote
from frappe import _



from bs4 import BeautifulSoup, NavigableString

# Supported language codes — used for normalization
LANGUAGES = {
    "en": "English", "kn": "Kannada", "ta": "Tamil", "hi": "Hindi",
    "te": "Telugu", "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati",
    "ml": "Malayalam", "ur": "Urdu", "pa": "Punjabi", "or": "Odia",
    "as": "Assamese", "sa": "Sanskrit", "gom": "Konkani", "doi": "Dogri",
    "mai": "Maithili", "mni-mtei": "Meitei", "ne": "Nepali",
    "sat": "Santali", "sd": "Sindhi", "tcy": "Tulu",
}

_groq_client = None

def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        key = frappe.conf.get("gemini_api_key")
        if not key:
            raise ValueError("Groq API key not configured. Run: bench --site <site> set-config gemini_api_key <key>")
        _groq_client = Groq(api_key=key)
    return _groq_client


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


def _groq_translate_batch(texts, lang_code):
    """Translate a list of strings to lang_code using Groq. Returns list of same length."""
    import json as _json, time
    lang_name = LANGUAGES.get(lang_code, lang_code)
    client = _get_groq_client()
    texts_json = _json.dumps(texts, ensure_ascii=False)
    prompt = (
        f"You are translating a childcare/creche protocol guide for anganwadi workers in India.\n"
        f"Translate each text in this JSON array to {lang_name}.\n"
        f"Rules:\n"
        f"- Return ONLY a valid JSON array with exactly {len(texts)} elements in the same order\n"
        f"- Keep proper nouns like ICDS, VHSND, Anganwadi, creche unchanged\n"
        f"- Use simple language that field workers understand\n"
        f"- No explanations, no markdown formatting, just the JSON array\n\n"
        f"{texts_json}"
    )
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
                temperature=0.1,
            )
            result = response.choices[0].message.content.strip()
            # Strip markdown code fences if model adds them
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            translated = _json.loads(result)
            if isinstance(translated, list) and len(translated) == len(texts):
                return [str(t) for t in translated]
            raise ValueError(f"Count mismatch: expected {len(texts)}, got {len(translated)}")
        except Exception as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
            else:
                frappe.log_error(f"Groq batch translation failed (lang={lang_code}): {e}", "Translation Error")
    return texts  # fallback: return originals on all failures


def translate_text(text, lang="en"):
    lang_code = get_normalized_lang(lang)
    if not text or lang_code == "en":
        return text
    try:
        result = _groq_translate_batch([text], lang_code)
        return result[0] if result else text
    except Exception as e:
        frappe.logger().warning(f"Translation failed for lang {lang_code}: {e}")
        return text


def _recreate_translator():
    pass  # no-op — kept so tasks.py import doesn't break


def translate_html(html_content, lang="en"):
    """Translates text nodes in HTML using Groq LLM in JSON batches."""
    import time
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

        for batch_nodes, batch_texts in batches:
            translated = _groq_translate_batch(batch_texts, lang_code)
            for node, t in zip(batch_nodes, translated):
                node.replace_with(t)
            time.sleep(0.3)  # stay within Groq rate limits

        return str(soup)

    except Exception as e:
        frappe.log_error(f"translate_html failed (lang={lang_code}): {e}", "Translation Error")
        return html_content


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG & HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_MD_EXTRAS = ["tables", "fenced-code-blocks", "strike", "cuddled-lists", "break-on-newline", "header-ids", "footnotes"]

def _md_to_html(text):
    """Robust markdown to HTML conversion with table-hiding to avoid markdown2 crashes."""
    if not text: return ""
    try:
        # Standard attempt
        return markdown2.markdown(text, extras=_MD_EXTRAS)
    except AssertionError:
        # Markdown2 failed (likely due to a large HTML table). 
        # Hide tables, parse the rest, then re-insert.
        import re
        tables = []
        def _hide(match):
            tables.append(match.group(0))
            return f"\n\nPROTECTEDTABLE{len(tables)-1}\n\n"
        
        hidden_md = re.sub(r'(<table[^>]*>.*?</table>)', _hide, text, flags=re.DOTALL | re.IGNORECASE)
        try:
            html = markdown2.markdown(hidden_md, extras=_MD_EXTRAS)
            for i, table_html in enumerate(tables):
                placeholder = f"PROTECTEDTABLE{i}"
                # Handle potential markdown wrapping
                html = html.replace(f"<p>{placeholder}</p>", table_html)
                html = html.replace(placeholder, table_html)
            return html
        except Exception:
            return f"<pre>{frappe.utils.escape_html(text)}</pre>"
    except Exception as e:
        frappe.log_error(f"Markdown parsing error: {str(e)}", "Wiki PDF Markdown Error")
        return f"<div>Error parsing content: {frappe.utils.escape_html(text[:100])}...</div>"

def _find_page(route):
    """Robust lookup for Wiki Page by route."""
    if not route: return None
    route = route.strip("/")
    # Try exact, suffix, or containing match
    for query in [route, route.split("/")[-1], f"%/{route.split('/')[-1]}", f"%{route}%"]:
        # Use get_value with LIKE if query has %
        if "%" in str(query):
            match = frappe.db.get_value("Wiki Page", {"route": ["like", query]}, "name")
        else:
            match = frappe.db.get_value("Wiki Page", {"route": query}, "name")
        if match: return match
    return None

def _inline_images(html):
    """Ensure images work in PDF by resolving local paths or inlining base64."""
    if not html: return html
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or src.startswith("data:"): continue

        # 1x1 transparent pixel fallback
        fallback = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

        try:
            # Handle encoded URLs (spaces, etc.)
            real_src = unquote(src).strip()
            
            resolved_path = None
            # 1. Try to resolve via find_file_by_url for any local-looking path
            if not real_src.startswith("http") or real_src.startswith(frappe.utils.get_url()):
                # Get the path part (e.g., /files/foo.jpg)
                url_path = urlparse(real_src).path
                file_doc = find_file_by_url(url_path)
                if file_doc and os.path.exists(file_doc.get_full_path()):
                    resolved_path = os.path.abspath(file_doc.get_full_path())
            
            # 2. Fallback for standard /files/ if find_file_by_url failed
            if not resolved_path and real_src.startswith("/files/"):
                fname = real_src.split("/")[-1]
                path = frappe.get_site_path("public", "files", fname)
                if os.path.exists(path):
                    resolved_path = os.path.abspath(path)
                else:
                    # Try direct site path if get_site_path/public/files is tricky
                    alt_path = os.path.join(frappe.get_site_path(), "public", "files", fname)
                    if os.path.exists(alt_path):
                        resolved_path = os.path.abspath(alt_path)

            if resolved_path:
                # Use file:// prefix for absolute paths to be extremely clear for wkhtmltopdf
                img["src"] = f"file://{resolved_path}"
            else:
                # If it's a relative path, maybe try to join with site path
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
        m = re.search(r'(<thead[^>]*>.*?</thead>)', table_html, re.DOTALL | re.IGNORECASE)
        if m: return m.group(1)
        first = re.search(r'(<tr[^>]*>.*?</tr>)', table_html, re.DOTALL | re.IGNORECASE)
        return f'<thead>{first.group(1)}</thead>' if first else ''

    def _get_colgroup(table_html):
        m = re.search(r'(<colgroup[^>]*>.*?</colgroup>)', table_html, re.DOTALL | re.IGNORECASE)
        return m.group(1) if m else ''

    def _get_tbody_rows(table_html):
        tbody = re.search(r'<tbody[^>]*>(.*?)</tbody>', table_html, re.DOTALL | re.IGNORECASE)
        src = tbody.group(1) if tbody else table_html
        return re.findall(r'<tr[^>]*>.*?</tr>', src, re.DOTALL | re.IGNORECASE)

    # Force each chunk to stay on one page if possible (Atomic Chunks)
    TABLE_STYLE = 'width:100%;border-collapse:collapse;table-layout:fixed;font-size:10pt;margin:0;page-break-inside:avoid !important;'

    def process_table(match):
        table_html = match.group(0)
        thead, colgroup, rows = _get_thead(table_html), _get_colgroup(table_html), _get_tbody_rows(table_html)
        if len(rows) <= max_rows:
            return re.sub(r'<table([^>]*)>', lambda m: f'<table{m.group(1)} style="{TABLE_STYLE}">', table_html, 1, re.IGNORECASE)

        chunks = [rows[i:i + max_rows] for i in range(0, len(rows), max_rows)]
        parts = []
        for idx, chunk in enumerate(chunks):
            continued = f'<div style="font-size:9pt;color:#555;text-align:right;margin-top:4pt;">(continued...)</div>' if idx > 0 else ''
            parts.append(f'{continued}<table style="{TABLE_STYLE}">{colgroup}{thead}<tbody>{"".join(chunk)}</tbody></table>')
        return '\n<div style="margin:4pt 0;"></div>\n'.join(parts)

    return re.sub(r'<table[^>]*>.*?</table>', process_table, html, flags=re.DOTALL | re.IGNORECASE)

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

    html = re.sub(r'<(iframe|video)[^>]*>.*?</\1>', replace_media, html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<details[^>]*>', '<div style="display:block;margin:6pt 0;">', html, flags=re.IGNORECASE)
    html = re.sub(r'</details>', '</div>', html, flags=re.IGNORECASE)
    html = re.sub(r'<summary[^>]*>', '<span style="font-weight:bold;">', html, flags=re.IGNORECASE)
    html = re.sub(r'</summary>', '</span><br>', html, flags=re.IGNORECASE)
    return _split_tables(html)

# ─────────────────────────────────────────────────────────────────────────────
# CSS & FOOTER
# ─────────────────────────────────────────────────────────────────────────────

PDF_CSS = """
@page { size: A4; margin: 15mm 18mm; }
body { font-family: 'Noto Sans Telugu', 'Noto Sans Kannada', 'Noto Sans Tamil', 'Noto Sans Devanagari', 'Noto Sans Bengali', 'Noto Sans Malayalam', 'Noto Sans Gujarati', 'Noto Sans Gurmukhi', 'Noto Sans Oriya', 'Noto Sans Arabic', Georgia, serif; font-size: 11pt; line-height: 1.4; color: #111; margin: 0; padding: 0; }
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
    "en": "Table of Contents",
    "kn": "ವಿಷಯ ಸೂಚಿ",
    "ta": "உள்ளடக்கம்",
    "hi": "विषय-सूची",
    "te": "విషయ సూచిక",
    "mr": "अनुक्रमणिका",
    "bn": "সূচিপত্র",
    "gu": "સામગ્રી સૂચિ",
    "ml": "ഉള്ളടക്കം",
    "ur": "فہرست مضامین",
    "pa": "ਸਮੱਗਰੀ ਸੂਚੀ",
    "or": "ବିଷୟ ସୂଚୀ",
    "as": "বিষয়বস্তুৰ তালিকা",
    "sa": "विषयसूची",
    "gom": "विषय सूची",
    "doi": "विषय-सूची",
    "mai": "विषय-सूची",
    "mni-mtei": "ꯋꯥꯈꯜ ꯑꯣꯏꯕꯒꯤ ꯄꯨꯛꯊꯣꯀꯄꯥ",
    "ne": "सामग्री तालिका",
    "sat": "ᱥᱟᱹᱦᱩᱛ ᱛᱟᱞᱤᱠᱟ",
    "sd": "مواد جي فهرست",
    "tcy": "ಪರಿವಿಡಿ",
}

# डिजाइन टोकन for manual TOC
TOC_STYLE = """
<style>
    body { font-family: 'Noto Sans Telugu', 'Noto Sans Kannada', 'Noto Sans Tamil', 'Noto Sans Devanagari', 'Noto Sans Bengali', 'Noto Sans Malayalam', 'Noto Sans Gujarati', 'Noto Sans Gurmukhi', 'Noto Sans Oriya', 'Noto Sans Arabic', Georgia, serif; padding: 20mm; margin: 0; color: #111; }
    h1 { font-size: 24pt; font-weight: bold; border-bottom: 2px solid #333; padding-bottom: 10px; margin-bottom: 30px; }
    .toc-container { width: 100%; }
    .toc-item { clear: both; overflow: hidden; margin-bottom: 12pt; line-height: 1.2; }
    .toc-title { float: left; white-space: nowrap; padding-right: 5px; }
    .toc-page { float: right; white-space: nowrap; padding-left: 5px; font-weight: bold; color: #1a52a0; }
    /* This block fills the exactly gap between the floating title and floating page number */
    .toc-line { overflow: hidden; border-bottom: 1px solid #999; height: 1.0em; }
    .level-0 .toc-title { font-weight: bold; font-size: 13pt; }
    .level-1 { padding-left: 25px; }
    .level-1 .toc-title { font-size: 11pt; color: #444; }
</style>
"""

FOOTER_STYLE = """
<style>
    body { font-family: Georgia, serif; font-size: 10pt; text-align: center; margin: 0; padding: 0; background: transparent !important; }
</style>
"""

def _add_page_numbers(pdf_bin, skip_first=False, skip_last=False, skip_count=1):
    """Adds page numbers using a single pdfkit call for all pages (prevents worker timeout on large docs)."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bin))
        total_pages = len(reader.pages)

        # Build ONE html document with all footer pages
        footer_divs = []
        for i in range(total_pages):
            page_break = "page-break-after:always;" if i < total_pages - 1 else ""
            if (skip_first and i < skip_count) or (skip_last and i == total_pages - 1):
                footer_divs.append(f'<div style="{page_break}width:210mm;height:20mm;"></div>')
            else:
                # Adjust numbering so TOC (page 3) starts at a logical number if needed, 
                # but currently we just use the literal index + 1
                page_num = i + 1
                footer_divs.append(
                    f'<div style="{page_break}width:210mm;height:20mm;position:relative;font-family:Georgia,serif;font-size:10pt;">'
                    f'<div style="position:absolute;bottom:2mm;width:100%;text-align:center;">{page_num}</div>'
                    f'</div>'
                )

        all_footers_html = (
            "<html><head><meta charset='UTF-8'>"
            "<style>body{{margin:0;padding:0;background:transparent !important;}}</style>"
            "</head><body>"
            + "".join(footer_divs)
            + "</body></html>"
        )

        footer_pdf_bin = pdfkit.from_string(all_footers_html, False, options={
            "page-height": "20mm", "page-width": "210mm",
            "margin-top": "0", "margin-bottom": "0", "margin-left": "0", "margin-right": "0",
            "quiet": ""
        })

        if not footer_pdf_bin:
            return pdf_bin

        footer_reader = PdfReader(io.BytesIO(footer_pdf_bin))
        writer = PdfWriter()

        for i in range(total_pages):
            content_page = reader.pages[i]
            # CRITICAL: Don't merge if it's a skipped page (prevents white bar on covers)
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

def _post_process_pdf(main_html, groups, lang_code="en"):
    """Generates PDF with manual TOC post-processing."""
    # 1. Add invisible anchors to groups and pages
    anchor_html = []
    for g_idx, group in enumerate(groups):
        g_id = f"GTOC-{g_idx}"
        group["anchor"] = g_id
        # Apply page break to the group container itself (except for the first group)
        gb = 'style="page-break-before:always;"' if g_idx > 0 else ""
        
        parts = [f'<div {gb}>']
        # Anchor: inline, white text on white bg, 1pt — stays in PDF text layer for pypdf extraction
        parts.append(f'<span style="color:#ffffff;font-size:1pt;line-height:0;">{g_id}</span>')

        if group["label"]:
            parts.append(f'<h1 class="group-name">{group["label"]}</h1>')

        for p_idx, page in enumerate(group["pages"]):
            p_id = f"PTOC-{g_idx}-{p_idx}"
            page["anchor"] = p_id
            p_div = f'<span style="color:#ffffff;font-size:1pt;line-height:0;">{p_id}</span>'
            # If we already had a group label/header, we don't need another break for the first page
            pb = 'style="page-break-before:always;"' if (p_idx > 0 or (g_idx > 0 and not group["label"])) else ""
            tag = "h2" if group["label"] else "h1"
            title_with_num = f"{page['number']} {page['title']}" if page.get("number") else page["title"]
            parts.append(f'<div {pb}>{p_div}<{tag} class="page-title">{title_with_num}</{tag}>{page["content_html"]}</div>')
        
        parts.append('</div>')
        anchor_html.append("\n".join(parts))

    full_body = "\n".join(anchor_html)
    content_html = _inline_images(_wrap(full_body))
    
    # 2. Generate Cover & Title Pages
    from frappe.utils.pdf import get_pdf
    
    # 2a. Page 1: Image Cover
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
            image_html = f"""
            <html><head><meta charset='UTF-8'><style>
                html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background-color: white; }}
                table {{ width: 100%; height: 100%; border-collapse: collapse; }}
                td {{ margin: 0; padding: 0; vertical-align: middle; text-align: center; }}
                img {{ width: 100%; height: auto; display: block; margin: 0; }}
            </style></head>
            <body><table><tr><td><img src="data:{mime};base64,{encoded}"></td></tr></table></body>
            </html>
            """
            cover_pdf_bin = get_pdf(image_html, options={"page-size": "A4", "margin-top": "0", "margin-bottom": "0", "margin-left": "0", "margin-right": "0", "quiet": ""})

    # # 2b. Page 2: Text Title
    # title_html = """
    # <html><head><meta charset='UTF-8'><style>
    #     html, body { margin: 0; padding: 0; width: 100%; height: 100%; display: table; background-color: white; }
    #     .container { display: table-cell; vertical-align: middle; text-align: center; height: 100%; width: 100%; }
    #     h1 { font-family: Georgia, serif; font-size: 48pt; font-weight: bold; color: #111; margin: 0; }
    # </style></head>
    # <body><div class="container"><h1>Creche Guidelines</h1></div></body>
    # </html>
    # """
    # title_pdf_bin = get_pdf(title_html, options={"page-size": "A4", "margin-top": "0", "margin-bottom": "0", "margin-left": "0", "margin-right": "0", "quiet": ""})

    # 2c. Back Cover
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
            b_image_html = f"""
            <html><head><meta charset='UTF-8'><style>
                html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background-color: white; }}
                table {{ width: 100%; height: 100%; border-collapse: collapse; }}
                td {{ margin: 0; padding: 0; vertical-align: middle; text-align: center; }}
                img {{ width: 100%; height: auto; display: block; margin: 0; }}
            </style></head>
            <body><table><tr><td><img src="data:{b_mime};base64,{b_encoded}"></td></tr></table></body>
            </html>
            """
            back_cover_pdf_bin = get_pdf(b_image_html, options={"page-size": "A4", "margin-top": "0", "margin-bottom": "0", "margin-left": "0", "margin-right": "0", "quiet": ""})

    # 3. Generate content PDF
    content_pdf = pdfkit.from_string(content_html, False, options=_pdf_options(None))
    if not content_pdf:
        frappe.log_error("Content PDF generation failed (empty result)")
        return b""
    
    # 3. Index pages
    # Note: 1pt font causes wkhtmltopdf to position characters individually,
    # so pypdf may insert spaces/newlines inside anchor text (e.g. "GTOC -0", "PTOC\n-0-0").
    # The flexible regex allows optional whitespace between components; we then strip it.
    reader = PdfReader(io.BytesIO(content_pdf))
    page_map = {}
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        matches = re.findall(r'[GP]TOC[\s]*-[\s]*\d+(?:[\s]*-[\s]*\d+)?', text)
        for m in matches:
            key = re.sub(r'\s+', '', m)  # normalize: "GTOC -0" → "GTOC-0"
            if key not in page_map:
                page_map[key] = i + 1

    if not page_map:
        frappe.log_error("PDF manual indexing failed: No anchors found in content PDF")
                
    # 4. Generate TOC PDF
    def build_toc(shift=0):
        toc_lines = [f'<h1>{TOC_TITLES.get(lang_code, "Table of Contents")}</h1><div class="toc-container">']
        for g_idx, group in enumerate(groups):
            if group["label"]:
                p_num = page_map.get(group["anchor"], 1) + shift
                title = f"{group['number']}. {group['label']}"
                toc_lines.append(f'<div class="toc-item level-0"><span class="toc-page">{p_num}</span><span class="toc-title">{title}</span><div class="toc-line"></div></div>')
            for p_idx, page in enumerate(group["pages"]):
                p_num = page_map.get(page["anchor"], 1) + shift
                title = f"{page['number']} {page['title']}"
                level = "level-1" if group["label"] else "level-0"
                toc_lines.append(f'<div class="toc-item {level}"><span class="toc-page">{p_num}</span><span class="toc-title">{title}</span><div class="toc-line"></div></div>')
        toc_lines.append('</div>')
        return f"<html><head><meta charset='UTF-8'><style>{_oriya_font_face_css()}</style>{TOC_STYLE}</head><body>{''.join(toc_lines)}</body></html>"

    # Pass 1: Estimate TOC size
    toc_pdf = pdfkit.from_string(build_toc(0), False, options=_pdf_options(None))
    toc_page_count = len(PdfReader(io.BytesIO(toc_pdf)).pages)
    
    # Pass 2: Final TOC with correct page shifts (Cover + Title + TOC pages)
    skip_c = (1 if cover_pdf_bin else 0) 
    # + (1 if title_pdf_bin else 0)
    shift_amount = skip_c + toc_page_count
    toc_pdf = pdfkit.from_string(build_toc(shift_amount), False, options=_pdf_options(None))
    toc_reader = PdfReader(io.BytesIO(toc_pdf))
    
    # 5. Merge
    writer = PdfWriter()
    
    # Add Cover (Image Page)
    if cover_pdf_bin:
        cover_reader = PdfReader(io.BytesIO(cover_pdf_bin))
        for page in cover_reader.pages: writer.add_page(page)

    # # Add Title Page (Text)
    # if title_pdf_bin:
    #     title_reader = PdfReader(io.BytesIO(title_pdf_bin))
    #     for page in title_reader.pages: writer.add_page(page)

    # Add TOC
    for page in toc_reader.pages: writer.add_page(page)
    
    # Add Content
    for page in reader.pages: writer.add_page(page)

    # Add Back Cover
    if back_cover_pdf_bin:
        back_reader = PdfReader(io.BytesIO(back_cover_pdf_bin))
        for page in back_reader.pages: writer.add_page(page)
    
    output = io.BytesIO()
    writer.write(output)
    
    # 6. Final Pass: Add Page Numbers (skip cover and back cover)
    return _add_page_numbers(output.getvalue(), skip_first=True, skip_last=True, skip_count=skip_c)

FOOTER_HTML = """<!DOCTYPE html><html><head><script>
function subst() {
    var vars = {};
    var qs = document.location.search.substring(1).split('&');
    for (var i in qs) { if (qs.hasOwnProperty(i)) { var kv = qs[i].split('=', 2); vars[kv[0]] = decodeURI(kv[1]); } }
    var cls = ['page'];
    for (var c in cls) { if (cls.hasOwnProperty(c)) { 
        var els = document.getElementsByClassName(cls[c]);
        for (var j = 0; j < els.length; ++j) { els[j].textContent = vars[cls[c]]; }
    } }
}
</script></head><body style="margin:0;" onload="subst()">
<div style="font-family:Georgia,serif;font-size:10pt;text-align:center;width:100%;"><span class="page"></span></div>
</body></html>"""

def _compress_pdf_gs(pdf_bin, label=""):
    """Compress PDF using Ghostscript (font subsetting + image recompression).
    Falls back to original bytes if gs is unavailable or compression makes it larger.
    """
    import subprocess, tempfile
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
            frappe.logger().info(f"Wiki PDF {label}: gs {before_kb} KB → {after_kb} KB")
            return compressed if len(compressed) < len(pdf_bin) else pdf_bin
        frappe.logger().warning(
            f"Wiki PDF: gs failed (rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:200]}"
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


def _save_pdf_to_cache(cache_fname, pdf_bin):
    """Compress, write PDF to disk, and create/update a File record."""
    import os

    pdf_bin = _compress_pdf_gs(pdf_bin, label=cache_fname)

    file_path = os.path.join(frappe.get_site_path("public", "files"), cache_fname)
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(pdf_bin)
    os.replace(tmp_path, file_path)

    # Compression can take several minutes; ping DB before using it to avoid
    # "MySQL server has gone away" if the connection dropped during that time.
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
        frappe.db.set_value("File", existing, {
            "file_name": cache_fname,
            "file_size": file_size,
        })
    else:
        frappe.db.sql("""
            INSERT INTO `tabFile`
                (name, owner, creation, modified, modified_by,
                 file_name, file_url, file_size, is_private, folder)
            VALUES
                (%(name)s, %(user)s, %(now)s, %(now)s, %(user)s,
                 %(file_name)s, %(file_url)s, %(file_size)s, 0, 'Home/Attachments')
        """, {
            "name": frappe.generate_hash(length=10),
            "user": user,
            "now": now,
            "file_name": cache_fname,
            "file_url": file_url,
            "file_size": file_size,
        })
    frappe.db.commit()


def _load_pdf_from_cache(cache_fname):
    """Read cached PDF bytes from disk. Returns None if not found."""
    import os
    file_path = os.path.join(frappe.get_site_path("public", "files"), cache_fname)
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            return f.read()
    return None

def _write_footer():
    return None # Unpatched QT doesn't support this

def _write_toc_xsl():
    return None # Unpatched QT doesn't support this

def _pdf_options(footer_path, toc_xsl_path=None):
    opts = {
        "page-size": "A4", "margin-top": "15mm", "margin-bottom": "18mm", "margin-left": "18mm", "margin-right": "18mm",
        "encoding": "UTF-8", "quiet": "", "enable-local-file-access": "", 
        "enable-external-links": "", "no-stop-slow-scripts": "",
        "load-error-handling": "ignore", "load-media-error-handling": "ignore",
        # Reduce image DPI to keep file size smaller
        "image-dpi": "96",
        "image-quality": "70",
    }
    return opts

@functools.lru_cache(maxsize=1)
def _oriya_font_face_css():
    """Base64-embeds Noto Sans Oriya so Odia renders correctly.

    Frappe Cloud's wkhtmltopdf host doesn't have this font installed
    system-wide (unlike the other Indic scripts), so without this the
    'Noto Sans Oriya' family in PDF_CSS/TOC_STYLE silently falls through
    to Georgia/serif and every Odia glyph renders as a black .notdef box.
    """
    font_dir = os.path.join(os.path.dirname(__file__), "public", "fonts")
    faces = []
    for weight, fname in [("normal", "NotoSansOriya-Regular.ttf"), ("bold", "NotoSansOriya-Bold.ttf")]:
        path = os.path.join(font_dir, fname)
        try:
            with open(path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
            faces.append(
                f"@font-face {{ font-family: 'Noto Sans Oriya'; font-weight: {weight}; "
                f"src: url(data:font/truetype;base64,{encoded}) format('truetype'); }}"
            )
        except FileNotFoundError:
            frappe.logger().warning(f"Wiki PDF: font file missing: {path}")
    return "\n".join(faces)

def _wrap(body):
    return f"<html><head><meta charset='UTF-8'><style>{_oriya_font_face_css()}\n{PDF_CSS}</style></head><body>{body}</body></html>"

# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

# @frappe.whitelist(allow_guest=True)

# # def download_wiki_pdf(page_name=None, route=None):

# def download_wiki_pdf(page_name=None, route=None, lang="en"):
#     """Download single Wiki page or current page and its siblings in space."""
#     try:
#         target_name = page_name or _find_page(route)
#         if not target_name: frappe.throw(f"Wiki Page not found: {route or page_name}")

#         page_doc = frappe.get_doc("Wiki Page", target_name, ignore_permissions=True)
#         wiki_group_item = frappe.db.get_value("Wiki Group Item", {"wiki_page": target_name}, ["parent"], as_dict=True)

#         groups = []
#         if not wiki_group_item:
            
#                 raw_content = page_doc.content or ""
#                 translated_content = translate_text(raw_content, lang)

#                 translated_title = translate_text(page_doc.title, lang)

#                 groups.append({ "label": None, "pages": [{ "title": translated_title, "content_html": _clean_for_pdf(_md_to_html(translated_content)) }]
# })
            
#             # groups.append({"label": None, "pages": [{"title": page_doc.title, "content_html": _clean_for_pdf(_md_to_html(page_doc.content or ""))}]})
#         else:
#             sidebar = frappe.get_all("Wiki Group Item", filters={"parent": wiki_group_item.parent}, fields=["wiki_page", "parent_label"], order_by="idx asc", ignore_permissions=True, limit=0)
#             p_names = [s.wiki_page for s in sidebar if s.wiki_page]
#             p_map = {p.name: p for p in frappe.get_all("Wiki Page", filters={"name": ["in", p_names]}, fields=["name", "title", "content"], ignore_permissions=True, limit=0)}

#     group_counter = 1
#     for s in sidebar:
#                 if s.wiki_page in p_map:
#                     p = p_map[s.wiki_page]
#                     label = s.parent_label or ""
                    
#     if not groups or groups[-1]["label"] != label:
#             groups.append({"label": label, "number": group_counter, "anchor": f"GTOC-{group_counter}", "pages": []})
#             group_counter += 1
#             ref_counter = 1
                    
#             full_number = f"{groups[-1]['number']}.{ref_counter}"
#                     # groups[-1]["pages"].append({
#                     #     "number": full_number,
#                     #     "title": p.title,
#                     #     "anchor": f"PTOC-{full_number.replace('.', '-')}",
#                     #     "content_html": _clean_for_pdf(_md_to_html(p.content or ""))
#                     # })
#             raw_content = p.content or ""
#             translated_content = translate_text(raw_content, lang)

#             translated_title = translate_text(p.title, lang)

#             groups[-1]["pages"].append({
#                             "number": full_number,
#                             "title": translated_title,
#                             "anchor": f"PTOC-{full_number.replace('.', '-')}",
#                             "content_html": _clean_for_pdf(_md_to_html(translated_content))
#                        })
#             ref_counter += 1

#             if not groups or not any(g["pages"] for g in groups):
#                     frappe.throw(_("No content found to generate PDF"))

#             pdf_bin = _post_process_pdf(None, groups)

#         # Build filename
#     filename = "Creche Guideline"

#     frappe.local.response.filename = f"{filename or 'Wiki'}.pdf".replace(" ", "_")
#     frappe.local.response.filecontent = pdf_bin
    
#     frappe.local.response.type = "download"

# except Exception as e:
# frappe.log_error(frappe.get_traceback(), "Wiki PDF Error")
# frappe.throw(f"Error: {str(e)}")

@frappe.whitelist(allow_guest=True)
def check_wiki_pdf_status(lang="en"):
    """Lightweight check — returns {ready, url} without streaming the file through Python."""
    import os
    lang_code = get_normalized_lang(lang)
    cache_fname = f"WikiPDF_DailyCache_{lang_code}.pdf"
    file_path = os.path.join(frappe.get_site_path("public", "files"), cache_fname)

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        return {"ready": True, "url": f"/files/{cache_fname}"}

    # Not ready — trigger background generation for this language
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

        # PDF not cached — trigger generation for this language and tell user to wait
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



# @frappe.whitelist(allow_guest=True)
# # def download_full_wiki_space(wiki_space):
# def download_full_wiki_space(wiki_space, lang="en"):
#     """Download entire space by wiki_space route name."""
#     try:
#         root_name = frappe.get_doc("Wiki Page", {"route": wiki_space}, ignore_permissions=True).name

#         # Pages
#         all_pages = frappe.get_all("Wiki Page", filters={"published": 1}, fields=["name", "title", "content", "parent_wiki_page"], order_by="creation asc", ignore_permissions=True, limit=0)
#         pages = [p for p in all_pages if p.name == root_name or p.parent_wiki_page == root_name]

#         if not pages:
#             frappe.throw(_("No content found to generate PDF"))

#         # pdf_bin = _post_process_pdf(None, [{"label": None, "pages": pages}])

#         processed_pages = []

#         for p in pages:
#           raw_content = p.content or ""

#           if lang and lang != "en":
#             translated_content = translate_text(raw_content, lang)
#             translated_title = translate_text(p.title, lang)
#           else:
#             translated_content = raw_content
#             translated_title = p.title

#             processed_pages.append({
#               "title": translated_title,
#               "content_html": _clean_for_pdf(_md_to_html(translated_content))
#              })

#             pdf_bin = _post_process_pdf(None, [{"label": None, "pages": processed_pages}])

#             frappe.local.response.filename = f"Creche Guideline.pdf".replace(" ", "_")
#             frappe.local.response.filecontent = pdf_bin
#             frappe.local.response.type = "download"

#         except Exception as e:
#             frappe.log_error(frappe.get_traceback(), "Wiki Full Space PDF Error")
#             frappe.throw(f"Error: {str(e)}")

@frappe.whitelist(allow_guest=True)
def download_full_wiki_space(wiki_space, lang="en"):
    """Download entire space by wiki_space route name."""
    try:
        root_name = frappe.get_doc(
            "Wiki Page",
            {"route": wiki_space},
            ignore_permissions=True
        ).name

        # ✅ CHECK CACHE FIRST (daily pre-generated takes priority)
        lang_code = get_normalized_lang(lang)
        fallback_space_cache_fname = f"WikiPDF_SpaceCached_{root_name.replace(' ', '_')}_{lang_code}.pdf"
        for cache_fname in [
            f"WikiPDF_DailyCache_{lang_code}.pdf",
            fallback_space_cache_fname
        ]:
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
            limit=0
        )

        pages = [
            p for p in all_pages
            if p.name == root_name or p.parent_wiki_page == root_name
        ]

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

            # ✅ ALWAYS append (fixed indentation)
            processed_pages.append({
                "title": translated_title,
                "content_html": _clean_for_pdf(translated_html)
            })

        # ✅ Generate PDF OUTSIDE loop
        pdf_bin = _post_process_pdf(None, [{
            "label": None,
            "pages": processed_pages
        }])

        # ✅ SAVE CACHE
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
