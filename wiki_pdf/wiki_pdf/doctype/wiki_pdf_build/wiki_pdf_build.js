frappe.ui.form.on("Wiki PDF Build", {
	refresh(frm) {
		if (frm.doc.file_url) {
			frm.add_custom_button(__("Open PDF"), () => {
				window.open(`${frm.doc.file_url}?v=${frm.doc.build_number || 0}`, "_blank");
			});
		}

		if (!frappe.user.has_role("System Manager")) return;

		frm.add_custom_button(__("Rebuild This Language"), () => {
			frappe.confirm(
				__(
					"Rebuild the {0} PDF now? Only pages that changed since their last translation are sent to the translator (paid); unchanged pages come from the cache.",
					[frm.doc.language]
				),
				() => {
					frappe
						.call({
							method: "wiki_pdf.tasks.rebuild_language",
							args: { lang: frm.doc.language },
							freeze: true,
						})
						.then((r) => {
							frappe.show_alert(
								{
									message:
										r.message === "running"
											? __("A build is already running; it will rebuild again when it finishes.")
											: __("Rebuild queued."),
									indicator: "green",
								},
								7
							);
							frm.reload_doc();
						});
				}
			);
		}).addClass("btn-primary");
	},
});
