// While a build runs, show "45 / 79 pages done · about 20 min left" and keep it fresh.
function show_build_progress(frm) {
	clearTimeout(frm._progress_timer);
	if (!["Updating", "Queued"].includes(frm.doc.status)) return;
	frappe
		.call({ method: "wiki_pdf.api.get_build_progress", args: { lang: frm.doc.language } })
		.then((r) => {
			const p = r.message;
			if (p) {
				const mins = Math.max(1, Math.round(p.eta_seconds / 60));
				const left = mins >= 60 ? `${Math.floor(mins / 60)} h ${mins % 60} min` : `${mins} min`;
				const what =
					p.phase === "rendering"
						? __("All pages translated; creating the PDF.")
						: __("{0} / {1} pages done ({2} of {3} that need translating).", [
								p.done,
								p.total,
								p.translated,
								p.to_translate,
						  ]);
				frm.set_intro(`${what} ${__("About {0} left.", [left])}`, "blue");
			} else if (frm.doc.status === "Queued") {
				frm.set_intro(__("Waiting in the queue to start."), "blue");
			}
			frm._progress_timer = setTimeout(() => {
				if (frappe.get_route_str() === `Form/Wiki PDF Build/${frm.doc.name}`) frm.reload_doc();
			}, 15000);
		});
}

frappe.ui.form.on("Wiki PDF Build", {
	refresh(frm) {
		show_build_progress(frm);
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
