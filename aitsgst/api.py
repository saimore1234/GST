"""Whitelisted endpoints behind the Sales Invoice "e-Invoice" buttons.

Every endpoint requires the AITS GST Manager (or System Manager) role AND read
permission on the invoice. Irreversible actions (generate / cancel IRN or
e-way bill, Part-B) additionally require confirm=1, which the form only sends
after the user ticks an explicit confirmation in a dialog.
"""

import base64
import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, escape_html

from aitsgst.core.cloud_client import CloudError
from aitsgst.core.gst import is_valid_gstin, normalize_gstin
from aitsgst.services.compliance import Blocked, ComplianceService, ConfirmationRequired
from aitsgst.services.context import GateClosed, SyncLog, build_client, get_context, load_config
from aitsgst.services.push import COMPANY_FIELD, PushService

ROLES = ("AITS GST Manager", "System Manager")


def require_role():
	# Not frappe.only_for: that one is skipped whenever tests run, so it could never be tested.
	if frappe.session.user != "Administrator" and set(ROLES).isdisjoint(frappe.get_roles()):
		raise frappe.PermissionError(_("Only AITS GST Manager or System Manager can do this."))


def _guard(name: str | None = None):
	require_role()
	if name:
		frappe.has_permission("Sales Invoice", "read", name, throw=True)


def _run(fn):
	"""Turns service exceptions into clean messages for the form."""
	try:
		return fn()
	except ConfirmationRequired as e:
		frappe.throw(escape_html(" ".join(e.problems)), title=e.headline)
	except Blocked as e:
		items = "".join(f"<li>{escape_html(p)}</li>" for p in e.problems)
		frappe.throw(f"<ul>{items}</ul>", title=e.headline)
	except CloudError as e:
		frappe.throw(escape_html(e.message), title=_("GST service"))


def _values(values) -> dict:
	return frappe.parse_json(values) if values else {}


# ===================================================================== push
@frappe.whitelist(methods=["POST"])
def push_invoice(name: str):
	_guard(name)
	cfg = load_config()
	build_client(cfg)  # fail now (gate closed / not configured) rather than in the background
	enqueue_push(name)
	return {"queued": True, "workers": _worker_count()}


def _worker_count() -> int | None:
	try:
		from frappe.utils.background_jobs import get_workers

		return len(get_workers())
	except Exception:
		return None  # Redis unreachable: unknown


def enqueue_push(name: str, retry_count: int = 0):
	frappe.db.set_value("Sales Invoice", name, "aitsgst_push_status", "Queued", update_modified=False)
	frappe.enqueue(
		"aitsgst.api.run_push", queue="long", timeout=600, enqueue_after_commit=True,
		job_id=f"aitsgst-push-{name}", deduplicate=True,
		name=name, retry_count=retry_count, notify_user=frappe.session.user,
	)


def run_push(name: str, retry_count: int = 0, notify_user: str | None = None):
	"""Background job. Never raises for expected failures: the outcome is on the invoice and in the log."""
	try:
		result = PushService(get_context()).push(name, retry_count=retry_count)
	except GateClosed as e:
		result = {"outcome": "Failed", "error": str(e)}
		frappe.db.set_value("Sales Invoice", name, {"aitsgst_push_status": "Failed", "aitsgst_last_error": str(e)}, update_modified=False)
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value("Sales Invoice", name, {"aitsgst_push_status": "Failed",
		                    "aitsgst_last_error": "Unexpected error while pushing. See Error Log."}, update_modified=False)
		frappe.db.commit()
		frappe.log_error(title=f"AITS GST push failed: {name}")
		result = {"outcome": "Failed", "error": "Unexpected error. See Error Log."}
	if notify_user:
		frappe.publish_realtime("aitsgst_push_done", {"name": name, **result}, user=notify_user, after_commit=True)
	return result


# ========================================================== e-invoice / e-way bill
@frappe.whitelist(methods=["POST"])
def generate_e_invoice(name: str, confirm=0):
	_guard(name)
	return _run(lambda: ComplianceService(get_context()).generate_e_invoice(name, confirm=cint(confirm)))


@frappe.whitelist(methods=["POST"])
def generate_e_waybill(name: str, values=None, confirm=0):
	_guard(name)
	return _run(lambda: ComplianceService(get_context()).generate_e_waybill(name, _values(values), confirm=cint(confirm)))


@frappe.whitelist(methods=["POST"])
def update_vehicle(name: str, values=None, confirm=0):
	_guard(name)
	return _run(lambda: ComplianceService(get_context()).update_vehicle(name, _values(values), confirm=cint(confirm)))


@frappe.whitelist(methods=["POST"])
def cancel_e_invoice(name: str, reason: str, remark: str | None = None, confirm=0):
	_guard(name)
	return _run(lambda: ComplianceService(get_context()).cancel_e_invoice(name, reason, remark, confirm=cint(confirm)))


@frappe.whitelist(methods=["POST"])
def cancel_e_waybill(name: str, reason: str, remark: str | None = None, confirm=0):
	_guard(name)
	return _run(lambda: ComplianceService(get_context()).cancel_e_waybill(name, reason, remark, confirm=cint(confirm)))


@frappe.whitelist(methods=["POST"])
def cancel_everywhere(name: str, reason: str | None = None, remark: str | None = None, confirm=0):
	_guard(name)
	return _run(lambda: ComplianceService(get_context()).cancel_everywhere(name, reason, remark, confirm=cint(confirm)))


@frappe.whitelist(methods=["POST"])
def refresh_invoice(name: str):
	_guard(name)
	return _run(lambda: ComplianceService(get_context()).refresh(name))


@frappe.whitelist()
def get_transport_defaults(name: str):
	"""Pre-fill for the e-way bill dialog from the local invoice's India Compliance transport fields."""
	_guard(name)
	si = frappe.db.get_value("Sales Invoice", name, ["mode_of_transport", "vehicle_no", "gst_vehicle_type", "gst_transporter_id",
	                                                 "transporter_name", "lr_no", "lr_date", "distance"], as_dict=True) or {}
	return {k: v for k, v in si.items() if v not in (None, "")}


# ================================================================ GSTIN autofill
GSTIN_INFO_METHOD = "india_compliance.gst_india.utils.gstin_info.get_gstin_info"
GSTIN_CACHE_SECONDS = 24 * 60 * 60
GSTIN_FIELDS = ("gstin", "business_name", "gst_category", "status", "permanent_address", "all_addresses")


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
def get_gstin_details(gstin: str):
	"""Legal name, GST category, status and registered addresses of a GSTIN, looked up through the
	GST service's India Compliance API. Returns {"disabled": True} when the feature is off."""
	if frappe.session.user == "Guest" or not frappe.get_cached_doc("User", frappe.session.user).has_desk_access():
		raise frappe.PermissionError(_("Not allowed"))

	gstin = normalize_gstin(gstin)
	if not is_valid_gstin(gstin):
		frappe.throw(_("{0} is not a valid GSTIN.").format(escape_html(gstin or "")), title=_("GSTIN"))

	if not _gstin_autofill_enabled():
		return {"disabled": True}

	cache_key = f"aitsgst:gstin:{gstin}"
	# expires=True: read Redis directly; without it a miss is memoised for the rest of the request.
	cached = frappe.cache.get_value(cache_key, expires=True)
	if cached:
		return cached

	try:
		info = get_context().client.call(GSTIN_INFO_METHOD, gstin=gstin) or {}
	except CloudError as e:
		frappe.throw(_("Could not fetch details for GSTIN {0}: {1}").format(gstin, escape_html(e.message)), title=_("GSTIN"))

	result = {k: info.get(k) for k in GSTIN_FIELDS}
	result["permanent_address"] = _clean_address(result["permanent_address"])
	result["all_addresses"] = [_clean_address(a) for a in result["all_addresses"] or []]
	if result["business_name"] or result["permanent_address"]:
		frappe.cache.set_value(cache_key, result, expires_in_sec=GSTIN_CACHE_SECONDS)
	return result


@frappe.whitelist()
def get_gstin_info_compat(gstin: str | None = None, doc=None, throw_error=True):
	"""Replaces India Compliance's get_gstin_info (override_whitelisted_methods), so its own quick-entry
	popups and setup wizard autofill through the GST service. Same result shape as India Compliance.
	When AITS GST autofill is off, India Compliance's original runs unchanged."""
	if not _gstin_autofill_enabled():
		from india_compliance.gst_india.utils.gstin_info import get_gstin_info

		return get_gstin_info(gstin, doc=doc, throw_error=throw_error)
	try:
		return frappe._dict(get_gstin_details(gstin))
	except Exception:
		if cint(throw_error):
			raise
		frappe.clear_last_message()
		return frappe._dict()


def _clean_address(address: dict | None) -> dict | None:
	"""The GST portal's address parts often end in stray separators ("PU 4 Commercial, ")."""
	if not address:
		return address
	return {k: (v.strip().strip(",").strip() or None) if isinstance(v, str) else v for k, v in address.items()}


def _gstin_autofill_enabled() -> bool:
	cfg = load_config()
	return bool(cfg.enabled and cfg.gate_open and frappe.db.get_single_value("AITS GST Settings", "gstin_autofill"))


# ======================================================================= setup
@frappe.whitelist(methods=["POST"])
def test_connection():
	"""Read-only checks against the GST service. Never creates or changes anything there."""
	require_role()
	checks = []

	def check(name, ok, detail=""):
		checks.append({"name": name, "ok": bool(ok), "detail": detail})
		return ok

	try:
		cfg = load_config()
		client = build_client(cfg)
	except (GateClosed, CloudError, frappe.ValidationError) as e:
		check("Configuration", False, getattr(e, "message", None) or str(e))
		return _checks_result(checks)

	try:
		user = client.call_get("frappe.auth.get_logged_user")
		check("Credentials", True, f"Logged in to the GST service as {user}")
	except CloudError as e:
		check("Credentials", False, e.message)
		return _checks_result(checks)

	for company, row in cfg.companies.items():
		if not row.get("enabled"):
			continue
		try:
			doc = client.get_doc("Company", row["cloud_company"])
			check(f"GST service company for {company}", doc, row["cloud_company"] if doc else f"'{row['cloud_company']}' not found in the GST service")
		except CloudError as e:
			check(f"GST service company for {company}", False, e.message)

	try:
		gst = client.get_doc("GST Settings", "GST Settings")
		if check("GST service settings readable", gst is not None, "" if gst else "API user cannot read GST Settings"):
			check("GST service India Compliance API enabled", gst.get("enable_api"))
			check("GST service e-Invoice enabled", gst.get("enable_e_invoice"))
			check("GST service e-Waybill enabled", gst.get("enable_e_waybill"))
			sandbox = bool(gst.get("sandbox_mode"))
			check("GST service mode", sandbox or cfg.allow_production,
			      "SANDBOX" if sandbox else "PRODUCTION (live GST portal)" + ("" if cfg.allow_production else " - blocked until 'Allow production' is checked"))
	except CloudError as e:
		check("GST service settings readable", False, e.message)

	for doctype in ("Sales Invoice", "Customer", "Address", "Item"):
		try:
			client.get_list(doctype, [[cfg.key_field, "=", "__aitsgst_probe__"]], ["name"], 1)
			check(f"Key field {cfg.key_field} in the GST service {doctype}", True)
		except CloudError as e:
			check(f"Key field {cfg.key_field} in the GST service {doctype}", False,
			      f"Missing or not queryable - create custom field '{cfg.key_field}' (Data, unique for Sales Invoice) in the GST service. {e.message}")

	for doctype in ("Customer", "Item", "Address"):
		present = _service_has_field(client, doctype, COMPANY_FIELD)
		check(f"Company field on GST service {doctype}", True,
		      "present: each company has its own records" if present
		      else "not present: records are shared by all companies (fine for one client; use 'Set up GST Service Fields' for several)")

	for (template_type, local), cloud_name in cfg.template_map.items():
		try:
			check(f"GST service {template_type} '{cloud_name}'", client.get_doc(template_type, cloud_name) is not None)
		except CloudError as e:
			check(f"GST service {template_type} '{cloud_name}'", False, e.message)

	return _checks_result(checks)


def _service_has_field(client, doctype: str, fieldname: str) -> bool:
	try:
		client.get_list(doctype, [[fieldname, "=", "__aitsgst_probe__"]], ["name"], 1)
		return True
	except CloudError as e:
		if e.status_code in (400, 417):
			return False
		raise


def service_field_definitions(key_field: str) -> list:
	"""Custom fields the app needs on the GST service. Created by 'Set up GST Service Fields', or by hand."""
	fields = [
		{"dt": dt, "fieldname": key_field, "label": "AITS GST Key", "fieldtype": "Data", "read_only": 1, "no_copy": 1,
		 "search_index": 1, "unique": 1 if dt == "Sales Invoice" else 0}
		for dt in ("Sales Invoice", "Customer", "Address", "Item")
	]
	fields += [
		{"dt": dt, "fieldname": COMPANY_FIELD, "label": "Client Company", "fieldtype": "Link", "options": "Company",
		 "no_copy": 1, "search_index": 1, "in_standard_filter": 1}
		for dt in ("Customer", "Address", "Item")
	]
	return fields


@frappe.whitelist(methods=["POST"])
def setup_service_fields(confirm=0):
	"""Creates the missing custom fields on the GST service. Needs a GST service API user allowed to create
	Custom Fields (System Manager). Never changes or deletes an existing field."""
	if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError(_("Only a System Manager can set up GST service fields."))
	cfg = load_config()
	client = build_client(cfg)
	if not cint(confirm):
		frappe.throw(_("Confirmation is required: this adds custom fields to the GST service."))

	results = []
	for field in service_field_definitions(cfg.key_field):
		label = f"{field['dt']}.{field['fieldname']}"
		try:
			if _service_has_field(client, field["dt"], field["fieldname"]):
				results.append({"field": label, "status": "already present"})
				continue
			client.insert("Custom Field", field)
			results.append({"field": label, "status": "created"})
		except CloudError as e:
			hint = " (the GST service API user needs the System Manager role to create fields; or create it by hand)" if e.status_code == 403 else ""
			results.append({"field": label, "status": f"failed: {e.message}{hint}"})
	ok = all(not r["status"].startswith("failed") for r in results)
	SyncLog().write("Setup Fields", "Success" if ok else "Failed", None, "; ".join(f"{r['field']}: {r['status']}" for r in results))
	return {"success": ok, "results": results}


def _checks_result(checks):
	result = {"success": all(c["ok"] for c in checks), "checks": checks}
	SyncLog().write("Test Connection", "Success" if result["success"] else "Failed", None,
	                "; ".join(f"{'OK' if c['ok'] else 'FAIL'} {c['name']}" for c in checks))
	return result


# ===================================================================== webhook
@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=300, seconds=60)
def status_webhook():
	"""Cloud Webhook (Sales Invoice, on update / on cancel) -> re-read that invoice from cloud.

	The payload is never trusted: it only says WHICH GST service record to re-read, and is
	accepted only with a valid X-Frappe-Webhook-Signature. Unknown invoices are ignored
	without saying so.
	"""
	secret = frappe.get_single("AITS GST Settings").get_password("webhook_secret", raise_exception=False)
	body = frappe.request.get_data() or b""
	if not secret or not verify_signature(secret, body, frappe.get_request_header("X-Frappe-Webhook-Signature")):
		frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

	try:
		cloud_name = (json.loads(body or b"{}") or {}).get("name")
	except ValueError:
		cloud_name = None
	local = frappe.db.get_value("Sales Invoice", {"aitsgst_cloud_invoice": cloud_name}, "name") if cloud_name else None
	if local:
		frappe.enqueue("aitsgst.tasks.refresh_one", queue="short", job_id=f"aitsgst-refresh-{local}", deduplicate=True, name=local)
	return {"ok": True}


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
	if not signature:
		return False
	expected = base64.b64encode(hmac.new(secret.encode("utf8"), body, hashlib.sha256).digest()).decode()
	return hmac.compare_digest(expected, signature.strip())
