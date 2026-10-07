"""Single source of truth for the Sales Invoice custom fields.

fixtures/custom_field.json is generated from this list
(`python -m aitsgst.setup.custom_fields` from the bench's env) and is what
`bench migrate` applies. Every field is prefixed `aitsgst_` so nothing collides
with India Compliance's own irn / ewaybill / vehicle fields on the local site:
the local India Compliance never sees these values and never reacts to them.
"""

import json
import os

MODULE = "AITS GST"
DT = "Sales Invoice"

_STATUS = "\nNot Generated\nGenerated\nFailed\nCancelled"


def _f(fieldname, label, fieldtype, insert_after, **extra):
	field = {
		"fieldname": fieldname,
		"label": label,
		"fieldtype": fieldtype,
		"insert_after": insert_after,
	}
	if fieldtype not in ("Tab Break", "Section Break", "Column Break", "HTML"):
		# Written only by the app (via db_set), never typed by users, never carried to an amendment/copy.
		field.update({"read_only": 1, "no_copy": 1, "allow_on_submit": 1, "print_hide": 1})
	field.update(extra)
	return field


FIELDS = [
	# User-visible labels never mention a second site: it is "the GST service". Internal fields
	# (the other site's document name, key, status, totals check, raw QR text) are hidden.
	_f("aitsgst_tab", "e-Invoice", "Tab Break", "remarks"),
	_f("aitsgst_status_html", "e-Invoice Status Summary", "HTML", "aitsgst_tab"),
	# ---- sync with the GST service
	_f("aitsgst_push_section", "e-Invoice Sync", "Section Break", "aitsgst_status_html"),
	_f("aitsgst_push_status", "e-Invoice Sync Status", "Select", "aitsgst_push_section",
	   options="\nQueued\nReady\nFailed\nBlocked", in_standard_filter=1),
	_f("aitsgst_cloud_invoice", "GST Service Reference", "Data", "aitsgst_push_status", search_index=1, hidden=1),
	_f("aitsgst_cloud_key", "GST Service Key", "Data", "aitsgst_cloud_invoice", search_index=1, hidden=1),
	_f("aitsgst_cloud_docstatus", "GST Service Record Status", "Data", "aitsgst_cloud_key", hidden=1),
	_f("aitsgst_push_cb", None, "Column Break", "aitsgst_cloud_docstatus"),
	_f("aitsgst_cloud_grand_total", "GST Service Grand Total", "Currency", "aitsgst_push_cb", options="currency", hidden=1),
	_f("aitsgst_recon_status", "Totals Check", "Select", "aitsgst_cloud_grand_total", options="\nMatch\nMismatch", hidden=1),
	_f("aitsgst_recon_detail", "Totals Check Detail", "Small Text", "aitsgst_recon_status", hidden=1),
	_f("aitsgst_last_error", "Last Error", "Small Text", "aitsgst_recon_detail"),
	_f("aitsgst_last_synced", "Last Synced", "Datetime", "aitsgst_last_error"),
	# ---- e-invoice
	_f("aitsgst_einv_section", "e-Invoice", "Section Break", "aitsgst_last_synced"),
	_f("aitsgst_einvoice_status", "e-Invoice Status", "Select", "aitsgst_einv_section",
	   options=_STATUS, in_standard_filter=1),
	_f("aitsgst_irn", "IRN No", "Data", "aitsgst_einvoice_status", print_hide=0, search_index=1),
	_f("aitsgst_ack_no", "Ack No", "Data", "aitsgst_irn", print_hide=0),
	_f("aitsgst_ack_date", "Ack Date", "Datetime", "aitsgst_ack_no", print_hide=0),
	_f("aitsgst_signed_qr_code", "Signed QR Code", "Long Text", "aitsgst_ack_date", hidden=1),
	_f("aitsgst_einv_cb", None, "Column Break", "aitsgst_signed_qr_code"),
	# PNG rendered on this server from the signed QR; usable in any print format as <img src="{{ doc.aitsgst_qr_image }}">
	_f("aitsgst_qr_image", "e-Invoice QR Code", "Attach Image", "aitsgst_einv_cb", print_hide=0),
	_f("aitsgst_qr_html", "e-Invoice QR Preview", "HTML", "aitsgst_qr_image"),
	_f("aitsgst_einvoice_cancel_reason", "e-Invoice Cancel Reason", "Data", "aitsgst_qr_html"),
	_f("aitsgst_einvoice_cancelled_on", "e-Invoice Cancelled On", "Datetime", "aitsgst_einvoice_cancel_reason"),
	_f("aitsgst_einvoice_cancelled_by", "e-Invoice Cancelled By", "Data", "aitsgst_einvoice_cancelled_on"),
	# ---- e-way bill
	_f("aitsgst_ewb_section", "e-Way Bill", "Section Break", "aitsgst_einvoice_cancelled_by"),
	_f("aitsgst_ewb_status", "e-Way Bill Status", "Select", "aitsgst_ewb_section",
	   options=_STATUS, in_standard_filter=1),
	_f("aitsgst_ewaybill", "e-Way Bill No", "Data", "aitsgst_ewb_status", print_hide=0, search_index=1),
	_f("aitsgst_ewb_date", "e-Way Bill Date", "Datetime", "aitsgst_ewaybill", print_hide=0),
	_f("aitsgst_ewb_valid_upto", "e-Way Bill Valid Upto", "Datetime", "aitsgst_ewb_date", print_hide=0),
	_f("aitsgst_ewb_cancel_reason", "e-Way Bill Cancel Reason", "Data", "aitsgst_ewb_valid_upto"),
	_f("aitsgst_ewb_cancelled_on", "e-Way Bill Cancelled On", "Datetime", "aitsgst_ewb_cancel_reason"),
	_f("aitsgst_ewb_cancelled_by", "e-Way Bill Cancelled By", "Data", "aitsgst_ewb_cancelled_on"),
	_f("aitsgst_ewb_cb", None, "Column Break", "aitsgst_ewb_cancelled_by"),
	_f("aitsgst_mode_of_transport", "Mode of Transport", "Data", "aitsgst_ewb_cb"),
	_f("aitsgst_vehicle_no", "Vehicle No", "Data", "aitsgst_mode_of_transport", print_hide=0),
	_f("aitsgst_transporter_id", "Transporter ID", "Data", "aitsgst_vehicle_no"),
	_f("aitsgst_transporter_name", "Transporter Name", "Data", "aitsgst_transporter_id"),
	_f("aitsgst_lr_no", "LR / Doc No", "Data", "aitsgst_transporter_name"),
	_f("aitsgst_lr_date", "LR / Doc Date", "Date", "aitsgst_lr_no"),
	_f("aitsgst_distance", "Distance (km)", "Int", "aitsgst_lr_date"),
]


def as_fixture_records():
	records = []
	for field in FIELDS:
		record = {
			"doctype": "Custom Field",
			"name": f"{DT}-{field['fieldname']}",
			"dt": DT,
			"module": MODULE,
			"label": field["label"],
		}
		record.update({k: v for k, v in field.items() if k != "label"})
		records.append(record)
	return records


if __name__ == "__main__":
	path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "custom_field.json")
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as fh:
		json.dump(as_fixture_records(), fh, indent=1, sort_keys=True)
		fh.write("\n")
	print(f"wrote {len(FIELDS)} fields to {path}")
