import frappe
import pyqrcode


def get_qr_data_uri(text: str | None, scale: int = 3) -> str:
	"""PNG data URI for a QR code, rendered from the signed QR string returned by the GST portal.
	Available in print formats as {{ get_qr_data_uri(doc.aitsgst_signed_qr_code) }}."""
	if not text:
		return ""
	png = pyqrcode.create(text, error="M").png_as_base64_str(scale=scale, quiet_zone=1)
	return f"data:image/png;base64,{png}"


@frappe.whitelist()
def get_invoice_qr(name: str) -> str:
	frappe.has_permission("Sales Invoice", "read", name, throw=True)
	status, qr = frappe.db.get_value("Sales Invoice", name, ["aitsgst_einvoice_status", "aitsgst_signed_qr_code"]) or (None, None)
	return get_qr_data_uri(qr, scale=4) if status in ("Generated", "Cancelled") else ""
