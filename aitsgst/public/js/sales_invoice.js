// Cloud GST (AITS GST): push to cloud ERPNext, e-invoice / e-way bill generated THERE,
// results shown here. Every irreversible action needs an explicit tick in a dialog.

const AITSGST_GROUP = __("Cloud GST");
const AITSGST_COLORS = { Pushed: "green", Generated: "green", Queued: "blue", Failed: "red", Blocked: "orange", Cancelled: "red", Mismatch: "red", Match: "green" };

frappe.ui.form.on("Sales Invoice", {
	setup(frm) {
		frappe.realtime.on("aitsgst_push_done", (data) => {
			if (!cur_frm || cur_frm.doctype !== "Sales Invoice" || cur_frm.doc.name !== data.name) return;
			const ok = ["Created", "Adopted", "AlreadyPushed"].includes(data.outcome);
			frappe.show_alert({
				message: ok ? __("Pushed to cloud: {0}", [data.cloud_invoice]) : __("Cloud push {0}", [data.outcome]),
				indicator: ok ? "green" : "red",
			});
			cur_frm.reload_doc();
		});
	},

	refresh(frm) {
		aitsgst.render_status(frm);
		aitsgst.render_qr(frm);
		aitsgst.cloud_cancelled_banner(frm);
		if (frm.doc.docstatus !== 1 || !aitsgst.can_use()) return;
		aitsgst.add_buttons(frm);
	},
});

const aitsgst = {
	can_use() {
		return frappe.user.has_role("AITS GST Manager") || frappe.user.has_role("System Manager");
	},

	add_buttons(frm) {
		const d = frm.doc;
		const pushed = d.aitsgst_push_status === "Pushed";
		const einv = d.aitsgst_einvoice_status;
		const ewb = d.aitsgst_ewb_status;
		const add = (label, fn) => frm.add_custom_button(label, fn, AITSGST_GROUP);

		if (!pushed) add(d.aitsgst_push_status === "Failed" || d.aitsgst_push_status === "Blocked" ? __("Retry Push to Cloud") : __("Push to Cloud"), () => aitsgst.push(frm));
		if (d.aitsgst_cloud_invoice) add(__("Refresh from Cloud"), () => aitsgst.call(frm, "refresh_invoice", {}, __("Reading the cloud invoice...")));
		if (pushed && einv !== "Generated" && einv !== "Cancelled") add(__("Generate e-Invoice"), () => aitsgst.generate_e_invoice(frm));
		if (pushed && ewb !== "Generated") add(__("Generate e-Way Bill"), () => aitsgst.generate_e_waybill(frm));
		if (ewb === "Generated") add(__("Update Vehicle (Part-B)"), () => aitsgst.update_vehicle(frm));
		if (einv === "Generated") add(__("Cancel e-Invoice"), () => aitsgst.cancel(frm, "cancel_e_invoice", __("Cancel e-Invoice (IRN)"),
			__("The IRN is cancelled on the GST portal. This cannot be undone and this invoice number can never get a new IRN. Any e-way bill is cancelled with it.")));
		if (ewb === "Generated") add(__("Cancel e-Way Bill"), () => aitsgst.cancel(frm, "cancel_e_waybill", __("Cancel e-Way Bill"),
			__("The e-way bill is cancelled on the GST portal. This cannot be undone.")));
		if (d.aitsgst_cloud_invoice && ["Draft", "Submitted"].includes(d.aitsgst_cloud_docstatus))
			add(__("Cancel Everywhere"), () => aitsgst.cancel_everywhere(frm));
	},

	cancel_everywhere(frm) {
		const d = frm.doc;
		const portal = d.aitsgst_einvoice_status === "Generated" || d.aitsgst_ewb_status === "Generated";
		const steps = [];
		if (d.aitsgst_einvoice_status === "Generated") steps.push(__("Cancel IRN {0} on the GST portal", [d.aitsgst_irn]) + (d.aitsgst_ewb_status === "Generated" ? __(" (with e-way bill {0})", [d.aitsgst_ewaybill]) : ""));
		else if (d.aitsgst_ewb_status === "Generated") steps.push(__("Cancel e-way bill {0} on the GST portal", [d.aitsgst_ewaybill]));
		steps.push(d.aitsgst_cloud_docstatus === "Draft" ? __("Delete cloud draft {0}", [d.aitsgst_cloud_invoice]) : __("Cancel cloud invoice {0}", [d.aitsgst_cloud_invoice]));
		steps.push(__("Cancel this invoice {0} here", [d.name]));

		aitsgst.confirm_dialog({
			title: __("Cancel Everywhere"),
			warning: __("Everything is checked first; if any step cannot be done, nothing is cancelled. This cannot be undone. Steps:") +
				"\n" + steps.map((s, i) => `${i + 1}. ${s}`).join("\n"),
			fields: portal ? [
				{ fieldtype: "Select", fieldname: "reason", label: __("Reason"), options: "\nDuplicate\nData Entry Mistake\nOrder Cancelled\nOthers", reqd: 1 },
				{ fieldtype: "Data", fieldname: "remark", label: __("Remark (max 100 characters; required for Others)"), length: 100 },
			] : [],
			primary: __("Cancel Everywhere"),
			onsubmit: (values) => aitsgst.call(frm, "cancel_everywhere", { reason: values.reason, remark: values.remark, confirm: 1 }, __("Cancelling on the GST portal, on cloud and here...")),
		});
	},

	call(frm, method, args, freeze_message) {
		return frappe.call({
			method: `aitsgst.api.${method}`,
			args: { name: frm.doc.name, ...args },
			freeze: true,
			freeze_message,
		}).then((r) => {
			frm.reload_doc();
			return r.message;
		});
	},

	push(frm) {
		frappe.call({ method: "aitsgst.api.push_invoice", args: { name: frm.doc.name } }).then((r) => {
			if (r.message && r.message.workers === 0) {
				frappe.msgprint({
					title: __("Queued, but no background worker is running"),
					indicator: "orange",
					message: __("The push will start as soon as a worker runs. Start the bench with <code>bench start</code> (or <code>bench worker</code>), or ask your administrator to check the workers."),
				});
			} else {
				frappe.show_alert({ message: __("Queued: pushing to cloud as a draft..."), indicator: "blue" });
			}
			frm.reload_doc();
		});
	},

	confirm_dialog({ title, fields = [], warning, primary, onsubmit }) {
		const dialog = new frappe.ui.Dialog({
			title,
			fields: [
				{ fieldtype: "HTML", options: `<div class="alert alert-warning" style="white-space: pre-line">${frappe.utils.escape_html(warning)}</div>` },
				...fields,
				{ fieldtype: "Check", fieldname: "i_understand", label: __("I understand. Send this to the GST portal through the cloud site."), reqd: 1 },
			],
			primary_action_label: primary,
			primary_action(values) {
				if (!values.i_understand) {
					frappe.msgprint(__("Tick the confirmation to continue."));
					return;
				}
				dialog.hide();
				delete values.i_understand;
				onsubmit(values);
			},
		});
		dialog.show();
		return dialog;
	},

	generate_e_invoice(frm) {
		aitsgst.confirm_dialog({
			title: __("Generate e-Invoice on cloud"),
			warning: __("This submits the cloud invoice {0} (if it is a draft) and registers it with the GST portal. An IRN can only be cancelled within 24 hours.", [frm.doc.aitsgst_cloud_invoice]),
			primary: __("Generate e-Invoice"),
			onsubmit: () => aitsgst.call(frm, "generate_e_invoice", { confirm: 1 }, __("Generating e-Invoice on cloud...")).then((r) => {
				if (r && r.irn) frappe.show_alert({ message: __("IRN {0}", [r.irn]), indicator: "green" });
			}),
		});
	},

	transport_fields(defaults, extra = []) {
		return [
			{ fieldtype: "Select", fieldname: "mode_of_transport", label: __("Mode of Transport"), options: "Road\nRail\nAir\nShip", default: defaults.mode_of_transport || "Road", reqd: 1 },
			{ fieldtype: "Data", fieldname: "vehicle_no", label: __("Vehicle No"), default: defaults.vehicle_no, depends_on: "eval:doc.mode_of_transport=='Road'" },
			{ fieldtype: "Select", fieldname: "gst_vehicle_type", label: __("Vehicle Type"), options: "Regular\nOver Dimensional Cargo (ODC)", default: defaults.gst_vehicle_type || "Regular", depends_on: "eval:doc.mode_of_transport=='Road'" },
			{ fieldtype: "Column Break" },
			{ fieldtype: "Data", fieldname: "lr_no", label: __("LR / RR / AWB / BL No"), default: defaults.lr_no },
			{ fieldtype: "Date", fieldname: "lr_date", label: __("Transport Doc Date"), default: defaults.lr_date },
			...extra,
		];
	},

	generate_e_waybill(frm) {
		frappe.call({ method: "aitsgst.api.get_transport_defaults", args: { name: frm.doc.name } }).then((r) => {
			const defaults = r.message || {};
			aitsgst.confirm_dialog({
				title: __("Generate e-Way Bill on cloud"),
				warning: __("This registers an e-way bill with the GST portal (cancellable only within 24 hours) and submits the cloud invoice if it is still a draft."),
				fields: aitsgst.transport_fields(defaults, [
					{ fieldtype: "Data", fieldname: "gst_transporter_id", label: __("Transporter GSTIN / ID"), default: defaults.gst_transporter_id },
					{ fieldtype: "Data", fieldname: "transporter_name", label: __("Transporter Name"), default: defaults.transporter_name },
					{ fieldtype: "Int", fieldname: "distance", label: __("Distance (km)"), default: defaults.distance || 0, description: __("0 = let the GST portal compute it from pincodes") },
				]),
				primary: __("Generate e-Way Bill"),
				onsubmit: (values) => aitsgst.call(frm, "generate_e_waybill", { values, confirm: 1 }, __("Generating e-Way Bill on cloud...")),
			});
		});
	},

	update_vehicle(frm) {
		const d = frm.doc;
		aitsgst.confirm_dialog({
			title: __("Update Vehicle (Part-B)"),
			warning: __("This updates Part-B of e-way bill {0} on the GST portal.", [d.aitsgst_ewaybill]),
			fields: aitsgst.transport_fields({ mode_of_transport: d.aitsgst_mode_of_transport, vehicle_no: d.aitsgst_vehicle_no }, [
				{ fieldtype: "Section Break" },
				{ fieldtype: "Select", fieldname: "reason", label: __("Reason"), options: "\nDue to Break Down\nDue to Trans Shipment\nFirst Time\nOthers", reqd: 1 },
				{ fieldtype: "Data", fieldname: "remark", label: __("Remark") },
				{ fieldtype: "Column Break" },
				{ fieldtype: "Data", fieldname: "place_of_change", label: __("Place of Change (city)"), reqd: 1 },
				{ fieldtype: "Data", fieldname: "state", label: __("State of Change"), reqd: 1, description: __("e.g. Maharashtra") },
			]),
			primary: __("Update Vehicle"),
			onsubmit: (values) => aitsgst.call(frm, "update_vehicle", { values, confirm: 1 }, __("Updating vehicle on cloud...")),
		});
	},

	cancel(frm, method, title, warning) {
		aitsgst.confirm_dialog({
			title,
			warning,
			fields: [
				{ fieldtype: "Select", fieldname: "reason", label: __("Reason"), options: "\nDuplicate\nData Entry Mistake\nOrder Cancelled\nOthers", reqd: 1 },
				{ fieldtype: "Data", fieldname: "remark", label: __("Remark (max 100 characters; required for Others)"), length: 100 },
			],
			primary: title,
			onsubmit: (values) => aitsgst.call(frm, method, { reason: values.reason, remark: values.remark, confirm: 1 }, __("Cancelling on cloud...")),
		});
	},

	render_status(frm) {
		const d = frm.doc;
		const badge = (label, value) =>
			value ? `<span class="indicator-pill ${AITSGST_COLORS[value] || "gray"}" style="margin-right:6px">${frappe.utils.escape_html(label)}: ${frappe.utils.escape_html(value)}</span>` : "";

		if (d.aitsgst_push_status) frm.dashboard.add_indicator(__("Cloud: {0}", [d.aitsgst_push_status]), AITSGST_COLORS[d.aitsgst_push_status] || "gray");
		if (d.aitsgst_einvoice_status) frm.dashboard.add_indicator(__("e-Invoice: {0}", [d.aitsgst_einvoice_status]), AITSGST_COLORS[d.aitsgst_einvoice_status] || "gray");
		if (d.aitsgst_ewb_status) frm.dashboard.add_indicator(__("e-Way Bill: {0}", [d.aitsgst_ewb_status]), AITSGST_COLORS[d.aitsgst_ewb_status] || "gray");

		const field = frm.get_field("aitsgst_status_html");
		if (!field) return;
		const error = d.aitsgst_last_error && d.aitsgst_push_status !== "Pushed"
			? `<div class="text-danger" style="margin-top:8px; white-space:pre-wrap">${frappe.utils.escape_html(d.aitsgst_last_error)}</div>` : "";
		field.$wrapper.html(
			`<div>${badge(__("Cloud"), d.aitsgst_push_status)}${badge(__("Totals"), d.aitsgst_recon_status)}` +
			`${badge(__("e-Invoice"), d.aitsgst_einvoice_status)}${badge(__("e-Way Bill"), d.aitsgst_ewb_status)}</div>${error}`
		);
	},

	cloud_cancelled_banner(frm) {
		const d = frm.doc;
		if (d.docstatus !== 1 || !["Cancelled", "Deleted"].includes(d.aitsgst_cloud_docstatus)) return;
		frm.dashboard.set_headline_alert(
			`<div class="row"><div class="col-sm-9">${__("This invoice was {0} on the cloud site ({1}) but is still submitted here.",
				[d.aitsgst_cloud_docstatus === "Deleted" ? __("deleted") : __("cancelled"), frappe.utils.escape_html(d.aitsgst_cloud_invoice || "")])}</div>` +
			`<div class="col-sm-3 text-right"><button class="btn btn-xs btn-danger aitsgst-cancel-here">${__("Cancel here too")}</button></div></div>`,
			"red"
		);
		frm.dashboard.headline_alert.find(".aitsgst-cancel-here").on("click", () => frm.savecancel());
	},

	render_qr(frm) {
		const field = frm.get_field("aitsgst_qr_html");
		if (!field) return;
		field.$wrapper.empty();
		if (!frm.doc.aitsgst_signed_qr_code) return;
		frappe.call({ method: "aitsgst.utils.qr.get_invoice_qr", args: { name: frm.doc.name } }).then((r) => {
			if (!r.message) return;
			const cancelled = frm.doc.aitsgst_einvoice_status === "Cancelled";
			field.$wrapper.html(
				`<img src="${r.message}" alt="e-Invoice QR" style="width:180px; ${cancelled ? "opacity:0.3" : ""}">` +
				(cancelled ? `<div class="text-danger">${__("IRN cancelled")}</div>` : "")
			);
		});
	},
};
