// AITS GST: let India Compliance's quick-entry popups (+ Add Customer / Supplier / Address) autofill from
// a GSTIN when the lookup is available through the GST service, even though this site has no India
// Compliance API account. Only the popups' GSTIN autofill is enabled; India Compliance's global
// "API enabled" flag (which also drives its e-invoice buttons) is left untouched.

(function () {
	const POPUPS = ["CustomerQuickEntryForm", "SupplierQuickEntryForm", "AddressQuickEntryForm"];

	function wrap() {
		if (!frappe.boot || !frappe.boot.aitsgst_gstin_autofill) return;
		for (const name of POPUPS) {
			const Base = frappe.ui.form[name];
			if (!Base || Base.__aitsgst_wrapped) continue;
			const Wrapped = class extends Base {
				constructor(...args) {
					super(...args);
					this.api_enabled = true; // read later, when the dialog renders and when the GSTIN changes
				}
			};
			Wrapped.__aitsgst_wrapped = true;
			frappe.ui.form[name] = Wrapped;
		}
	}

	// India Compliance defines the popups in its own bundle; wrap once everything has loaded.
	if (window.frappe && frappe.boot && frappe.ui && frappe.ui.form) wrap();
	$(document).on("app_ready", wrap);
})();
