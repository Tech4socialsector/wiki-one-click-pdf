app_name = "wiki_pdf"
app_title = "Wiki PDF"
app_publisher = "Mercy Selvanayagi"
app_description = "Custom App for Wiki PDF Download"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = "mercy.selvanayagi@azimpremjifoundation.org"
app_license = "MIT"

web_include_js = "/assets/wiki_pdf/js/wiki_pdf.js"

scheduler_events = {
    # Friday 6am (site timezone) so content edits made Mon-Thu are picked up,
    # and the run finishes well before anyone is back editing next week.
    "cron": {
        "0 6 * * 5": [
            "wiki_pdf.tasks.generate_weekly_translated_pdfs"
        ]
    }
}

