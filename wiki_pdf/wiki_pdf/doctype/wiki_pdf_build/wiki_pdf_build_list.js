frappe.listview_settings["Wiki PDF Build"] = {
	get_indicator(doc) {
		const colors = {
			"Up to Date": "green",
			Outdated: "orange",
			Queued: "blue",
			Updating: "blue",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},

	onload(listview) {
		if (frappe.user.has_role("System Manager")) {
			listview.page.add_inner_button(__("Rebuild All Languages"), () => {
				frappe.confirm(
					__(
						"Rebuild the PDF for all languages now? Only pages that changed since their last translation are sent to the translator (paid); unchanged pages come from the cache."
					),
					() => {
						frappe
							.call({
								method: "wiki_pdf.tasks.trigger_pdf_generation",
								freeze: true,
								freeze_message: __("Queuing PDF builds..."),
							})
							.then((r) => {
								const results = Object.values(r.message || {});
								const queued = results.filter((s) => s === "queued").length;
								const running = results.filter((s) => s === "running").length;
								frappe.show_alert(
									{
										message: __("{0} language(s) queued, {1} already building.", [
											queued,
											running,
										]),
										indicator: "green",
									},
									7
								);
								listview.refresh();
							});
					}
				);
			});
		}

		// Builds update their records directly (no realtime events), so refresh
		// the list while it's open to show status changes as they happen.
		const route = frappe.get_route_str();
		const timer = setInterval(() => {
			if (frappe.get_route_str() !== route) {
				clearInterval(timer);
				return;
			}
			if (!document.hidden) listview.refresh();
		}, 15000);
	},
};
