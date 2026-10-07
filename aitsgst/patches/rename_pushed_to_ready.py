import frappe


def execute():
	"""The sync status value 'Pushed' became 'Ready' (neutral wording for end users)."""
	if frappe.db.has_column("Sales Invoice", "aitsgst_push_status"):
		frappe.db.sql("update `tabSales Invoice` set aitsgst_push_status = 'Ready' where aitsgst_push_status = 'Pushed'")
