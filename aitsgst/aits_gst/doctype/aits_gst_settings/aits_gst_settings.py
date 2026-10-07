import re
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from aitsgst.core.gst import is_valid_gstin, normalize_gstin

_SITE_CODE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
_FIELDNAME = re.compile(r"^[a-z][a-z0-9_]{1,60}$")


def validate_cloud_url(url: str | None) -> str | None:
	"""Returns the normalised site root, or throws. HTTPS only; no path, query or credentials."""
	if not url or not url.strip():
		return None
	url = url.strip().rstrip("/")
	parsed = urlparse(url)
	if parsed.scheme != "https" or not parsed.hostname:
		frappe.throw(_("Cloud Site URL must start with https:// (plain http is not allowed)."))
	if parsed.path.strip("/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
		frappe.throw(_("Cloud Site URL must be the site root only, e.g. https://yoursite.m.erpnext.com"))
	return url


class AITSGSTSettings(Document):
	def onload(self):
		self.set_onload("local_ic_warning", get_local_ic_warning())

	def validate(self):
		self.cloud_url = validate_cloud_url(self.cloud_url)
		self.api_key = (self.api_key or "").strip() or None

		if not self.request_timeout or not 5 <= self.request_timeout <= 300:
			frappe.throw(_("Request Timeout must be between 5 and 300 seconds."))
		if not self.sync_interval_minutes or self.sync_interval_minutes < 5:
			frappe.throw(_("Status Sync Interval must be at least 5 minutes."))
		if self.max_push_retries is None or not 0 <= self.max_push_retries <= 10:
			frappe.throw(_("Max Push Retries must be between 0 and 10."))

		if not _FIELDNAME.match(self.cloud_key_field or ""):
			frappe.throw(_("Cloud Key Field must be a valid fieldname, e.g. sap_b1_key."))
		self._validate_site_code()
		self._validate_companies()

		if self.enabled and not (self.cloud_url and self.api_key and self.get_password("api_secret", raise_exception=False)):
			frappe.throw(_("Cloud Site URL, API Key and API Secret are required before enabling."))

	def _validate_site_code(self):
		code = (self.local_site_code or "").strip()
		if not _SITE_CODE.match(code):
			frappe.throw(_("Local Site Code may only contain letters, digits, '-' and '_' (max 20)."))
		self.local_site_code = code

		previous = self.get_doc_before_save()
		if previous and previous.local_site_code and previous.local_site_code != code:
			if frappe.db.exists("Sales Invoice", {"aitsgst_cloud_key": ["like", f"{previous.local_site_code}|%"]}):
				frappe.throw(_("Local Site Code cannot change: invoices were already pushed with keys using '{0}'.").format(previous.local_site_code))

	def _validate_companies(self):
		seen = set()
		for row in self.companies:
			if row.company in seen:
				frappe.throw(_("Row {0}: company {1} is mapped twice.").format(row.idx, row.company))
			seen.add(row.company)

			row.company_gstin = normalize_gstin(row.company_gstin)
			if not is_valid_gstin(row.company_gstin):
				frappe.throw(_("Row {0}: '{1}' is not a valid 15-character GSTIN.").format(row.idx, row.company_gstin or ""))
			row.cloud_company = (row.cloud_company or "").strip()
			row.sap_company_code = (row.sap_company_code or "").strip() or None
			if row.sap_company_code and "|" in row.sap_company_code:
				frappe.throw(_("Row {0}: SAP Company Code cannot contain '|'.").format(row.idx))

		seen_templates = set()
		for row in self.template_map:
			key = (row.template_type, row.local_template)
			if key in seen_templates:
				frappe.throw(_("Template Map row {0}: {1} is mapped twice.").format(row.idx, row.local_template))
			seen_templates.add(key)
			row.cloud_template = (row.cloud_template or "").strip()


def get_local_ic_warning() -> str | None:
	"""IRNs must only be generated on the cloud site. If this site's India Compliance can
	also reach the GST portal, the same invoice could be registered twice."""
	try:
		gst = frappe.get_cached_doc("GST Settings")
	except frappe.DoesNotExistError:
		return None

	if not gst.get("enable_api"):
		return None
	enabled = [label for field, label in (("enable_e_invoice", "e-Invoice"), ("enable_e_waybill", "e-Waybill")) if gst.get(field)]
	if not enabled:
		return None
	mode = "SANDBOX" if gst.get("sandbox_mode") else "PRODUCTION"
	return _(
		"This LOCAL site's India Compliance has the API enabled for {0} in {1} mode. "
		"IRNs and e-way bills must be generated only on the cloud site; otherwise the same invoice can be "
		"registered twice. Disable e-Invoice / e-Waybill in local GST Settings, or make sure no user generates them here."
	).format(" and ".join(enabled), mode)
