import frappe

from aitsgst.utils.qr import save_qr_image


def execute():
	"""Create the QR code image for invoices that received an IRN before the image field existed."""
	if not frappe.db.has_column("Sales Invoice", "aitsgst_qr_image"):
		return
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"aitsgst_signed_qr_code": ["is", "set"], "aitsgst_qr_image": ["is", "not set"]},
		fields=["name", "aitsgst_signed_qr_code"],
	)
	for row in rows:
		url = save_qr_image(row.name, row.aitsgst_signed_qr_code)
		frappe.db.set_value("Sales Invoice", row.name, "aitsgst_qr_image", url, update_modified=False)
