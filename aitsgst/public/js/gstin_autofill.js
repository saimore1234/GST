// AITS GST: enter a GSTIN on a Customer, Supplier or Address, tab out, and the legal name,
// GST category and registered address are filled in (looked up through the GST service).
// Fields the user already filled are never overwritten; nothing is saved automatically.

frappe.provide("aitsgst.gstin");

aitsgst.gstin.ADDRESS_FIELDS = ["address_line1", "address_line2", "city", "state", "pincode", "country"];

aitsgst.gstin.lookup = async function (frm) {
	const gstin = (frm.doc.gstin || "").trim().toUpperCase();
	if (gstin.length !== 15 || frm.__aitsgst_gstin === gstin) return null;
	frm.__aitsgst_gstin = gstin;

	frappe.show_alert({ message: __("Fetching details for GSTIN {0}...", [gstin]), indicator: "blue" }, 3);
	let r;
	try {
		r = await frappe.call({ method: "aitsgst.api.get_gstin_details", args: { gstin } });
	} catch (e) {
		frm.__aitsgst_gstin = null; // allow a retry after an error
		return null;
	}
	const info = r && r.message;
	if (!info || info.disabled) return null;
	aitsgst.gstin.show_status(frm, info);
	return info;
};

aitsgst.gstin.show_status = function (frm, info) {
	const field = frm.get_field("gstin");
	const status = info.status || "";
	if (field && status) {
		const active = status.toLowerCase() === "active";
		field.set_description(
			`<span class="indicator-pill ${active ? "green" : "red"}">${__("GSTIN status")}: ${frappe.utils.escape_html(status)}</span>`
		);
	}
	if (status && status.toLowerCase() !== "active") {
		frappe.msgprint({
			title: __("GSTIN is not active"),
			indicator: "red",
			message: __("GSTIN {0} has status <b>{1}</b> on the GST portal. Check before invoicing this party.", [
				frappe.utils.escape_html(info.gstin || ""),
				frappe.utils.escape_html(status),
			]),
		});
	}
};

// Sets a field only when it is empty; returns true when the existing value differs (kept).
aitsgst.gstin.fill = function (frm, fieldname, value) {
	if (!value || !frm.get_field(fieldname)) return false;
	const current = frm.doc[fieldname];
	if (current === undefined || current === null || current === "") {
		frm.set_value(fieldname, value);
		return false;
	}
	return String(current).trim().toLowerCase() !== String(value).trim().toLowerCase();
};

aitsgst.gstin.summary = function (filled, kept) {
	let message = __("Details filled from GSTIN.");
	if (kept.length) message += " " + __("Kept your values for: {0}.", [kept.join(", ")]);
	frappe.show_alert({ message, indicator: "green" }, 6);
};

aitsgst.gstin.party = async function (frm, name_field) {
	const info = await aitsgst.gstin.lookup(frm);
	if (!info) return;
	const kept = [];

	if (aitsgst.gstin.fill(frm, name_field, info.business_name)) kept.push(__("Name"));
	if (info.gst_category) frm.set_value("gst_category", info.gst_category);

	// New party: India Compliance creates its primary address on save from these (non-field) values.
	const addr = info.permanent_address;
	if (frm.is_new() && addr) {
		Object.assign(frm.doc, {
			_address_line1: addr.address_line1,
			address_line2: addr.address_line2,
			city: addr.city,
			state: addr.state,
			pincode: addr.pincode,
			country: addr.country || "India",
			is_primary_address: 1,
			is_shipping_address: 1,
		});
		frappe.show_alert({ message: __("The registered address will be created when you save."), indicator: "blue" }, 6);
	}
	aitsgst.gstin.summary(true, kept);
};

aitsgst.gstin.address = async function (frm) {
	const info = await aitsgst.gstin.lookup(frm);
	if (!info) return;
	const addresses = info.all_addresses || (info.permanent_address ? [info.permanent_address] : []);

	const apply = (addr) => {
		const kept = [];
		if (info.gst_category) frm.set_value("gst_category", info.gst_category);
		if (aitsgst.gstin.fill(frm, "address_title", info.business_name)) kept.push(__("Address Title"));
		for (const f of aitsgst.gstin.ADDRESS_FIELDS) {
			if (addr && aitsgst.gstin.fill(frm, f, f === "country" ? addr[f] || "India" : addr[f])) kept.push(__(frappe.meta.get_label("Address", f)));
		}
		aitsgst.gstin.summary(true, kept);
	};

	if (addresses.length <= 1) return apply(addresses[0]);

	// The GSTIN has additional places of business: let the user pick which one this address is.
	const label = (a) => [a.address_line1, a.city, a.pincode].filter(Boolean).join(", ");
	const options = addresses.map((a, i) => ({ value: String(i), label: (i === 0 ? __("Principal") + ": " : "") + label(a) }));
	frappe.prompt(
		[{ fieldtype: "Select", fieldname: "choice", label: __("Place of business"), options, default: "0", reqd: 1 }],
		(values) => apply(addresses[parseInt(values.choice, 10)]),
		__("Which address of this GSTIN?"),
		__("Use this address")
	);
};

// This file is loaded once per doctype (Customer, Supplier, Address): register the handlers only once.
if (!aitsgst.gstin.registered) {
	aitsgst.gstin.registered = true;
	frappe.ui.form.on("Customer", { gstin: (frm) => aitsgst.gstin.party(frm, "customer_name") });
	frappe.ui.form.on("Supplier", { gstin: (frm) => aitsgst.gstin.party(frm, "supplier_name") });
	frappe.ui.form.on("Address", { gstin: (frm) => aitsgst.gstin.address(frm) });
}
