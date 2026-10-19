"""Adds the Download PDF button script and the Wiki Style Settings to wiki
pages on Wiki 3 (Frappe 16).

Wiki 3 renders pages with its own standalone layout (WikiDocumentRenderer +
templates/wiki/layout.html), which loads neither `web_include_js` nor the
website context that `update_website_context` fills in, so on Wiki 3 the
assets are added to the HTML response instead. Wiki 2 pages still get them
the old way and are left untouched here.
"""

import frappe

# Marks pages that already have the assets, so they're never added twice.
MARKER = "<!-- wiki_pdf assets -->"
# Only Wiki 3's page layout has this; Wiki 2 uses class="wiki-content".
WIKI3_CONTENT = 'id="wiki-content"'


def add_assets_to_wiki_pages(response=None, request=None):
    """after_request hook. Must never break a page, whatever goes wrong."""
    try:
        if response is None or response.status_code != 200 or response.direct_passthrough:
            return
        if not (response.mimetype or "").startswith("text/html"):
            return
        body = response.get_data(as_text=True)
        if WIKI3_CONTENT not in body or MARKER in body or "</head>" not in body:
            return
        response.set_data(body.replace("</head>", _assets_html() + "</head>", 1))
    except Exception:
        frappe.logger().error(f"Wiki PDF: could not add assets to a wiki page: {frappe.get_traceback()}")


def _assets_html():
    from frappe.utils.jinja_globals import bundled_asset

    from wiki_pdf.wiki_pdf.doctype.wiki_style_settings.wiki_style_settings import get_head_html

    script = f'<script defer src="{bundled_asset("wiki_pdf.bundle.js")}"></script>'
    return f"{MARKER}{get_head_html()}\n{script}\n"
