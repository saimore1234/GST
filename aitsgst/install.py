import frappe

ROLE = "AITS GST Manager"


def before_install():
	# DocType permissions reference this role, and fixtures are synced only after DocTypes.
	if not frappe.db.exists("Role", ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)


def after_install():
	settings = frappe.get_single("AITS GST Settings")
	if not settings.local_site_code:
		# Stable, readable default for keys of non-SAP invoices; can be changed until the first push.
		settings.local_site_code = (frappe.local.site or "LOCAL").split(".")[0][:20].upper().replace(".", "-")
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)
