"""E-invoice (IRN) and e-way bill through the CLOUD site's India Compliance; results are
written back to the local Sales Invoice.

Ported from the SAP B1 Web Portal's ErpNextComplianceService. Two facts shape every step:
  * It is NOT reversible. An IRN / e-way bill can only be cancelled within 24 hours, and
    generation needs a SUBMITTED cloud invoice (submitting posts GL entries on cloud). So every
    check that can be made up front IS made up front, and nothing is submitted or sent unless
    all pass AND the user confirmed.
  * A timeout is ambiguous - the GST portal may have registered the document before the
    connection dropped. After any failure the cloud invoice is re-read; if the IRN / e-way
    bill is there it is recorded instead of reported as a failure.

Extra guards added for the local -> cloud setup:
  * the cloud GST Settings must be in sandbox mode unless 'Allow production' is checked;
  * an e-invoice is refused while the local and cloud totals do not reconcile.
"""

import re
import time
from datetime import datetime, timedelta

from aitsgst.core.cloud_client import CloudError
from aitsgst.core.gst import STATES
from aitsgst.services.push import docstatus_text

SI = "Sales Invoice"
EINV = "india_compliance.gst_india.utils.e_invoice"
EWB = "india_compliance.gst_india.utils.e_waybill"

CANCEL_REASONS = ("Duplicate", "Data Entry Mistake", "Order Cancelled", "Others")
VEHICLE_UPDATE_REASONS = ("Due to Break Down", "Due to Trans Shipment", "First Time", "Others")
MODES = ("Road", "Rail", "Air", "Ship")
VEHICLE_TYPES = ("Regular", "Over Dimensional Cargo (ODC)")
_VEHICLE_NO = re.compile(r"^[A-Z0-9]{6,15}$")
_TRANSPORTER_ID = re.compile(r"^[0-9A-Z]{15}$")
CANCEL_WINDOW = timedelta(hours=24)
# Ledger rows ERPNext itself ignores when cancelling a Sales Invoice.
IGNORED_LINKED_DOCTYPES = {
	"GL Entry", "Stock Ledger Entry", "Repost Item Valuation", "Repost Payment Ledger", "Repost Payment Ledger Items",
	"Repost Accounting Ledger", "Repost Accounting Ledger Items", "Unreconcile Payment", "Unreconcile Payment Entries",
	"Payment Ledger Entry", "Serial and Batch Bundle",
}


class Blocked(Exception):
	"""Nothing was sent to the GST portal. `problems` are safe to show."""

	def __init__(self, headline: str, problems):
		super().__init__(headline)
		self.headline = headline
		self.problems = [problems] if isinstance(problems, str) else list(problems)


class ConfirmationRequired(Blocked):
	pass


def _parse_dt(value) -> datetime | None:
	if not value:
		return None
	if isinstance(value, datetime):
		return value
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
		try:
			return datetime.strptime(str(value), fmt)
		except ValueError:
			continue
	return None


def _is_cancelled_einvoice(doc) -> bool:
	return doc.get("einvoice_status") in ("Cancelled", "Manually Cancelled")


def _is_active_ewb(doc) -> bool:
	return bool(doc.get("ewaybill")) and doc.get("e_waybill_status") != "Cancelled"


def _norm_vehicle(v) -> str | None:
	v = re.sub(r"[\s\-]", "", str(v or "")).upper()
	return v or None


class ComplianceService:
	# India Compliance writes the e-Waybill Log in a background job ("short" queue), so right after
	# generating, the log (date / valid upto) may not exist yet: wait for it briefly.
	EWB_LOG_WAITS = (1, 2, 3)

	def __init__(self, ctx, sleep=time.sleep):
		self.cfg, self.client, self.store, self.log = ctx.cfg, ctx.client, ctx.store, ctx.log
		self._sleep = sleep

	def _wait_for_ewaybill_log(self, ewb):
		for delay in self.EWB_LOG_WAITS:
			if self._ewaybill_log(ewb).get("valid_upto"):
				return
			self._sleep(delay)

	# ============================================================ e-invoice
	def generate_e_invoice(self, name: str, confirm: bool = False) -> dict:
		si, cloud_name = self._require_pushed(name)
		doc = self._read(cloud_name)

		if doc.get("irn") and not _is_cancelled_einvoice(doc):
			self.sync_from_doc(si, doc)
			self.log.write("E-Invoice", "Success", name, f"Already generated on cloud for {cloud_name}.", cloud_invoice=cloud_name)
			return {"outcome": "AlreadyGenerated", "irn": doc.get("irn")}

		settings = self._gst_settings()
		problems = self._environment_problems(settings) + self._einvoice_problems(si, doc, settings)
		if problems:
			self._log_blocked("E-Invoice", name, cloud_name, problems)
			raise Blocked("The e-invoice was not generated. Nothing was submitted or sent to the GST portal.", problems)
		if not confirm:
			raise ConfirmationRequired("Confirmation is required to generate an e-invoice.",
			                           "Generating an e-invoice registers this invoice with the GST portal and cannot be undone after 24 hours.")

		with self.store.lock(name, "e-invoice"):
			try:
				self._submit_if_draft(cloud_name, doc)
				self.client.call(f"{EINV}.generate_e_invoice", docname=cloud_name)
				return self._record_einvoice(si, cloud_name, "Generated")
			except CloudError as e:
				after = self._try_read(cloud_name)
				if after and after.get("irn") and not _is_cancelled_einvoice(after):
					return self._record_einvoice(si, cloud_name, "Generated", note="(the call reported an error, but the IRN exists on cloud)")
				self._mark_failed(name, "aitsgst_einvoice_status", "E-Invoice", cloud_name, e.message, after)
				raise

	def _einvoice_problems(self, si, doc, settings) -> list:
		problems = []
		if doc.get("docstatus") == 2:
			problems.append("This invoice is cancelled on the cloud site.")
		if _is_cancelled_einvoice(doc):
			problems.append("The IRN for this invoice was cancelled. A cancelled IRN can never be generated again for the "
			                "same invoice number: cancel the invoice and issue a new one.")
		if doc.get("gst_category") == "Unregistered" or not doc.get("billing_address_gstin"):
			problems.append("E-invoicing applies to B2B invoices. This customer has no GSTIN, so no IRN can be generated.")

		company_cfg = self.cfg.company(si.get("company")) or {}
		if (doc.get("company_gstin") or "").upper() != (company_cfg.get("company_gstin") or "").upper():
			problems.append(f"The cloud invoice's company GSTIN ({doc.get('company_gstin') or 'blank'}) does not match the "
			                f"GSTIN mapped for this company ({company_cfg.get('company_gstin') or 'not mapped'}).")
		if si.get("aitsgst_recon_status") == "Mismatch":
			problems.append("The cloud invoice totals do not match the local invoice (see Reconciliation Detail). "
			                "Fix the templates and re-push before registering it with the GST portal.")

		if settings is not None:
			if not settings.get("enable_e_invoice"):
				problems.append("E-Invoice is not enabled in the cloud site's GST Settings.")
			if not self._has_credentials(settings, company_cfg.get("company_gstin"), "e-Invoice"):
				problems.append(f"Cloud GST Settings has no e-Invoice API credentials for GSTIN {company_cfg.get('company_gstin')}.")
			posting = _parse_dt(doc.get("posting_date"))
			applicable_from = _parse_dt(settings.get("e_invoice_applicable_from"))
			if posting and applicable_from and posting.date() < applicable_from.date():
				problems.append(f"E-invoicing is applicable on cloud from {applicable_from:%d %b %Y}; this invoice is dated {posting:%d %b %Y}.")
			limit = settings.get("e_invoice_reporting_time_limit_days") or 0
			if posting and limit and (self.store.now().date() - posting.date()).days > int(limit):
				problems.append(f"This invoice is more than {limit} days old, beyond the e-invoice reporting time limit.")
		return problems

	def _record_einvoice(self, si, cloud_name, status, note="") -> dict:
		doc = self._read(cloud_name)
		irn = doc.get("irn")
		if not irn:
			raise CloudError("The cloud site finished without returning an IRN. Check the invoice there before trying again.")
		values = self._doc_values(doc)
		self.store.update_invoice(si["name"], values, comment=f"e-Invoice generated on cloud. IRN {irn} {note}".strip())
		self.log.write("E-Invoice", "Success", si["name"], f"IRN generated for {cloud_name}. {note}".strip(),
		               cloud_invoice=cloud_name, response={"irn": irn, "ack_no": values.get("aitsgst_ack_no")})
		return {"outcome": status, "irn": irn, "ack_no": values.get("aitsgst_ack_no")}

	# ========================================================== e-way bill
	def generate_e_waybill(self, name: str, values: dict, confirm: bool = False) -> dict:
		si, cloud_name = self._require_pushed(name)
		doc = self._read(cloud_name)

		if _is_active_ewb(doc):
			self.sync_from_doc(si, doc)
			self.log.write("E-Way Bill", "Success", name, f"Already generated on cloud for {cloud_name}.", cloud_invoice=cloud_name)
			return {"outcome": "AlreadyGenerated", "ewaybill": doc.get("ewaybill")}

		settings = self._gst_settings()
		problems, clean = self._ewaybill_problems(si, doc, settings, values or {})
		problems = self._environment_problems(settings) + problems
		if problems:
			self._log_blocked("E-Way Bill", name, cloud_name, problems)
			raise Blocked("The e-way bill was not generated. Nothing was sent to the GST portal.", problems)
		if not confirm:
			raise ConfirmationRequired("Confirmation is required to generate an e-way bill.",
			                           "Generating an e-way bill registers it with the GST portal (cancellable only within 24 hours)"
			                           + (" and submits the draft invoice on cloud." if doc.get("docstatus") == 0 else "."))

		with self.store.lock(name, "e-waybill"):
			try:
				self._submit_if_draft(cloud_name, doc)
				self.client.call(f"{EWB}.generate_e_waybill", doctype=SI, docname=cloud_name, values=clean)
				return self._record_ewaybill(si, cloud_name)
			except CloudError as e:
				after = self._try_read(cloud_name)
				if after and _is_active_ewb(after):
					return self._record_ewaybill(si, cloud_name, note="(the call reported an error, but the e-way bill exists on cloud)")
				self._mark_failed(name, "aitsgst_ewb_status", "E-Way Bill", cloud_name, e.message, after)
				raise

	def _ewaybill_problems(self, si, doc, settings, r: dict):
		problems, clean = [], {}
		if doc.get("docstatus") == 2:
			problems.append("This invoice is cancelled on the cloud site.")
		company_cfg = self.cfg.company(si.get("company")) or {}
		if settings is not None:
			if not settings.get("enable_e_waybill"):
				problems.append("E-Waybill is not enabled in the cloud site's GST Settings.")
			if not self._has_credentials(settings, company_cfg.get("company_gstin"), "e-Waybill") and not (
				doc.get("irn") and self._has_credentials(settings, company_cfg.get("company_gstin"), "e-Invoice")
			):
				problems.append(f"Cloud GST Settings has no e-Waybill API credentials for GSTIN {company_cfg.get('company_gstin')}.")
		problems += self._transport_problems(r, clean, require_vehicle_for_road=True)
		return problems, clean

	def _transport_problems(self, r: dict, clean: dict, require_vehicle_for_road: bool) -> list:
		problems = []
		mode = next((m for m in MODES if m.lower() == str(r.get("mode_of_transport") or "Road").strip().lower()), None)
		if not mode:
			problems.append("Mode of transport must be Road, Rail, Air or Ship.")
		vehicle = _norm_vehicle(r.get("vehicle_no"))
		if vehicle and not _VEHICLE_NO.match(vehicle):
			problems.append("The vehicle number is not valid. Use letters and digits only, for example MH12AB1234.")
		transporter_id = (str(r.get("gst_transporter_id") or "").strip().upper()) or None
		if transporter_id and not _TRANSPORTER_ID.match(transporter_id):
			problems.append("The transporter GSTIN / Transporter ID must be 15 characters.")
		lr_no = (str(r.get("lr_no") or "").strip()) or None
		lr_date = None
		if r.get("lr_date"):
			parsed = _parse_dt(r.get("lr_date"))
			if parsed:
				lr_date = parsed.strftime("%Y-%m-%d")
			else:
				problems.append("The LR / document date is not a valid date.")
		if mode == "Road":
			if require_vehicle_for_road and not vehicle and not transporter_id:
				problems.append("For road transport, enter the vehicle number, or the transporter's GSTIN if the transporter will add the vehicle later.")
		elif mode and (not lr_no or not lr_date):
			problems.append(f"For {mode.lower()} transport, the transport document (LR/RR/AWB/BL) number and date are required.")
		try:
			distance = int(r.get("distance") or 0)
		except (TypeError, ValueError):
			distance = -1
		if not 0 <= distance <= 4000:
			problems.append("Distance must be between 0 and 4000 km (0 lets the GST portal work it out from the pincodes).")
		vehicle_type = next((v for v in VEHICLE_TYPES if v.lower() == str(r.get("gst_vehicle_type") or "Regular").strip().lower()), None)
		if not vehicle_type:
			problems.append("Vehicle type must be Regular or Over Dimensional Cargo (ODC).")

		if not problems:
			clean.update({"mode_of_transport": mode, "distance": distance})
			if mode == "Road":
				clean["gst_vehicle_type"] = vehicle_type
			for k, v in (("vehicle_no", vehicle), ("gst_transporter_id", transporter_id), ("lr_no", lr_no), ("lr_date", lr_date),
			             ("transporter_name", (str(r.get("transporter_name") or "").strip()) or None)):
				if v:
					clean[k] = v
		return problems

	def _record_ewaybill(self, si, cloud_name, note="") -> dict:
		doc = self._read(cloud_name)
		ewb = doc.get("ewaybill")
		if not ewb:
			raise CloudError("The cloud site finished without returning an e-way bill number. Check the invoice there before trying again.")
		self._wait_for_ewaybill_log(ewb)
		values = self._doc_values(doc)
		self.store.update_invoice(si["name"], values, comment=f"e-Way Bill {ewb} generated on cloud. {note}".strip())
		self.log.write("E-Way Bill", "Success", si["name"], f"E-way bill {ewb} generated for {cloud_name}. {note}".strip(),
		               cloud_invoice=cloud_name, response={"ewaybill": ewb, "valid_upto": str(values.get("aitsgst_ewb_valid_upto"))})
		return {"outcome": "Generated", "ewaybill": ewb, "valid_upto": str(values.get("aitsgst_ewb_valid_upto") or "")}

	# ======================================================= update vehicle
	def update_vehicle(self, name: str, values: dict, confirm: bool = False) -> dict:
		"""Part-B update. Allowed only while the e-way bill is active on cloud."""
		si, cloud_name = self._require_pushed(name)
		doc = self._read(cloud_name)
		r = values or {}

		problems, clean = [], {}
		if not _is_active_ewb(doc):
			problems.append("This invoice has no active e-way bill on the cloud site.")
		problems += self._transport_problems(r, clean, require_vehicle_for_road=False)
		if clean.get("mode_of_transport") == "Road" and not clean.get("vehicle_no"):
			problems.append("Enter the new vehicle number.")
		reason = next((x for x in VEHICLE_UPDATE_REASONS if x.lower() == str(r.get("reason") or "").strip().lower()), None)
		if not reason:
			problems.append("Choose a reason: " + ", ".join(VEHICLE_UPDATE_REASONS) + ".")
		remark = (str(r.get("remark") or "").strip()) or None
		if reason == "Others" and not remark:
			problems.append("Enter a remark when the reason is Others.")
		place = (str(r.get("place_of_change") or "").strip()) or None
		if not place:
			problems.append("Enter the place (city) where the vehicle changed.")
		state = next((v for v in STATES.values() if v.lower() == str(r.get("state") or "").strip().lower()), None)
		if not state:
			problems.append("Choose the state where the vehicle changed.")
		settings = self._gst_settings()
		problems = self._environment_problems(settings) + problems
		if problems:
			self._log_blocked("Update Vehicle", name, cloud_name, problems)
			raise Blocked("The vehicle was not updated. Nothing was sent to the GST portal.", problems)
		if not confirm:
			raise ConfirmationRequired("Confirmation is required to update the vehicle.", "This updates Part-B of the e-way bill on the GST portal.")

		clean.update({"reason": reason, "remark": remark or reason, "place_of_change": place, "state": state})
		with self.store.lock(name, "e-waybill"):
			try:
				self.client.call(f"{EWB}.update_vehicle_info", doctype=SI, docname=cloud_name, values=clean)
			except CloudError as e:
				self.log.write("Update Vehicle", "Failed", name, e.message + " Use Refresh to check the cloud state.",
				               cloud_invoice=cloud_name, request=clean)
				raise
			doc = self._read(cloud_name)
			self._sleep(self.EWB_LOG_WAITS[0])  # the new validity is written to the log in the background too
			values_ = self._doc_values(doc)
			self.store.update_invoice(name, values_, comment=f"e-Way Bill vehicle updated on cloud: {clean.get('vehicle_no') or clean.get('lr_no')} ({reason}).")
			self.log.write("Update Vehicle", "Success", name, f"Part-B updated ({reason}).", cloud_invoice=cloud_name, request=clean)
			return {"outcome": "Updated", "vehicle_no": values_.get("aitsgst_vehicle_no"), "valid_upto": str(values_.get("aitsgst_ewb_valid_upto") or "")}

	# ================================================================ cancel
	def cancel_e_invoice(self, name: str, reason: str, remark: str | None = None, confirm: bool = False) -> dict:
		si, cloud_name = self._require_pushed(name)
		doc = self._read(cloud_name)

		problems, reason, remark = self._cancel_request_problems(reason, remark)
		irn = doc.get("irn")
		if not irn or _is_cancelled_einvoice(doc):
			problems.append("This invoice has no active e-invoice (IRN) to cancel.")
		else:
			ack = self._einvoice_log(irn).get("acknowledged_on")
			ack_dt = _parse_dt(ack)
			if ack_dt and ack_dt + CANCEL_WINDOW < self.store.now():
				problems.append(f"An e-invoice can only be cancelled within 24 hours. This one was generated on {ack_dt:%d %b %Y %H:%M}; "
				                f"the window ended on {ack_dt + CANCEL_WINDOW:%d %b %Y %H:%M}.")
		problems = self._environment_problems(self._gst_settings()) + problems
		if problems:
			self._log_blocked("E-Invoice Cancel", name, cloud_name, problems)
			raise Blocked("The e-invoice was not cancelled. Nothing was sent to the GST portal.", problems)
		if not confirm:
			raise ConfirmationRequired("Confirmation is required to cancel an e-invoice.",
			                           "Cancelling an e-invoice cannot be undone, and this invoice number can never get a new IRN.")

		had_ewb = _is_active_ewb(doc)
		values = {"reason": reason, **({"remark": remark} if remark else {})}
		with self.store.lock(name, "e-invoice"):
			try:
				self.client.call(f"{EINV}.cancel_e_invoice", docname=cloud_name, values=values)
			except CloudError as e:
				after = self._try_read(cloud_name)
				if not (after and (not after.get("irn") or _is_cancelled_einvoice(after))):
					self.log.write("E-Invoice Cancel", "Failed", name, e.message, cloud_invoice=cloud_name, request=values)
					raise
			return self._record_cancel(si, cloud_name, "einvoice", reason, remark, had_ewb)

	def cancel_e_waybill(self, name: str, reason: str, remark: str | None = None, confirm: bool = False) -> dict:
		si, cloud_name = self._require_pushed(name)
		doc = self._read(cloud_name)

		problems, reason, remark = self._cancel_request_problems(reason, remark)
		if not _is_active_ewb(doc):
			problems.append("This invoice has no active e-way bill to cancel.")
		else:
			created = _parse_dt(self._ewaybill_log(doc["ewaybill"]).get("created_on"))
			if created and created + CANCEL_WINDOW < self.store.now():
				problems.append(f"An e-way bill can only be cancelled within 24 hours. This one was generated on {created:%d %b %Y %H:%M}; "
				                f"the window ended on {created + CANCEL_WINDOW:%d %b %Y %H:%M}.")
		problems = self._environment_problems(self._gst_settings()) + problems
		if problems:
			self._log_blocked("E-Way Bill Cancel", name, cloud_name, problems)
			raise Blocked("The e-way bill was not cancelled. Nothing was sent to the GST portal.", problems)
		if not confirm:
			raise ConfirmationRequired("Confirmation is required to cancel an e-way bill.", "Cancelling an e-way bill cannot be undone.")

		values = {"reason": reason, **({"remark": remark} if remark else {})}
		with self.store.lock(name, "e-waybill"):
			try:
				self.client.call(f"{EWB}.cancel_e_waybill", doctype=SI, docname=cloud_name, values=values)
			except CloudError as e:
				after = self._try_read(cloud_name)
				if not (after and not _is_active_ewb(after)):
					self.log.write("E-Way Bill Cancel", "Failed", name, e.message, cloud_invoice=cloud_name, request=values)
					raise
			return self._record_cancel(si, cloud_name, "ewb", reason, remark)

	def _cancel_request_problems(self, reason, remark):
		problems = []
		reason = next((x for x in CANCEL_REASONS if x.lower() == str(reason or "").strip().lower()), None)
		if not reason:
			problems.append("Choose a cancellation reason: " + ", ".join(CANCEL_REASONS) + ".")
		remark = (str(remark or "").strip()) or None
		if remark and len(remark) > 100:
			problems.append("The remark can be at most 100 characters.")
		if reason == "Others" and not remark:
			problems.append("Enter a remark when the reason is Others.")
		return problems, reason, remark

	def _record_cancel(self, si, cloud_name, what, reason, remark, had_ewb=False) -> dict:
		doc = self._read(cloud_name)
		text = f"{reason}: {remark}" if remark else reason
		now, user = self.store.now(), self.store.user()
		values = self._doc_values(doc)
		if what == "einvoice":
			if doc.get("irn") and not _is_cancelled_einvoice(doc):
				raise CloudError("The cloud site finished without cancelling the IRN. Check the invoice there before trying again.")
			values.update({"aitsgst_einvoice_status": "Cancelled", "aitsgst_einvoice_cancel_reason": text,
			               "aitsgst_einvoice_cancelled_on": now, "aitsgst_einvoice_cancelled_by": user})
			if had_ewb and not _is_active_ewb(doc):
				values.update({"aitsgst_ewb_status": "Cancelled", "aitsgst_ewb_cancel_reason": text,
				               "aitsgst_ewb_cancelled_on": now, "aitsgst_ewb_cancelled_by": user})
			action, label = "E-Invoice Cancel", "e-Invoice (IRN)"
		else:
			if _is_active_ewb(doc):
				raise CloudError("The cloud site finished without cancelling the e-way bill. Check the invoice there before trying again.")
			values.update({"aitsgst_ewb_status": "Cancelled", "aitsgst_ewb_cancel_reason": text,
			               "aitsgst_ewb_cancelled_on": now, "aitsgst_ewb_cancelled_by": user})
			action, label = "E-Way Bill Cancel", "e-Way Bill"
		self.store.update_invoice(si["name"], values, comment=f"{label} cancelled on cloud ({text}). The local invoice itself was not cancelled.")
		self.log.write(action, "Success", si["name"], f"{label} cancelled for {cloud_name} ({text}).", cloud_invoice=cloud_name)
		return {"outcome": "Cancelled"}

	# ===================================================== cancel everywhere
	def cancel_everywhere(self, name: str, reason: str | None = None, remark: str | None = None, confirm: bool = False) -> dict:
		"""Cancel e-way bill + IRN on the GST portal, the cloud invoice (a cloud draft is deleted), then
		the local invoice - in that order, after checking up front that every step can succeed.

		Each step is skipped when already done, so after a part-way failure it can simply be run again.
		"""
		si = self.store.get_invoice(name)
		if si.get("docstatus") != 1:
			raise Blocked("Only a submitted invoice can be cancelled.", f"{name} is not submitted.")
		cloud_name = si.get("aitsgst_cloud_invoice")
		doc = self.client.get_doc(SI, cloud_name) if cloud_name else None  # None = not on cloud (never pushed or deleted there)

		irn_active = bool(doc and doc.get("irn") and not _is_cancelled_einvoice(doc))
		ewb_active = bool(doc and _is_active_ewb(doc))
		problems = []
		if irn_active or ewb_active:
			p, reason, remark = self._cancel_request_problems(reason, remark)
			problems += p
			problems += self._window_problems(doc, irn_active, ewb_active)
			problems += self._environment_problems(self._gst_settings())
		if doc and doc.get("docstatus") == 1:
			problems += self._cloud_linked_problems(cloud_name)
		problems += self.store.local_cancel_problems(name)
		if problems:
			self._log_blocked("Cancel Everywhere", name, cloud_name, problems)
			raise Blocked("Nothing was cancelled - on the GST portal, on cloud or here.", problems)

		plan = self._cancel_plan(name, cloud_name, doc, irn_active, ewb_active)
		if not confirm:
			raise ConfirmationRequired("Confirmation is required to cancel everywhere.", "This will: " + "; ".join(plan) + ". It cannot be undone.")

		done = []
		with self.store.lock(name, "cancel"):
			try:
				if irn_active:  # India Compliance cancels the e-way bill first, with the same reason
					self._cancel_portal(f"{EINV}.cancel_e_invoice", {"docname": cloud_name}, cloud_name, reason, remark,
					                    done_when=lambda d: not d.get("irn") or _is_cancelled_einvoice(d))
					done.append("IRN cancelled on the GST portal" + (" with its e-way bill" if ewb_active else ""))
				elif ewb_active:
					self._cancel_portal(f"{EWB}.cancel_e_waybill", {"doctype": SI, "docname": cloud_name}, cloud_name, reason, remark,
					                    done_when=lambda d: not _is_active_ewb(d))
					done.append("e-Way Bill cancelled on the GST portal")

				if doc and doc.get("docstatus") == 1:
					self._call_then_check("frappe.client.cancel", cloud_name, lambda d: d and d.get("docstatus") == 2)
					done.append(f"Cloud invoice {cloud_name} cancelled")
				elif doc and doc.get("docstatus") == 0:
					self._call_then_check("frappe.client.delete", cloud_name, lambda d: d is None)
					done.append(f"Cloud draft {cloud_name} deleted")
			except CloudError as e:
				self._record_cloud_cancel_state(si, cloud_name, reason, remark)
				self.log.write("Cancel Everywhere", "Failed", name, "Done: " + ("; ".join(done) or "nothing") + f". Failed: {e.message}",
				               cloud_invoice=cloud_name)
				raise Blocked("Cancel Everywhere stopped part-way. The local invoice was NOT cancelled.",
				              [*(f"Done: {d}" for d in done), f"Failed: {e.message}",
				               "Fix the problem and run Cancel Everywhere again - finished steps are skipped."])

			self._record_cloud_cancel_state(si, cloud_name, reason, remark)
			try:
				self.store.cancel_invoice(name)
			except Exception as e:  # e.g. a document was linked meanwhile
				message = str(e) or type(e).__name__
				self.log.write("Cancel Everywhere", "Failed", name, "; ".join(done) + f". Local cancel failed: {message}", cloud_invoice=cloud_name)
				raise Blocked("Cancelled on cloud, but the local invoice could not be cancelled.",
				              [*(f"Done: {d}" for d in done), f"Local cancel failed: {message}",
				               "Fix that, then cancel this invoice with the normal Cancel button."])
		done.append(f"Local invoice {name} cancelled")
		self.log.write("Cancel Everywhere", "Success", name, "; ".join(done), cloud_invoice=cloud_name)
		return {"outcome": "Cancelled", "steps": done}

	def _cancel_plan(self, name, cloud_name, doc, irn_active, ewb_active) -> list:
		plan = []
		if irn_active:
			plan.append("cancel the IRN" + (" and e-way bill" if ewb_active else "") + " on the GST portal")
		elif ewb_active:
			plan.append("cancel the e-way bill on the GST portal")
		if doc and doc.get("docstatus") == 1:
			plan.append(f"cancel cloud invoice {cloud_name}")
		elif doc and doc.get("docstatus") == 0:
			plan.append(f"delete cloud draft {cloud_name}")
		plan.append(f"cancel local invoice {name}")
		return plan

	def _window_problems(self, doc, irn_active, ewb_active) -> list:
		problems = []
		now = self.store.now()
		if irn_active:
			ack = _parse_dt(self._einvoice_log(doc["irn"]).get("acknowledged_on"))
			if ack and ack + CANCEL_WINDOW < now:
				problems.append(f"The IRN was generated on {ack:%d %b %Y %H:%M}; the GST portal allows cancelling only within 24 hours. "
				                "Issue a credit note instead of cancelling this invoice.")
		if ewb_active:
			created = _parse_dt(self._ewaybill_log(doc["ewaybill"]).get("created_on"))
			if created and created + CANCEL_WINDOW < now:
				problems.append(f"The e-way bill was generated on {created:%d %b %Y %H:%M}; it can be cancelled only within 24 hours.")
		return problems

	def _cloud_linked_problems(self, cloud_name) -> list:
		try:
			result = self.client.call("frappe.desk.form.linked_with.get_submitted_linked_docs", doctype=SI, name=cloud_name) or {}
		except CloudError as e:
			return [f"Could not check documents linked to {cloud_name} on cloud ({e.message}). Nothing was cancelled."]
		docs = [d for d in result.get("docs") or [] if d.get("doctype") not in IGNORED_LINKED_DOCTYPES]
		if docs:
			listed = ", ".join(f"{d.get('doctype')} {d.get('name')}" for d in docs[:5])
			return [f"On the cloud site, {cloud_name} has submitted documents linked to it ({listed}). Cancel those on cloud first."]
		return []

	def _cancel_portal(self, method, args, cloud_name, reason, remark, done_when):
		values = {"reason": reason, **({"remark": remark} if remark else {})}
		try:
			self.client.call(method, **args, values=values)
		except CloudError:
			after = self._try_read(cloud_name)
			if not (after and done_when(after)):
				raise

	def _call_then_check(self, method, cloud_name, done_when):
		try:
			self.client.call(method, doctype=SI, name=cloud_name)
		except CloudError:
			# Re-read before reporting failure. get_doc raising here propagates: never guess "deleted".
			if not done_when(self.client.get_doc(SI, cloud_name)):
				raise

	def _record_cloud_cancel_state(self, si, cloud_name, reason, remark):
		"""Write whatever the cloud now says (IRN / e-way bill / invoice status) onto the local invoice."""
		if not cloud_name:
			return
		after = self._try_read(cloud_name)
		text = (f"{reason}: {remark}" if remark else reason) or None
		now, user = self.store.now(), self.store.user()
		values = self._doc_values(after) if after else {"aitsgst_cloud_docstatus": "Deleted", "aitsgst_last_synced": now}
		if values.get("aitsgst_einvoice_status") == "Cancelled" and si.get("aitsgst_einvoice_status") != "Cancelled":
			values.update(aitsgst_einvoice_cancel_reason=text, aitsgst_einvoice_cancelled_on=now, aitsgst_einvoice_cancelled_by=user)
		if values.get("aitsgst_ewb_status") == "Cancelled" and si.get("aitsgst_ewb_status") != "Cancelled":
			values.update(aitsgst_ewb_cancel_reason=text, aitsgst_ewb_cancelled_on=now, aitsgst_ewb_cancelled_by=user)
		if si.get("aitsgst_ewb_status") == "Generated" and after and not _is_active_ewb(after):
			values.update(aitsgst_ewb_status="Cancelled", aitsgst_ewb_cancel_reason=text, aitsgst_ewb_cancelled_on=now, aitsgst_ewb_cancelled_by=user)
		self.store.update_invoice(si["name"], values)

	# ============================================================ sync-back
	def refresh(self, name: str) -> dict:
		si, cloud_name = self._require_pushed(name)
		doc = self._read(cloud_name)
		changed = self.sync_from_doc(si, doc)
		self.log.write("Refresh", "Success", name, "Updated from cloud." if changed else "No change on cloud.", cloud_invoice=cloud_name)
		return {"outcome": "Refreshed", "changed": changed}

	def sync_from_doc(self, si: dict, doc: dict) -> bool:
		"""Writes the cloud state onto the local invoice; returns True if anything changed."""
		values = self._doc_values(doc)
		# Cancellations made directly on the cloud site: take reason/date from the cloud logs.
		if values.get("aitsgst_einvoice_status") == "Cancelled" and si.get("aitsgst_einvoice_status") != "Cancelled":
			log = self._einvoice_log(doc.get("irn")) if doc.get("irn") else {}
			values.setdefault("aitsgst_einvoice_cancel_reason", log.get("cancel_reason_code") or "Cancelled on cloud")
			values.setdefault("aitsgst_einvoice_cancelled_on", _parse_dt(log.get("cancelled_on")) or self.store.now())
			values.setdefault("aitsgst_einvoice_cancelled_by", "cloud")
		if values.get("aitsgst_ewb_status") == "Cancelled" and si.get("aitsgst_ewb_status") != "Cancelled":
			values.setdefault("aitsgst_ewb_cancel_reason", "Cancelled on cloud")
			values.setdefault("aitsgst_ewb_cancelled_on", self.store.now())
			values.setdefault("aitsgst_ewb_cancelled_by", "cloud")

		cancelled_on_cloud = (values.get("aitsgst_cloud_docstatus") == "Cancelled" and si.get("aitsgst_cloud_docstatus") != "Cancelled"
		                      and si.get("docstatus") == 1)
		changed = {k: v for k, v in values.items() if k != "aitsgst_last_synced" and _differs(si.get(k), v)}
		self.store.update_invoice(si["name"], values, comment=(
			"Cloud GST status changed: " + ", ".join(f"{k.replace('aitsgst_', '')}={v}" for k, v in changed.items()
			                                         if k in ("aitsgst_einvoice_status", "aitsgst_ewb_status", "aitsgst_cloud_docstatus", "aitsgst_vehicle_no"))
		) if changed and any(k in changed for k in ("aitsgst_einvoice_status", "aitsgst_ewb_status", "aitsgst_cloud_docstatus", "aitsgst_vehicle_no")) else None)
		if cancelled_on_cloud:
			self.store.on_cloud_cancelled(si["name"])  # notify, or cancel locally if that setting is on
		return bool(changed)

	def _doc_values(self, doc: dict) -> dict:
		"""Local field values for a cloud invoice (plus its e-Invoice / e-Waybill logs)."""
		values = {"aitsgst_cloud_docstatus": docstatus_text(doc.get("docstatus")), "aitsgst_last_synced": self.store.now()}

		irn = doc.get("irn")
		einv_status = doc.get("einvoice_status")
		if irn:
			log = self._einvoice_log(irn)
			values.update({"aitsgst_irn": irn, "aitsgst_ack_no": log.get("acknowledgement_number"),
			               "aitsgst_ack_date": _parse_dt(log.get("acknowledged_on")), "aitsgst_signed_qr_code": log.get("signed_qr_code")})
			values["aitsgst_einvoice_status"] = "Cancelled" if _is_cancelled_einvoice(doc) else "Generated"
		elif einv_status == "Failed":
			values["aitsgst_einvoice_status"] = "Failed"
		elif einv_status in ("Cancelled", "Manually Cancelled"):
			values["aitsgst_einvoice_status"] = "Cancelled"

		ewb = doc.get("ewaybill")
		if ewb:
			log = self._ewaybill_log(ewb)
			values.update({"aitsgst_ewaybill": ewb, "aitsgst_ewb_date": _parse_dt(log.get("created_on")),
			               "aitsgst_ewb_valid_upto": _parse_dt(log.get("valid_upto"))})
			values["aitsgst_ewb_status"] = "Cancelled" if doc.get("e_waybill_status") == "Cancelled" else "Generated"
		elif doc.get("e_waybill_status") == "Cancelled":
			values["aitsgst_ewb_status"] = "Cancelled"

		for local, cloud in (("aitsgst_vehicle_no", "vehicle_no"), ("aitsgst_mode_of_transport", "mode_of_transport"),
		                     ("aitsgst_transporter_id", "gst_transporter_id"), ("aitsgst_transporter_name", "transporter_name"),
		                     ("aitsgst_lr_no", "lr_no"), ("aitsgst_lr_date", "lr_date"), ("aitsgst_distance", "distance")):
			if doc.get(cloud) not in (None, ""):
				values[local] = doc.get(cloud)
		return values

	# =============================================================== helpers
	def _require_pushed(self, name):
		si = self.store.get_invoice(name)
		cloud_name = si.get("aitsgst_cloud_invoice")
		if not cloud_name or si.get("aitsgst_push_status") != "Pushed":
			raise Blocked("This invoice has not been pushed to cloud yet.", "Push it to cloud first, then generate the e-invoice or e-way bill.")
		if not self.cfg.company(si.get("company")):
			raise Blocked("This company is not mapped in AITS GST Settings.", f"Map company {si.get('company')} first.")
		return si, cloud_name

	def _read(self, cloud_name) -> dict:
		doc = self.client.get_doc(SI, cloud_name)
		if doc is None:
			raise Blocked(f"Sales Invoice {cloud_name} was not found on the cloud site.", "It may have been deleted or renamed there.")
		return doc

	def _try_read(self, cloud_name):
		try:
			return self.client.get_doc(SI, cloud_name)
		except CloudError:
			return None

	def _submit_if_draft(self, cloud_name, doc):
		if doc.get("docstatus") == 0:
			self.client.update(SI, cloud_name, {"docstatus": 1})

	def _gst_settings(self):
		return self.client.get_doc("GST Settings", "GST Settings")

	def _environment_problems(self, settings) -> list:
		if settings is None:
			return ["The cloud site's GST Settings could not be read (check the API user's permissions)."]
		problems = []
		if not settings.get("enable_api"):
			problems.append("The India Compliance API is not enabled in the cloud site's GST Settings.")
		if not settings.get("sandbox_mode") and not self.cfg.allow_production:
			problems.append("The cloud site's GST Settings are in PRODUCTION mode (live GST portal), and 'Allow production' "
			                "is off in AITS GST Settings. Nothing was sent.")
		return problems

	@staticmethod
	def _has_credentials(settings, gstin, service) -> bool:
		return any((c.get("gstin") or "").upper() == (gstin or "").upper() and service.lower() in (c.get("service") or "").lower()
		           for c in settings.get("credentials") or [])

	def _einvoice_log(self, irn) -> dict:
		try:
			return self.client.get_doc("e-Invoice Log", irn) or {}
		except CloudError:
			return {}  # best effort: the IRN on the invoice is what matters

	def _ewaybill_log(self, ewb) -> dict:
		try:
			return self.client.get_doc("e-Waybill Log", str(ewb)) or {}
		except CloudError:
			return {}

	def _mark_failed(self, name, status_field, action, cloud_name, message, after):
		values = {status_field: "Failed", "aitsgst_last_error": message}
		if after:
			values["aitsgst_cloud_docstatus"] = docstatus_text(after.get("docstatus"))
		self.store.update_invoice(name, values)
		self.log.write(action, "Failed", name, message, cloud_invoice=cloud_name)

	def _log_blocked(self, action, name, cloud_name, problems):
		self.log.write(action, "Blocked", name, "\n".join(problems), cloud_invoice=cloud_name)


def _differs(a, b) -> bool:
	if a in (None, "") and b in (None, ""):
		return False
	return str(a) != str(b)
