"""In-memory stand-ins for the cloud site, the local store and the sync log.

FakeCloud mimics the CloudClient surface (get_doc / get_list / insert / update /
call / call_get) closely enough to exercise every service branch, including
"ambiguous" failures where the cloud acted but the response was lost.
"""

import copy
import datetime
from collections import defaultdict
from contextlib import contextmanager
from types import SimpleNamespace

from aitsgst.core.cloud_client import CloudError
from aitsgst.services.context import Config
from aitsgst.tests import samples as s

NOW = datetime.datetime(2026, 10, 7, 12, 0, 0)


def timeout():
	return CloudError("Cloud ERPNext did not respond in time.", ambiguous=True)


def validation(msg="Validation failed"):
	return CloudError(f"Cloud ERPNext error (417): {msg}", 417)


class FakeCloud:
	def __init__(self):
		self.igst_rate = 18  # rate in the cloud's tax template (tests change it to force a mismatch)
		self.docs = defaultdict(dict)
		self.calls = []
		self.methods = {}
		self._failures = []
		self._seq = 0

	# ------------------------------------------------------------ test setup
	def add(self, doctype, **doc):
		doc.setdefault("docstatus", 0)
		self.docs[doctype][doc["name"]] = doc
		return doc

	def fail(self, op, target, error, after=False, times=1):
		"""Make the next `times` calls of op(target) raise. after=True: perform the call, then raise."""
		self._failures.append({"op": op, "target": target, "error": error, "after": after, "times": times})

	def count(self, op, target=None):
		return sum(1 for c in self.calls if c[0] == op and (target is None or c[1] == target))

	def _check(self, op, target):
		for f in self._failures:
			if f["op"] == op and f["target"] == target and f["times"] > 0:
				f["times"] -= 1
				return f
		return None

	def _run(self, op, target, action):
		self.calls.append((op, target))
		f = self._check(op, target)
		if f and not f["after"]:
			raise f["error"]
		result = action()
		if f and f["after"]:
			raise f["error"]
		return result

	# --------------------------------------------------------- client surface
	def get_doc(self, doctype, name):
		return self._run("get_doc", doctype, lambda: copy.deepcopy(self.docs[doctype].get(name)))

	def get_list(self, doctype, filters=None, fields=("name",), limit=20, order_by=None):
		def action():
			rows = [d for d in self.docs[doctype].values() if all(_match(d, f) for f in filters or [])]
			return [{f: d.get(f) for f in fields} for d in rows[:limit]]
		return self._run("get_list", doctype, action)

	def insert(self, doctype, doc):
		def action():
			self._seq += 1
			new = copy.deepcopy(doc)
			# ERPNext autonames these by a field rather than a series.
			name_field = {"Item": "item_code", "GST HSN Code": "hsn_code"}.get(doctype)
			if name_field:
				new.setdefault("name", doc[name_field])
			new.setdefault("name", f"{doctype.split()[0].upper()}-{self._seq:04d}")
			new.setdefault("docstatus", 0)
			if doctype == "Sales Invoice" and "grand_total" not in new:
				# Like ERPNext: net from lines, tax from the (cloud) template rates.
				net = sum((i.get("qty") or 0) * (i.get("rate") or 0) for i in new.get("items") or [])
				for t in new.get("taxes") or []:
					t["base_tax_amount"] = round(net * (t.get("rate") or 0) / 100, 2)
				new["grand_total"] = round(net + sum(t["base_tax_amount"] for t in new.get("taxes") or []), 2)
				new["rounded_total"] = round(new["grand_total"])
			self.docs[doctype][new["name"]] = new
			return copy.deepcopy(new)
		return self._run("insert", doctype, action)

	def update(self, doctype, name, values):
		def action():
			self.docs[doctype][name].update(copy.deepcopy(values))
			return copy.deepcopy(self.docs[doctype][name])
		return self._run("update", doctype, action)

	def call(self, method, **kwargs):
		return self._run("call", method, lambda: self.methods[method](**kwargs))

	def call_get(self, method, **params):
		return self._run("call_get", method, lambda: self.methods[method](**params))


def _match(doc, f):
	field, op, value = f
	actual = doc.get(field)
	if op == "=":
		return actual == value
	if op == "!=":
		return actual != value
	if op == "in":
		return actual in value
	if op == ">":
		return actual is not None and str(actual) > str(value)
	raise NotImplementedError(op)


class FakeStore:
	def __init__(self):
		self.invoices = {}
		self.docs = {}
		self.items = copy.deepcopy(s.ITEM_MASTERS)
		self.comments = []

	def add_invoice(self, **overrides):
		inv = s.invoice(**overrides)
		self.invoices[inv["name"]] = inv
		return inv

	def get_invoice(self, name):
		return copy.deepcopy(self.invoices[name])

	def get_doc_dict(self, doctype, name):
		return copy.deepcopy(self.docs.get((doctype, name)))

	def get_item_masters(self, codes):
		return {c: copy.deepcopy(self.items[c]) for c in codes if c in self.items}

	def cloud_invoice_of(self, name):
		return (self.invoices.get(name) or {}).get("aitsgst_cloud_invoice")

	def update_invoice(self, name, values, comment=None):
		self.invoices[name].update(values)
		if comment:
			self.comments.append((name, comment))

	@contextmanager
	def lock(self, name, action):
		yield

	# ---- local cancel
	local_problems = ()
	cancel_error = None
	cloud_cancelled_calls = ()

	def local_cancel_problems(self, name):
		return list(self.local_problems)

	def cancel_invoice(self, name):
		if self.cancel_error:
			raise self.cancel_error
		self.invoices[name]["docstatus"] = 2

	def on_cloud_cancelled(self, name):
		self.cloud_cancelled_calls = (*self.cloud_cancelled_calls, name)

	def user(self):
		return "tester@example.com"

	def now(self):
		return NOW


class FakeLog:
	def __init__(self):
		self.entries = []

	def write(self, action, status, name=None, message=None, **kw):
		self.entries.append({"action": action, "status": status, "name": name, "message": message, **kw})
		return f"LOG-{len(self.entries)}"

	def last(self):
		return self.entries[-1]


def make_cfg(**overrides):
	cfg = Config(
		enabled=True, cloud_url="https://cloud.example.com", key_field="sap_b1_key", site_code="DEV",
		sandbox_confirmed=True, allow_production=False, max_push_retries=3,
		companies={s.COMPANY: dict(s.COMPANY_CFG, enabled=1)}, template_map=dict(s.TEMPLATE_MAP),
	)
	for k, v in overrides.items():
		setattr(cfg, k, v)
	return cfg


def make_ctx(**cfg_overrides):
	store = FakeStore()
	store.docs[("Customer", "CUST-0001")] = copy.deepcopy(s.CUSTOMER)
	store.docs[("Address", "CUST-0001-Billing")] = copy.deepcopy(s.BILLING)
	store.docs[("Address", "CUST-0001-Shipping")] = copy.deepcopy(s.SHIPPING)
	return SimpleNamespace(cfg=make_cfg(**cfg_overrides), client=FakeCloud(), store=store, log=FakeLog())


def seed_cloud_setup(cloud: FakeCloud):
	"""What a correctly configured cloud site has before the first push."""
	cloud.add("UOM", name="Nos")
	cloud.add("UOM", name="Box")
	cloud.add("Stock Settings", name="Stock Settings", valuation_method="FIFO")
	cloud.add("Customer Group", name="All Customer Groups", is_group=1)
	cloud.add("Customer Group", name="Commercial", is_group=0)
	cloud.add("Selling Settings", name="Selling Settings", customer_group="Commercial", territory="India")
	cloud.add("Item Tax Template", name="GST 18% - TTCC", taxes=[{"tax_type": "Output Tax IGST - TTCC", "tax_rate": 18}])
	cloud.add("Sales Taxes and Charges Template", name="Output GST Out-state - TTCC")
	cloud.add("Address", name="TTC-Billing", gstin=s.GSTIN, is_your_company_address=1)
	cloud.methods["erpnext.controllers.accounts_controller.get_taxes_and_charges"] = lambda master_doctype, master_name: (
		[{"charge_type": "On Net Total", "account_head": "Output Tax IGST - TTCC", "rate": cloud.igst_rate, "description": "IGST"}]
		if master_name == "Output GST Out-state - TTCC" else None
	)
