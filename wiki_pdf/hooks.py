app_name = "wiki_pdf"
app_title = "Wiki PDF"
app_publisher = "Mercy Selvanayagi"
app_description = "Custom App for Wiki PDF Download"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = "mercy.selvanayagi@azimpremjifoundation.org"
app_license = "MIT"

# A bundle, so every build gets a new hashed URL and browsers never keep
# running a stale cached copy (plain /assets files are cached for 12 hours).
web_include_js = "wiki_pdf.bundle.js"
# Default look of all wiki page content (font, heading colour, justified text)
web_include_css = "wiki_content.bundle.css"

# Edits only mark the translated PDFs as outdated; they don't start a build
# (that happens on download, the Friday job, or an admin trigger).
doc_events = {
    "Wiki Page": {
        "on_update": "wiki_pdf.tasks.on_wiki_content_change",
        "on_trash": "wiki_pdf.tasks.on_wiki_content_change",
    },
    "Wiki Space": {
        "on_update": "wiki_pdf.tasks.on_wiki_content_change",
    },
}

scheduler_events = {
    # Friday 6am (site timezone) so content edits made Mon-Thu are picked up,
    # and the run finishes well before anyone is back editing next week.
    "cron": {
        "0 6 * * 5": [
            "wiki_pdf.tasks.generate_weekly_translated_pdfs"
        ]
    }
}

