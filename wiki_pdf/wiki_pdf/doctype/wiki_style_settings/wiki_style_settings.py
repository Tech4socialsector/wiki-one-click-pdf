"""Wiki Style Settings: the default look of wiki page content.

The settings are turned into a small stylesheet that update_website_context
(hooks.py) adds to every web page's <head>, so a change applies as soon as
it's saved: no build step, and no stale cached CSS file in browsers.
"""

import re

import frappe
from frappe.model.document import Document

DOCTYPE = "Wiki Style Settings"
CSS_CACHE_KEY = "wiki_style_settings_head"

# Page content: published page, editor live preview, "review changes" preview.
CONTENT = (".wiki-content", ".markdown-preview", ".preview-content")

FONT_STACKS = {
    "Tahoma": "Tahoma, sans-serif",
    "Arial": "Arial, Helvetica, sans-serif",
    "Verdana": "Verdana, Geneva, sans-serif",
    "Georgia": "Georgia, serif",
    "Times New Roman": "'Times New Roman', Times, serif",
    "Poppins": "'Poppins', sans-serif",
    "Roboto": "'Roboto', sans-serif",
    "Open Sans": "'Open Sans', sans-serif",
    "Lato": "'Lato', sans-serif",
    "Inter": "'Inter', sans-serif",
    "Montserrat": "'Montserrat', sans-serif",
    "Noto Sans": "'Noto Sans', sans-serif",
    "Site Default": None,
}

# Web fonts most readers won't have installed: loaded from Google Fonts.
GOOGLE_FONTS = {"Poppins", "Roboto", "Open Sans", "Lato", "Inter", "Montserrat", "Noto Sans"}

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FONT_RE = re.compile(r"^[A-Za-z0-9 ,'\"-]+$")


class WikiStyleSettings(Document):
    def validate(self):
        if self.font == "Custom" and self.custom_font and not _FONT_RE.match(self.custom_font.strip()):
            frappe.throw(
                frappe._("Custom Font may only contain letters, numbers, spaces, commas, quotes and hyphens.")
            )
        for field in ("font_size", "h1_font_size", "h2_font_size", "h3_font_size", "h4_font_size"):
            if (self.get(field) or 0) < 0:
                frappe.throw(frappe._("{0} can't be negative.").format(self.meta.get_label(field)))

    def on_update(self):
        frappe.cache().delete_value(CSS_CACHE_KEY)
        # The PDFs follow these settings too: the source fingerprint includes
        # them, so drop the cached one to mark the PDFs outdated straight away.
        frappe.cache().delete_value("wiki_pdf_source_fingerprint")
        from wiki_pdf.tasks import _mark_outdated_after_commit

        frappe.db.after_commit.add(_mark_outdated_after_commit)
        # Pages rendered for guests may be cached as a whole; drop them so the
        # new styles show straight away.
        from frappe.website.utils import clear_cache

        clear_cache()


def update_website_context(context):
    """hooks.update_website_context: append the font link and generated
    styles to <head>."""
    try:
        head = get_head_html()
    except Exception:
        # Styling must never break a page.
        frappe.logger().error(f"Wiki Style Settings: could not build CSS: {frappe.get_traceback()}")
        return
    if head:
        return {"head_html": (context.get("head_html") or "") + head}


def get_head_html():
    head = frappe.cache().get_value(CSS_CACHE_KEY)
    if head is None:
        head = build_head_html(frappe.get_cached_doc(DOCTYPE))
        frappe.cache().set_value(CSS_CACHE_KEY, head)
    return head


def build_head_html(s):
    css = build_css(s)
    if not css:
        return ""
    head = ""
    family = _google_font_family(s)
    if family:
        url = f"https://fonts.googleapis.com/css2?family={family.replace(' ', '+')}:wght@400;500;600;700&display=swap"
        head += (
            '\n<link rel="preconnect" href="https://fonts.googleapis.com">'
            '\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            f'\n<link rel="stylesheet" href="{url}" id="wiki-style-settings-font">'
        )
    return head + f'\n<style id="wiki-style-settings">{css}</style>'


def _google_font_family(s):
    """The Google Fonts family to load, if any: a listed web font, or the
    first name in a Custom font list when "Load from Google Fonts" is ticked."""
    if not s.enabled:
        return None
    if s.font in GOOGLE_FONTS:
        return s.font
    if s.font == "Custom" and s.load_custom_font and s.custom_font and _FONT_RE.match(s.custom_font.strip()):
        first = s.custom_font.split(",")[0].strip().strip("'\"").strip()
        if first and re.fullmatch(r"[A-Za-z0-9 ]+", first) and first.lower() not in _GENERIC_FAMILIES:
            return first
    return None


_GENERIC_FAMILIES = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}


def build_css(s):
    if not s.enabled:
        return ""
    rules = []

    def rule(selectors, declarations):
        declarations = [d for d in declarations if d]
        if declarations:
            rules.append(f"{', '.join(selectors)} {{ {' '.join(declarations)} }}")

    def within(suffix, prefix=""):
        return [f"{prefix}{c}{suffix}" for c in CONTENT]

    font = _font_stack(s)
    color = _color(s.text_color)
    rule(within(""), [
        f"font-family: {font};" if font else None,
        f"font-size: {int(s.font_size)}px;" if s.font_size else None,
        f"line-height: {float(s.line_height):g};" if s.line_height else None,
        f"color: {color};" if color else None,
    ])

    align = _align(s.paragraph_align)
    pcolor = _color(s.paragraph_color)
    rule(within(" p"), [
        f"text-align: {align};" if align else None,
        f"color: {pcolor};" if pcolor else None,
    ])

    for n in (1, 2, 3, 4):
        selectors = within(f" h{n}")
        if n == 1 and s.style_page_title:
            selectors.append(".wiki-title")
        hcolor = _color(s.get(f"h{n}_color"))
        halign = _align(s.get(f"h{n}_align"))
        size = s.get(f"h{n}_font_size")
        weight = {"Normal": "400", "Bold": "700"}.get(s.get(f"h{n}_bold"))
        # !important: the wiki's own stylesheet sets heading styles too.
        rule(selectors, [
            f"font-family: {font} !important;" if font else None,
            f"color: {hcolor} !important;" if hcolor else None,
            f"text-align: {halign} !important;" if halign else None,
            f"font-size: {int(size)}px !important;" if size else None,
            f"font-weight: {weight} !important;" if weight else None,
        ])

    link = within(" a:not(.btn)")
    hover = within(" a:not(.btn):hover")
    rule(link, [
        f"color: {_color(s.link_color)} !important;" if _color(s.link_color) else None,
        "text-decoration: underline; text-underline-offset: 2px;" if s.link_underline else "text-decoration: none;",
    ])
    if _color(s.link_hover_color):
        rule(hover, [f"color: {_color(s.link_hover_color)} !important;"])
    # The wiki's dark mode puts a "dark" class on <body>.
    if _color(s.dark_link_color):
        rule(within(" a:not(.btn)", "body.dark "), [f"color: {_color(s.dark_link_color)} !important;"])
    if _color(s.dark_link_hover_color):
        rule(within(" a:not(.btn):hover", "body.dark "), [f"color: {_color(s.dark_link_hover_color)} !important;"])

    if s.custom_css:
        # Keep it inside the <style> tag no matter what was typed.
        rules.append(s.custom_css.replace("</", "<\\/"))

    return "\n".join(rules)


def _font_stack(s):
    if s.font == "Custom":
        custom = (s.custom_font or "").strip()
        if not custom or not _FONT_RE.match(custom):
            return None
        # A bare name like Poppins: quote it and add a fallback.
        if "," not in custom:
            name = custom.strip("'\"").strip()
            return f"'{name}', sans-serif" if name.lower() not in _GENERIC_FAMILIES else name
        return custom
    return FONT_STACKS.get(s.font)


def _color(value):
    value = (value or "").strip()
    return value if _COLOR_RE.match(value) else None


def _align(value):
    return {"Left": "left", "Center": "center", "Right": "right", "Justify": "justify"}.get(value)


def build_pdf_css(s):
    """The same look for the translated PDF: heading colours and alignment,
    paragraph alignment, line spacing and link colour. Returns
    (font_stylesheet_url or None, latin_font_family or None, css). The font
    family isn't in the css: the PDF orders fonts per language (see
    wiki_pdf.pdf._font_family_for). Sizes stay the PDF's own (pt), as web px
    sizes don't map well to print."""
    if not s or not s.enabled:
        return None, None, ""
    rules = []

    family = _google_font_family(s)
    font_url = (
        f"https://fonts.googleapis.com/css2?family={family.replace(' ', '+')}:wght@400;600;700&display=swap"
        if family else None
    )
    stack = _font_stack(s)
    latin_family = stack.split(",")[0].strip() if stack else None

    if s.line_height:
        rules.append(f"body {{ line-height: {float(s.line_height):g}; }}")
    if _color(s.text_color):
        rules.append(f"body {{ color: {_color(s.text_color)}; }}")

    align = _align(s.paragraph_align)
    pcolor = _color(s.paragraph_color)
    p_decl = " ".join(d for d in (
        f"text-align: {align};" if align else None,
        f"color: {pcolor};" if pcolor else None,
    ) if d)
    if p_decl:
        rules.append(f"p, li {{ {p_decl} }}")
        # Narrow table cells read badly justified.
        rules.append("th p, td p, th li, td li { text-align: left; }")

    # Content headings only: the PDF's own page titles / section names keep
    # their style unless "Also Style the Page Title" is ticked.
    for n in (1, 2, 3, 4):
        selector = f"h{n}:not(.page-title):not(.group-name)"
        if n == 1 and s.style_page_title:
            selector += ", .page-title"
        hcolor = _color(s.get(f"h{n}_color"))
        halign = _align(s.get(f"h{n}_align"))
        weight = {"Normal": "400", "Bold": "700"}.get(s.get(f"h{n}_bold"))
        decl = " ".join(d for d in (
            f"color: {hcolor} !important;" if hcolor else None,
            f"text-align: {halign};" if halign else None,
            f"font-weight: {weight};" if weight else None,
        ) if d)
        if decl:
            rules.append(f"{selector} {{ {decl} }}")

    lcolor = _color(s.link_color)
    rules.append(
        "a { "
        + (f"color: {lcolor}; " if lcolor else "")
        + ("text-decoration: underline; " if s.link_underline else "text-decoration: none; ")
        + "}"
    )
    return font_url, latin_family, "\n".join(rules)


def pdf_style_signature():
    """Changes when the PDF-relevant style settings change; part of the PDF
    source fingerprint, so a style change marks every PDF outdated (they are
    re-created from cached translations, nothing is re-translated)."""
    import hashlib

    try:
        font_url, latin_family, css = build_pdf_css(frappe.get_cached_doc(DOCTYPE))
    except Exception:
        return ""
    return hashlib.sha256(f"{font_url}|{latin_family}|{css}".encode()).hexdigest()[:16]
