frappe.ui.form.on("Wiki Style Settings", {
	refresh(frm) {
		frm.add_custom_button(__("View Wiki"), () => window.open("/", "_blank"));
		frm.set_intro(
			__(
				"Changes apply to every wiki page as soon as you save. Reload an open wiki page to see them."
			),
			"blue"
		);
	},
});
