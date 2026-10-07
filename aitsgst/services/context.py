"""Real (Frappe-backed) implementations of the settings, local store and log that
the services use, plus `get_context()` which assembles them with the cloud client."""

from contextlib import contextmanager
from dataclasses import dataclass, field

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from aitsgst.core.cloud_client import CloudClient
from aitsgst.core.masking import mask_text, to_log_json


def _commit():
	# Never commit while tests run: a commit there would persist test fixtures into the real site.
	if not frappe.flags.in_test:
		frappe.db.commit()


class GateClosed(frappe.ValidationError):
	"""Cloud calls are disabled until the user confirms sandbox (or production)."""


@dataclass
class Config:
	enabled: bool
	cloud_url: str | None
	key_field: str
	site_code: str
	sandbox_confirmed: bool
	allow_production: bool
	max_push_retries: int
	companies: dict = field(default_factory=dict)  # local company -> row dict
	template_map: dict = field(default_factory=dict)

	def company(self, name: str) -> dict | None:
		row = self.companies.get(name)
		return row if row and row.get("enabled") else None

	@property
	def gate_open(self) -> bool:
		return self.sandbox_confirmed or self.allow_production


def load_config() -> Config:
	s = frappe.get_cached_doc("AITS GST Settings")
	return Config(
		enabled=bool(s.enabled),
		cloud_url=s.cloud_url,
		key_field=s.cloud_key_field or "sap_b1_key",
		site_code=s.local_site_code,
		sandbox_confirmed=bool(s.sandbox_confirmed),
		allow_production=bool(s.allow_production),
		max_push_retries=s.max_push_retries or 0,
		companies={r.company: r.as_dict() for r in s.companies},
		template_map={(r.template_type, r.local_template): r.cloud_template for r in s.template_map},
	)


def build_client(cfg: Config) -> CloudClient:
	if not cfg.enabled:
		frappe.throw(_("AITS GST is disabled. Enable it in AITS GST Settings."), GateClosed)
	if not cfg.gate_open:
		frappe.throw(
			_("No calls are made to the GST service until 'I confirm the GST service is a TEST / SANDBOX setup' "
			  "(or 'Allow production') is checked in AITS GST Settings."),
			GateClosed,
		)
	s = frappe.get_cached_doc("AITS GST Settings")
	return CloudClient(s.cloud_url, s.api_key, s.get_password("api_secret"), timeout=s.request_timeout or 60)


class LocalStore:
	"""Reads/writes the local Sales Invoice. Writes use db_set-style updates: the
	aitsgst_* fields are allow_on_submit + read-only, and the invoice itself is
	never re-validated or re-submitted by this app."""

	def get_invoice(self, name: str) -> dict:
		return frappe.get_doc("Sales Invoice", name).as_dict(convert_dates_to_str=True)

	def get_doc_dict(self, doctype: str, name: str | None) -> dict | None:
		if not name or not frappe.db.exists(doctype, name):
			return None
		return frappe.get_doc(doctype, name).as_dict(convert_dates_to_str=True)

	def get_item_masters(self, codes) -> dict:
		masters = {}
		for code in set(codes):
			if code and frappe.db.exists("Item", code):
				masters[code] = frappe.get_doc("Item", code).as_dict()
		return masters

	def cloud_invoice_of(self, local_name: str) -> str | None:
		return frappe.db.get_value("Sales Invoice", local_name, "aitsgst_cloud_invoice")

	def update_invoice(self, name: str, values: dict, comment: str | None = None):
		qr = values.get("aitsgst_signed_qr_code")
		if qr:
			current = frappe.db.get_value("Sales Invoice", name, ["aitsgst_signed_qr_code", "aitsgst_qr_image"], as_dict=True) or {}
			if qr != current.get("aitsgst_signed_qr_code") or not current.get("aitsgst_qr_image"):
				from aitsgst.utils.qr import save_qr_image

				values = {**values, "aitsgst_qr_image": save_qr_image(name, qr)}
		frappe.db.set_value("Sales Invoice", name, values, update_modified=False)
		if comment:
			frappe.get_doc("Sales Invoice", name).add_comment("Info", comment)
		_commit()  # results from the cloud must survive a later error in the same request

	@contextmanager
	def lock(self, name: str, action: str):
		"""One push / one compliance action per invoice at a time (a double click cannot send two)."""
		try:
			with filelock(f"aitsgst-{action}-{name}".replace("/", "_"), timeout=2):
				yield
		except LockTimeoutError:
			frappe.throw(_("Another {0} of {1} is already running. Wait a moment and refresh.").format(action, name))

	def local_cancel_problems(self, name: str) -> list:
		"""What would stop the local cancel - checked BEFORE anything is cancelled in the GST service."""
		from frappe.desk.form.linked_with import get_submitted_linked_docs

		from aitsgst.services.compliance import IGNORED_LINKED_DOCTYPES

		problems = []
		if not frappe.has_permission("Sales Invoice", "cancel", name):
			problems.append(_("You do not have permission to cancel {0} here.").format(name))
		linked = [d for d in (get_submitted_linked_docs("Sales Invoice", name) or {}).get("docs") or []
		          if d.get("doctype") not in IGNORED_LINKED_DOCTYPES]
		if linked:
			listed = ", ".join(f"{d.get('doctype')} {d.get('name')}" for d in linked[:5])
			problems.append(_("Locally, {0} has submitted documents linked to it ({1}). Cancel those first.").format(name, listed))
		return problems

	def cancel_invoice(self, name: str):
		doc = frappe.get_doc("Sales Invoice", name)
		doc.flags.aitsgst_cancel_everywhere = True  # lets events.before_cancel through
		doc.cancel()
		_commit()

	def on_cloud_cancelled(self, name: str):
		if frappe.db.get_single_value("AITS GST Settings", "auto_cancel_local_on_cloud_cancel"):
			frappe.enqueue("aitsgst.tasks.cancel_local_after_cloud", queue="short", enqueue_after_commit=True,
			               job_id=f"aitsgst-cancel-{name}", deduplicate=True, name=name)
		else:
			notify_managers(name, _("Sales Invoice {0} was cancelled in the GST service. Cancel it here too.").format(name))

	def user(self) -> str:
		return frappe.session.user

	def now(self):
		return now_datetime()


def notify_managers(name: str, subject: str):
	"""Bell notification for every enabled AITS GST Manager, plus a timeline comment."""
	users = frappe.get_all("Has Role", filters={"role": "AITS GST Manager", "parenttype": "User"}, pluck="parent")
	for user in set(users):
		if user in ("Administrator", "Guest") or not frappe.db.get_value("User", user, "enabled"):
			continue
		frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert", "subject": subject,
		                "document_type": "Sales Invoice", "document_name": name}).insert(ignore_permissions=True)
	frappe.get_doc("Sales Invoice", name).add_comment("Info", subject)
	_commit()


class SyncLog:
	def __init__(self, secrets=()):
		self._secrets = tuple(s for s in secrets if s)

	def write(self, action: str, status: str, name: str | None = None, message: str | None = None, *,
	          company=None, cloud_invoice=None, request=None, response=None, retry_count=0, next_retry_at=None) -> str:
		doc = frappe.get_doc({
			"doctype": "AITS GST Sync Log",
			"action": action,
			"status": status,
			"reference_doctype": "Sales Invoice" if name else None,
			"reference_name": name,
			"company": company,
			"cloud_invoice": cloud_invoice,
			"user": frappe.session.user,
			"message": mask_text(message, *self._secrets),
			"request": mask_text(to_log_json(request), *self._secrets),
			"response": mask_text(to_log_json(response), *self._secrets),
			"retry_count": retry_count,
			"next_retry_at": next_retry_at,
		})
		doc.insert(ignore_permissions=True)
		_commit()
		return doc.name


@dataclass
class Context:
	cfg: Config
	client: CloudClient
	store: LocalStore
	log: SyncLog


def get_context() -> Context:
	cfg = load_config()
	client = build_client(cfg)
	s = frappe.get_cached_doc("AITS GST Settings")
	return Context(cfg=cfg, client=client, store=LocalStore(), log=SyncLog(secrets=(s.api_key, s.get_password("api_secret", raise_exception=False))))
