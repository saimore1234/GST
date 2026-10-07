"""Push a local Sales Invoice to the cloud as a DRAFT. Never submits.

Order (from the SAP B1 Web Portal's ErpNextInvoiceService.Push), and why:
  1. Validate with build_push_plan - a blocked invoice sends nothing.
  2. Lock the invoice so two simultaneous pushes cannot both create one.
  3. Look the invoice up in the GST service by its key BEFORE creating - covers an earlier
     attempt that timed out after the cloud had already saved it, and invoices
     the SAP B1 Web Portal pushed directly with the same key.
  4. Find-or-create Customer, Addresses, Items (keyed), copy tax rows from the
     cloud's own templates, insert the draft.
  5. Read back the cloud totals, reconcile with the local invoice, record.
"""

import json
from datetime import timedelta

from aitsgst.core.cloud_client import CloudError
from aitsgst.core.payload import ITEM_TAX_TEMPLATE, TAX_TEMPLATE, build_push_plan
from aitsgst.core.reconcile import reconcile

SI = "Sales Invoice"
RETRY_BASE_MINUTES = 5


class PushProblem(Exception):
	"""A setup/data problem on the cloud side that retrying will not fix."""


def docstatus_text(docstatus) -> str | None:
	return {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(docstatus)


def invoice_key(si: dict, company_cfg: dict | None, site_code: str) -> str:
	"""Invoices that came from SAP B1 get the SAP B1 Web Portal's key ("{code}|{DocEntry}"),
	so one the portal already pushed is adopted rather than duplicated."""
	code = (company_cfg or {}).get("sap_company_code")
	docentry = si.get("sap_b1_docentry")
	if code and docentry:
		return f"{code}|{str(docentry).strip()}"
	return f"{site_code}|{si['name']}"


def retry_delay(retry_count: int) -> timedelta:
	return timedelta(minutes=RETRY_BASE_MINUTES * (2 ** retry_count))


class PushService:
	def __init__(self, ctx):
		self.cfg, self.client, self.store, self.log = ctx.cfg, ctx.client, ctx.store, ctx.log
		self.key_field = self.cfg.key_field

	# ------------------------------------------------------------------- push
	def push(self, name: str, retry_count: int = 0) -> dict:
		with self.store.lock(name, "push"):
			return self._push(name, retry_count)

	def _push(self, name: str, retry_count: int) -> dict:
		si = self.store.get_invoice(name)
		company_cfg = self.cfg.company(si.get("company"))

		if si.get("aitsgst_cloud_invoice") and si.get("aitsgst_push_status") == "Ready":
			return self._already_pushed(si)

		key = invoice_key(si, company_cfg, self.cfg.site_code)
		return_against_cloud = (
			self.store.cloud_invoice_of(si["return_against"]) if si.get("is_return") and si.get("return_against") else None
		)
		plan = build_push_plan(
			si, company_cfg,
			invoice_key=key, key_field=self.key_field, site_code=self.cfg.site_code,
			customer=self.store.get_doc_dict("Customer", si.get("customer")),
			billing_address=self.store.get_doc_dict("Address", si.get("customer_address")),
			shipping_address=self.store.get_doc_dict("Address", si.get("shipping_address_name")),
			item_masters=self.store.get_item_masters(r.get("item_code") for r in si.get("items") or []),
			template_map=self.cfg.template_map,
			return_against_cloud=return_against_cloud,
		)
		if not plan.is_valid:
			return self._blocked(si, plan.problems, plan.warnings)

		try:
			existing = self._find_by_key(SI, key, [["docstatus", "!=", 2]])
			if existing:
				return self._record(si, key, existing, "Adopted", plan.payload)

			self._prepare(plan, company_cfg)
			created = self.client.insert(SI, plan.payload)
			return self._record(si, key, created["name"], "Created", plan.payload, created)
		except PushProblem as e:
			return self._blocked(si, [str(e)], plan.warnings, request=plan.payload)
		except CloudError as e:
			if e.ambiguous:
				# The cloud may have saved the invoice before the connection dropped: look before failing.
				saved = self._try_find_by_key(SI, key, [["docstatus", "!=", 2]])
				if saved:
					return self._record(si, key, saved, "Adopted", plan.payload)
			return self._failed(si, key, e, retry_count, plan.payload)

	# --------------------------------------------------------- prerequisites
	def _prepare(self, plan, company_cfg):
		for uom in plan.uoms:
			if self.client.get_doc("UOM", uom) is None:
				raise PushProblem(f"Unit of measure '{uom}' does not exist in the GST service. Create it there and push again.")
		for code, description in plan.hsn_codes.items():
			if self.client.get_doc("GST HSN Code", code) is None:
				self.client.insert("GST HSN Code", {"hsn_code": code, "description": description})

		customer_name = self._ensure_customer(plan.customer)
		plan.payload["customer"] = customer_name
		for addr in plan.addresses:
			plan.payload[addr["role"]] = self._ensure_address(addr, customer_name)
		for item in plan.items:
			self._ensure_item(item)

		company_address = self._company_address(company_cfg.get("company_gstin"))
		if company_address:
			plan.payload["company_address"] = company_address

		self._attach_taxes(plan)

	def _ensure_customer(self, c: dict) -> str:
		found = self._find_by_key("Customer", c["key"])
		if found:
			return found

		existing = self.client.get_doc("Customer", c["name"])
		if existing is None:
			matches = self.client.get_list("Customer", [["customer_name", "=", c["name"]]], ["name", self.key_field], 2)
			if len(matches) > 1:
				raise PushProblem(f"More than one GST service Customer is named '{c['name']}'. Set the key field "
				                  f"({self.key_field}={c['key']}) on the right one and push again.")
			existing = matches[0] if matches else None
		if existing:
			self._adopt("Customer", existing, c["key"], f"Customer '{c['name']}'")
			return existing["name"]

		if not c["create_if_missing"]:
			raise PushProblem(f"Customer '{c['name']}' does not exist in the GST service and 'Create missing Customers' is off.")
		doc = {
			"customer_name": c["name"], "customer_type": c["customer_type"], "customer_group": c["customer_group"],
			"territory": c["territory"], "gst_category": c["gst_category"], self.key_field: c["key"],
		}
		if c.get("gstin"):
			doc["gstin"] = c["gstin"]
		return self.client.insert("Customer", doc)["name"]

	def _ensure_address(self, a: dict, customer_name: str) -> str:
		found = self._find_by_key("Address", a["key"])
		if found:
			return found
		doc = {k: v for k, v in a["doc"].items() if v not in (None, "")}
		doc[self.key_field] = a["key"]
		doc["links"] = [{"link_doctype": "Customer", "link_name": customer_name}]
		return self.client.insert("Address", doc)["name"]

	def _ensure_item(self, i: dict):
		if self._find_by_key("Item", i["key"]):
			return
		existing = self.client.get_doc("Item", i["cloud_code"])
		if existing:
			self._adopt("Item", existing, i["key"], f"Item '{i['cloud_code']}'")
			return
		if not i["create_if_missing"]:
			raise PushProblem(f"Item '{i['cloud_code']}' does not exist in the GST service and 'Create missing Items' is off.")

		stock_settings = self.client.get_doc("Stock Settings", "Stock Settings") or {}
		valuation_method = stock_settings.get("valuation_method")
		if not valuation_method:
			raise PushProblem("GST service Stock Settings has no default Valuation Method, which new Items require. Set one there and push again.")
		doc = {
			"item_code": i["cloud_code"], "item_name": i["item_name"], "description": i["description"],
			"item_group": i["item_group"], "stock_uom": i["stock_uom"], "is_stock_item": i["is_stock_item"],
			"valuation_method": valuation_method, "gst_hsn_code": i["gst_hsn_code"], self.key_field: i["key"],
		}
		if i["uoms"]:
			doc["uoms"] = [{"uom": i["stock_uom"], "conversion_factor": 1}, *i["uoms"]]
		if i.get("item_tax_template"):
			# The cloud only accepts an Item Tax Template on a line if the item (or its group) lists it.
			doc["taxes"] = [{"item_tax_template": i["item_tax_template"]}]
		self.client.insert("Item", doc)

	def _adopt(self, doctype: str, existing: dict, key: str, label: str):
		current = existing.get(self.key_field)
		if not current:
			self.client.update(doctype, existing["name"], {self.key_field: key})
		elif current != key:
			raise PushProblem(f"{label} already exists in the GST service but is linked to a different record ({current}).")

	def _company_address(self, gstin: str | None) -> str | None:
		if not gstin:
			return None
		rows = self.client.get_list("Address", [["gstin", "=", gstin], ["is_your_company_address", "=", 1]], ["name"], 1)
		return rows[0]["name"] if rows else None

	def _attach_taxes(self, plan):
		"""Over the API the cloud does not expand templates into rows, so copy rows from the
		cloud's OWN templates (its own accounts) - no tax is calculated here."""
		if plan.cloud_tax_template:
			rows = self.client.call_get(
				"erpnext.controllers.accounts_controller.get_taxes_and_charges",
				master_doctype=TAX_TEMPLATE, master_name=plan.cloud_tax_template,
			) or []
			if not rows:
				raise PushProblem(f"Tax template '{plan.cloud_tax_template}' was not found in the GST service, or has no tax rows. "
				                  "Add it to the Tax Template Map in AITS GST Settings.")
			plan.payload["taxes"] = rows

		rate_cache = {}
		for line in plan.payload["items"]:
			template = line.get("item_tax_template")
			if not template:
				continue
			if template not in rate_cache:
				doc = self.client.get_doc(ITEM_TAX_TEMPLATE, template)
				if doc is None:
					raise PushProblem(f"Item Tax Template '{template}' was not found in the GST service. Map it in AITS GST Settings.")
				rate_cache[template] = json.dumps({t["tax_type"]: t.get("tax_rate") for t in doc.get("taxes") or [] if t.get("tax_type")})
			line["item_tax_rate"] = rate_cache[template]

	# ---------------------------------------------------------------- lookups
	def _find_by_key(self, doctype: str, key: str, extra=None) -> str | None:
		rows = self.client.get_list(doctype, [[self.key_field, "=", key], *(extra or [])], ["name"], 1)
		return rows[0]["name"] if rows else None

	def _try_find_by_key(self, doctype, key, extra=None):
		try:
			return self._find_by_key(doctype, key, extra)
		except CloudError:
			return None

	# ---------------------------------------------------------------- results
	def _already_pushed(self, si: dict) -> dict:
		cloud_name = si["aitsgst_cloud_invoice"]
		doc = self.client.get_doc(SI, cloud_name)
		if doc is None:
			msg = ("The e-invoice record of this invoice no longer exists in the GST service. Nothing was created. "
			       "Ask your administrator to check; the link is kept so no duplicate is created by accident.")
			self.store.update_invoice(si["name"], {"aitsgst_last_error": msg})
			self.log.write("Push", "Blocked", si["name"], msg, company=si.get("company"), cloud_invoice=cloud_name)
			return {"outcome": "Blocked", "problems": [msg]}
		self.store.update_invoice(si["name"], {
			"aitsgst_cloud_docstatus": docstatus_text(doc.get("docstatus")),
			"aitsgst_last_synced": self.store.now(),
		})
		return {"outcome": "AlreadyReady", "cloud_invoice": cloud_name}

	def _record(self, si, key, cloud_name, outcome, payload, cloud_doc=None) -> dict:
		if not cloud_doc or "grand_total" not in cloud_doc:
			cloud_doc = self.client.get_doc(SI, cloud_name) or {}
		status, detail, cloud_total = reconcile(si, cloud_doc)
		self.store.update_invoice(si["name"], {
			"aitsgst_push_status": "Ready",
			"aitsgst_cloud_invoice": cloud_name,
			"aitsgst_cloud_key": key,
			"aitsgst_cloud_docstatus": docstatus_text(cloud_doc.get("docstatus")),
			"aitsgst_cloud_grand_total": cloud_total,
			"aitsgst_recon_status": status,
			"aitsgst_recon_detail": detail,
			"aitsgst_last_error": None,
			"aitsgst_last_synced": self.store.now(),
		}, comment=f"Prepared for e-invoicing; totals check: {status}.")
		self.log.write("Push", "Success", si["name"], f"{outcome} {cloud_name}; reconciliation {status}. {detail}",
		               company=si.get("company"), cloud_invoice=cloud_name, request=payload,
		               response={"name": cloud_name, "docstatus": cloud_doc.get("docstatus"), "grand_total": cloud_doc.get("grand_total")})
		return {"outcome": outcome, "cloud_invoice": cloud_name, "recon_status": status, "recon_detail": detail}

	def _blocked(self, si, problems, warnings=(), request=None) -> dict:
		message = "\n".join(problems)
		self.store.update_invoice(si["name"], {"aitsgst_push_status": "Blocked", "aitsgst_last_error": message})
		self.log.write("Push", "Blocked", si["name"], message + ("\nWarnings: " + "; ".join(warnings) if warnings else ""),
		               company=si.get("company"), request=request)
		return {"outcome": "Blocked", "problems": list(problems), "warnings": list(warnings)}

	def _failed(self, si, key, error: CloudError, retry_count: int, payload) -> dict:
		self.store.update_invoice(si["name"], {"aitsgst_push_status": "Failed", "aitsgst_last_error": error.message})
		if error.transient and retry_count < self.cfg.max_push_retries:
			next_at = self.store.now() + retry_delay(retry_count)
			self.log.write("Push", "Retry Scheduled", si["name"], error.message, company=si.get("company"),
			               request=payload, retry_count=retry_count, next_retry_at=next_at)
			return {"outcome": "Failed", "error": error.message, "retry_at": str(next_at)}
		status = "Gave Up" if error.transient else "Failed"
		self.log.write("Push", status, si["name"], error.message, company=si.get("company"), request=payload, retry_count=retry_count)
		return {"outcome": "Failed", "error": error.message}
