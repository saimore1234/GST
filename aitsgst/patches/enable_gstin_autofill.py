import frappe


def execute():
	"""New 'Fill party details from GSTIN' setting: on by default, also for sites already configured
	(a new field's default is not applied to an existing Single)."""
	exists = frappe.db.sql(
		"select 1 from `tabSingles` where doctype = %s and field = %s", ("AITS GST Settings", "gstin_autofill")
	)
	if not exists:
		frappe.db.set_single_value("AITS GST Settings", "gstin_autofill", 1)
