import frappe
from frappe import _

from aitsgst.services.context import load_config


def on_submit(doc, method=None):
	"""Optionally queue a push (as a DRAFT on cloud). E-invoice / e-way bill are never automatic."""
	cfg = load_config()
	if (cfg.enabled and cfg.gate_open and cfg.company(doc.company)
			and frappe.db.get_single_value("AITS GST Settings", "auto_push_on_submit")):
		from aitsgst.api import enqueue_push

		enqueue_push(doc.name)


def before_cancel(doc, method=None):
	"""A plain local cancel must not leave the cloud invoice, IRN or e-way bill active, so it is
	refused while they are, pointing to Cloud GST > Cancel Everywhere (which does all of it, in order)."""
	if doc.flags.get("aitsgst_cancel_everywhere"):
		return
	block_cancel_with_live_documents(doc)


def block_cancel_with_live_documents(doc):
	live = []
	if doc.get("aitsgst_einvoice_status") == "Generated":
		live.append(_("e-Invoice (IRN {0})").format(doc.aitsgst_irn))
	if doc.get("aitsgst_ewb_status") == "Generated":
		live.append(_("e-Way Bill {0}").format(doc.aitsgst_ewaybill))
	if doc.get("aitsgst_cloud_invoice") and doc.get("aitsgst_cloud_docstatus") in ("Draft", "Submitted"):
		live.append(_("cloud invoice {0} ({1})").format(doc.aitsgst_cloud_invoice, doc.aitsgst_cloud_docstatus))
	if live:
		frappe.throw(
			_("This invoice still has an active {0}. Use <b>Cloud GST &gt; Cancel Everywhere</b> to cancel it on the GST portal, "
			  "on cloud and here in one step (or cancel it on the cloud site first).").format(_(" and ").join(live)),
			title=_("Cloud GST"),
		)
