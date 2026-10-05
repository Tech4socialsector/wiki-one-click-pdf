import frappe

from wiki_pdf.tasks import get_pdf_state


@frappe.whitelist(allow_guest=True)
def get_pdf(lang="en"):
    """Used by the Download PDF button. Returns the language's PDF status and,
    if one exists, the URL of the latest published PDF. Queues an update when
    the PDF is older than the current wiki content (deduplicated, safe to poll).

    status: "Up to Date" | "Queued" | "Updating" | "Failed"
    """
    return get_pdf_state(lang)
