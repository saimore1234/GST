"""Scheduler jobs: push retries with backoff, and polling the cloud for changes
(e.g. an IRN cancelled or a vehicle updated directly in the GST service).

Only idempotent work runs unattended: a retried push looks the invoice up by key
before creating, and polling only reads. Nothing is generated, cancelled or
submitted on a schedule.
"""

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime, today

from aitsgst.core.cloud_client import CloudError
from aitsgst.services.compliance import ComplianceService
from aitsgst.services.context import GateClosed, get_context, load_config

POLL_FIELDS = ["name", "docstatus", "irn", "einvoice_status", "ewaybill", "e_waybill_status", "vehicle_no"]
BATCH = 100


def run_due_jobs():
	cfg = load_config()
	if not cfg.enabled or not cfg.gate_open:
		return
	retry_failed_pushes()

	settings = frappe.get_cached_doc("AITS GST Settings")
	last = settings.last_status_poll
	if not last or add_to_date(get_datetime(last), minutes=settings.sync_interval_minutes or 15) <= now_datetime():
		frappe.db.set_single_value("AITS GST Settings", "last_status_poll", now_datetime())
		frappe.db.commit()
		frappe.enqueue("aitsgst.tasks.poll_status", queue="long", timeout=1500, job_id="aitsgst-poll", deduplicate=True)


def retry_failed_pushes():
	from aitsgst.api import enqueue_push

	due = frappe.get_all(
		"AITS GST Sync Log",
		filters={"action": "Push", "status": "Retry Scheduled", "next_retry_at": ["<=", now_datetime()]},
		fields=["name", "reference_name", "retry_count"],
		order_by="creation asc",
		limit=50,
	)
	for row in due:
		frappe.db.set_value("AITS GST Sync Log", row.name, "status", "Retried", update_modified=False)
		if frappe.db.get_value("Sales Invoice", row.reference_name, "aitsgst_push_status") != "Ready":
			enqueue_push(row.reference_name, retry_count=(row.retry_count or 0) + 1)
	frappe.db.commit()


def poll_status():
	try:
		ctx = get_context()
	except GateClosed:
		return
	svc = ComplianceService(ctx)
	settings = frappe.get_cached_doc("AITS GST Settings")
	since = add_days(today(), -(settings.sync_lookback_days or 7))

	# Recent pushed invoices, plus any with a live e-way bill regardless of age.
	local = frappe.get_all(
		"Sales Invoice",
		filters={"aitsgst_push_status": "Ready", "aitsgst_cloud_invoice": ["is", "set"]},
		or_filters={"posting_date": [">=", since], "aitsgst_ewb_status": "Generated"},
		fields=["name", "aitsgst_cloud_invoice", "aitsgst_cloud_docstatus", "aitsgst_irn", "aitsgst_einvoice_status",
		        "aitsgst_ewaybill", "aitsgst_ewb_status", "aitsgst_ewb_valid_upto", "aitsgst_vehicle_no"],
		limit=5000,
	)
	by_cloud = {row.aitsgst_cloud_invoice: row for row in local}
	names = list(by_cloud)
	changed = errors = 0
	for i in range(0, len(names), BATCH):
		batch = names[i : i + BATCH]
		try:
			rows = ctx.client.get_list("Sales Invoice", [["name", "in", batch]], POLL_FIELDS, len(batch))
		except CloudError as e:
			ctx.log.write("Status Poll", "Failed", None, e.message)
			return
		for missing in set(batch) - {row["name"] for row in rows}:
			local_row = by_cloud[missing]
			if local_row.aitsgst_cloud_docstatus == "Deleted":
				continue
			try:
				if ctx.client.get_doc("Sales Invoice", missing) is None:  # a definite 404, not a permission error
					ctx.store.update_invoice(local_row.name, {"aitsgst_cloud_docstatus": "Deleted", "aitsgst_last_synced": now_datetime()})
					from aitsgst.services.context import notify_managers

					notify_managers(local_row.name, f"The e-invoice record of {local_row.name} was deleted in the GST service.")
					changed += 1
			except CloudError:
				errors += 1
		for row in rows:
			if not _looks_changed(by_cloud[row["name"]], row):
				continue
			try:
				doc = ctx.client.get_doc("Sales Invoice", row["name"])
				if doc and svc.sync_from_doc(ctx.store.get_invoice(by_cloud[row["name"]].name), doc):
					changed += 1
			except CloudError:
				errors += 1
	if changed or errors:
		ctx.log.write("Status Poll", "Failed" if errors else "Success", None,
		              f"Checked {len(names)} invoice(s): {changed} updated from the GST service, {errors} error(s).")


def _looks_changed(local, cloud) -> bool:
	"""Cheap comparison on the list fields; a full read happens only when something differs."""
	from aitsgst.services.push import docstatus_text

	irn_status = None
	if cloud.get("irn"):
		irn_status = "Cancelled" if cloud.get("einvoice_status") in ("Cancelled", "Manually Cancelled") else "Generated"
	ewb_status = None
	if cloud.get("ewaybill"):
		ewb_status = "Cancelled" if cloud.get("e_waybill_status") == "Cancelled" else "Generated"
	elif cloud.get("e_waybill_status") == "Cancelled":
		ewb_status = "Cancelled"
	pairs = [
		(local.aitsgst_cloud_docstatus, docstatus_text(cloud.get("docstatus"))),
		(local.aitsgst_irn, cloud.get("irn")),
		(local.aitsgst_einvoice_status if irn_status else None, irn_status),
		(local.aitsgst_ewaybill if cloud.get("ewaybill") else None, cloud.get("ewaybill")),
		(local.aitsgst_ewb_status if ewb_status else None, ewb_status),
		(local.aitsgst_vehicle_no if cloud.get("vehicle_no") else None, cloud.get("vehicle_no")),
	]
	# India Compliance writes the e-Waybill Log in the background; fill in the validity once it exists.
	missing_validity = ewb_status == "Generated" and not local.get("aitsgst_ewb_valid_upto")
	return missing_validity or any((a or None) != (b or None) for a, b in pairs)


def cancel_local_after_cloud(name: str):
	"""Only runs when 'Auto-cancel local invoice when cancelled in the GST service' is ON."""
	from aitsgst.services.context import LocalStore, SyncLog, notify_managers

	frappe.set_user("Administrator")
	if frappe.db.get_value("Sales Invoice", name, "docstatus") != 1:
		return
	store, log = LocalStore(), SyncLog()
	problems = store.local_cancel_problems(name)
	if problems:
		log.write("Cancel Everywhere", "Blocked", name, "Cancelled in the GST service; local auto-cancel blocked: " + " ".join(problems))
		notify_managers(name, f"{name} was cancelled in the GST service but could not be cancelled here automatically: {' '.join(problems)}")
		return
	try:
		store.cancel_invoice(name)
		log.write("Cancel Everywhere", "Success", name, "Cancelled locally because it was cancelled in the GST service.")
	except Exception as e:
		frappe.db.rollback()
		log.write("Cancel Everywhere", "Failed", name, f"Cancelled in the GST service; local auto-cancel failed: {e}")
		notify_managers(name, f"{name} was cancelled in the GST service but the automatic local cancel failed. Cancel it manually.")


def refresh_one(name: str):
	"""Webhook-triggered re-read of one invoice."""
	frappe.set_user("Administrator")
	try:
		ComplianceService(get_context()).refresh(name)
	except (GateClosed, CloudError):
		pass
	except Exception:
		frappe.log_error(title=f"AITS GST webhook refresh failed: {name}")
