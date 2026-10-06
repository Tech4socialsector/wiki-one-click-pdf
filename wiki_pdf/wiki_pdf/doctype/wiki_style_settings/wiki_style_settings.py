"""Wiki Style Settings: the default look of wiki page content.

The settings are turned into a small stylesheet that update_website_context
(hooks.py) adds to every web page's <head>, so a change applies as soon as
it's saved: no build step, and no stale cached CSS file in browsers.
"""

import re

import frappe
from frappe.model.document import Document

DOCTYPE = "Wiki Style Settings"
CSS_CACHE_KEY = "wiki_style_settings_css"

# Page content: published page, editor live preview, "review changes" preview.
CONTENT = (".wiki-content", ".markdown-preview", ".preview-content")

FONT_STACKS = {
    "Tahoma": "Tahoma, sans-serif",
    "Arial": "Arial, Helvetica, sans-serif",
    "Verdana": "Verdana, Geneva, sans-serif",
    "Georgia": "Georgia, serif",
    "Times New Roman": "'Times New Roman', Times, serif",
    "Noto Sans": "'Noto Sans', sans-serif",
    "Site Default": None,
}

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
        # Pages rendered for guests may be cached as a whole; drop them so the
        # new styles show straight away.
        from frappe.website.utils import clear_cache

        clear_cache()


def update_website_context(context):
    """hooks.update_website_context: append the generated styles to <head>."""
    try:
        css = get_css()
    except Exception:
        # Styling must never break a page.
        frappe.logger().error(f"Wiki Style Settings: could not build CSS: {frappe.get_traceback()}")
        return
    if css:
        return {"head_html": (context.get("head_html") or "") + f'\n<style id="wiki-style-settings">{css}</style>'}


def get_css():
    css = frappe.cache().get_value(CSS_CACHE_KEY)
    if css is None:
        css = build_css(frappe.get_cached_doc(DOCTYPE))
        frappe.cache().set_value(CSS_CACHE_KEY, css)
    return css


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
        return custom if custom and _FONT_RE.match(custom) else None
    return FONT_STACKS.get(s.font)


def _color(value):
    value = (value or "").strip()
    return value if _COLOR_RE.match(value) else None


def _align(value):
    return {"Left": "left", "Center": "center", "Right": "right", "Justify": "justify"}.get(value)
