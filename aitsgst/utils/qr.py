import base64
import re

import frappe
import pyqrcode


def qr_png(text: str, scale: int = 4) -> bytes:
	return base64.b64decode(pyqrcode.create(text, error="M").png_as_base64_str(scale=scale, quiet_zone=1))


def save_qr_image(invoice: str, text: str) -> str:
	"""Saves the QR as a PRIVATE file attached to the invoice (field aitsgst_qr_image); returns its URL.
	Private: it encodes GSTINs, invoice number and value. Frappe inlines private images when making PDFs."""
	file = frappe.get_doc({
		"doctype": "File",
		"file_name": f"e-invoice-qr-{re.sub(r'[^A-Za-z0-9]+', '-', invoice).strip('-')}.png",
		"attached_to_doctype": "Sales Invoice",
		"attached_to_name": invoice,
		"attached_to_field": "aitsgst_qr_image",
		"is_private": 1,
		"content": qr_png(text),
	})
	file.save(ignore_permissions=True)
	return file.file_url


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
