frappe.ui.form.on("AITS GST Settings", {
	refresh(frm) {
		const warning = frm.doc.__onload && frm.doc.__onload.local_ic_warning;
		frm.get_field("local_ic_warning").$wrapper.html(
			warning
				? `<div class="alert alert-warning" style="margin-bottom: 0">${frappe.utils.escape_html(warning)}</div>`
				: ""
		);

		if (!frm.is_new() && frm.doc.cloud_url) {
			frm.add_custom_button(__("Test Connection"), () => {
				frappe.call({
					method: "aitsgst.api.test_connection",
					freeze: true,
					freeze_message: __("Checking the GST service..."),
				}).then((r) => show_checks(r.message));
			});
		}
	},
});

function show_checks(result) {
	if (!result) return;
	const rows = (result.checks || [])
		.map(
			(c) =>
				`<tr><td>${c.ok ? "&#x2705;" : "&#x274C;"}</td><td>${frappe.utils.escape_html(c.name)}</td>` +
				`<td>${frappe.utils.escape_html(c.detail || "")}</td></tr>`
		)
		.join("");
	frappe.msgprint({
		title: result.success ? __("All checks passed") : __("Some checks failed"),
		indicator: result.success ? "green" : "red",
		message: `<table class="table table-bordered">${rows}</table>`,
		wide: true,
	});
}
