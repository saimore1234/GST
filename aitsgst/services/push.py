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
COMPANY_FIELD = "aitsgst_company"  # on the GST service's Customer / Item / Address: the client company owning it


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
		self._company_cfg = company_cfg
		self._resolve_templates(plan, company_cfg)
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
		renamed = {}
		for item in plan.items:
			code = self._ensure_item(item)
			if code != item["cloud_code"]:
				renamed[item["cloud_code"]] = code
		for line in plan.payload["items"]:
			line["item_code"] = renamed.get(line["item_code"], line["item_code"])

		company_address = self._company_address(company_cfg.get("company_gstin"))
		if company_address:
			plan.payload["company_address"] = company_address

		self._attach_taxes(plan)

	# ------------------------------------------- one GST service, several client companies
	# When the GST service's Customer / Item / Address have the field aitsgst_company (Link to Company), every
	# master is tagged with the client's company and only that company's masters are ever matched or changed:
	# clients sharing one GST service never touch each other's records, and User Permissions (Company = X)
	# on each client's API user hide the other clients' data. Without the field, masters are shared (one client).
	def _tag(self, doctype: str) -> str | None:
		if not hasattr(self, "_tag_cache"):
			self._tag_cache = {}
		if doctype not in self._tag_cache:
			try:
				self.client.get_list(doctype, [[COMPANY_FIELD, "=", "__aitsgst_probe__"]], ["name"], 1)
				present = True
			except CloudError as e:
				if e.status_code not in (400, 417):
					raise
				present = False  # "Field not permitted in query": the GST service has no company field
			self._tag_cache[doctype] = present
		return self._company_cfg.get("cloud_company") if self._tag_cache[doctype] else None

	def _is_ours(self, key: str | None) -> bool:
		"""An untagged record (created before the company field existed) is claimed only if it has no key or a
		key written by this site (or this client's SAP B1 Web Portal)."""
		if not key:
			return True
		own = {self.cfg.site_code, (self._company_cfg or {}).get("sap_company_code")}
		return key.split("|", 1)[0] in own - {None, ""}

	def _cloud_abbr(self, company_cfg=None) -> str:
		if not hasattr(self, "_abbr"):
			cfg = company_cfg or self._company_cfg
			self._abbr = (self.client.get_doc("Company", cfg.get("cloud_company")) or {}).get("abbr") or ""
		return self._abbr

	def _ensure_customer(self, c: dict) -> str:
		tag = self._tag("Customer")
		if tag:
			return self._ensure_customer_tagged(c, tag)
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

		return self._create_customer(c, tag=None)

	def _ensure_customer_tagged(self, c: dict, tag: str) -> str:
		fields = ["name", self.key_field, COMPANY_FIELD]
		by_key = self.client.get_list("Customer", [[self.key_field, "=", c["key"]]], fields, 1)
		if by_key:
			if not by_key[0].get(COMPANY_FIELD):
				self.client.update("Customer", by_key[0]["name"], {COMPANY_FIELD: tag})
			return by_key[0]["name"]

		mine = self.client.get_list("Customer", [["customer_name", "=", c["name"]], [COMPANY_FIELD, "=", tag]], fields, 2)
		if len(mine) > 1:
			raise PushProblem(f"Company {tag} has more than one GST service Customer named '{c['name']}'. Set the key field "
			                  f"({self.key_field}={c['key']}) on the right one and push again.")
		if mine:
			if not mine[0].get(self.key_field):
				self.client.update("Customer", mine[0]["name"], {self.key_field: c["key"]})
			return mine[0]["name"]

		untagged = [r for r in self.client.get_list("Customer", [["customer_name", "=", c["name"]], [COMPANY_FIELD, "is", "not set"]], fields, 5)
		            if self._is_ours(r.get(self.key_field))]
		if len(untagged) > 1:
			raise PushProblem(f"More than one untagged GST service Customer is named '{c['name']}'. Set {COMPANY_FIELD} = {tag} "
			                  "on the right one and push again.")
		if untagged:
			values = {COMPANY_FIELD: tag}
			if not untagged[0].get(self.key_field):
				values[self.key_field] = c["key"]
			self.client.update("Customer", untagged[0]["name"], values)
			return untagged[0]["name"]

		# Customers of other client companies are never touched: this company gets its own record
		# (ERPNext names it "<name> - 1" when the name is taken).
		return self._create_customer(c, tag)

	def _create_customer(self, c: dict, tag: str | None) -> str:
		if not c["create_if_missing"]:
			raise PushProblem(f"Customer '{c['name']}' does not exist in the GST service and 'Create missing Customers' is off.")
		doc = {
			"customer_name": c["name"], "customer_type": c["customer_type"], "customer_group": self._customer_group(c["customer_group"]),
			"territory": c["territory"], "gst_category": c["gst_category"], self.key_field: c["key"],
		}
		if c.get("gstin"):
			doc["gstin"] = c["gstin"]
		if tag:
			doc[COMPANY_FIELD] = tag
		return self.client.insert("Customer", doc)["name"]

	def _resolve_templates(self, plan, company_cfg):
		"""ERPNext names tax templates "<title> - <company abbr>", so the local "Output GST Out-state - CEII" is
		"Output GST Out-state - <GST service company abbr>" there. Names from the Tax Template Map are used
		as-is; any other name not found in the GST service is retried with the GST service company's abbr."""
		explicit = set(self.cfg.template_map.values())
		cache = {}

		def cloud_abbr():
			return self._cloud_abbr(company_cfg)

		def resolve(doctype, name):
			if not name or name in explicit:
				return name
			if (doctype, name) not in cache:
				result = name
				if " - " in name and self.client.get_doc(doctype, name) is None and cloud_abbr():
					candidate = f"{name.rsplit(' - ', 1)[0]} - {cloud_abbr()}"
					if candidate != name and self.client.get_doc(doctype, candidate) is not None:
						result = candidate
				cache[(doctype, name)] = result
			return cache[(doctype, name)]

		plan.cloud_tax_template = resolve(TAX_TEMPLATE, plan.cloud_tax_template)
		if plan.cloud_tax_template:
			plan.payload["taxes_and_charges"] = plan.cloud_tax_template
		for line in plan.payload["items"]:
			if line.get("item_tax_template"):
				line["item_tax_template"] = resolve(ITEM_TAX_TEMPLATE, line["item_tax_template"])
		for item in plan.items:
			if item.get("item_tax_template"):
				item["item_tax_template"] = resolve(ITEM_TAX_TEMPLATE, item["item_tax_template"])

	def _customer_group(self, configured: str | None) -> str:
		"""ERPNext rejects a group (tree folder) Customer Group such as "All Customer Groups". Use the
		configured one if it is a leaf, else the GST service's own default from Selling Settings."""
		doc = self.client.get_doc("Customer Group", configured) if configured else None
		if doc and not doc.get("is_group"):
			return configured
		default = (self.client.get_doc("Selling Settings", "Selling Settings") or {}).get("customer_group")
		default_doc = self.client.get_doc("Customer Group", default) if default else None
		if default_doc and not default_doc.get("is_group"):
			return default
		raise PushProblem(
			f"Customer Group '{configured}' is a group (or does not exist) in the GST service, and its Selling Settings "
			"has no non-group default Customer Group. Set a non-group Customer Group (e.g. Commercial) in "
			"AITS GST Settings > Companies > GST Service Customer Group."
		)

	def _ensure_address(self, a: dict, customer_name: str) -> str:
		found = self._find_by_key("Address", a["key"])
		if found:
			return found
		doc = {k: v for k, v in a["doc"].items() if v not in (None, "")}
		doc[self.key_field] = a["key"]
		doc["links"] = [{"link_doctype": "Customer", "link_name": customer_name}]
		tag = self._tag("Address")
		if tag:
			doc[COMPANY_FIELD] = tag
		return self.client.insert("Address", doc)["name"]

	def _ensure_item(self, i: dict) -> str:
		"""Returns the GST service item code used for this item."""
		tag = self._tag("Item")
		if tag:
			return self._ensure_item_tagged(i, tag)
		if self._find_by_key("Item", i["key"]):
			return i["cloud_code"]
		existing = self.client.get_doc("Item", i["cloud_code"])
		if existing:
			self._adopt("Item", existing, i["key"], f"Item '{i['cloud_code']}'")
			return i["cloud_code"]
		return self._create_item(i, i["cloud_code"], tag=None)

	def _ensure_item_tagged(self, i: dict, tag: str) -> str:
		by_key = self.client.get_list("Item", [[self.key_field, "=", i["key"]]], ["name", COMPANY_FIELD], 1)
		if by_key:
			if not by_key[0].get(COMPANY_FIELD):
				self.client.update("Item", by_key[0]["name"], {COMPANY_FIELD: tag})
			return by_key[0]["name"]

		# Item codes are unique across the whole GST service: if another client company already uses this
		# code, this company's item gets "<company abbr>-<code>".
		candidates = [i["cloud_code"]]
		abbr = self._cloud_abbr()
		if abbr and not i["cloud_code"].startswith(f"{abbr}-"):
			candidates.append(f"{abbr}-{i['cloud_code']}")
		for code in candidates:
			existing = self.client.get_doc("Item", code)
			if existing is None:
				return self._create_item(i, code, tag)
			owner, key = existing.get(COMPANY_FIELD), existing.get(self.key_field)
			if owner == tag or (not owner and self._is_ours(key)):
				values = {}
				if not owner:
					values[COMPANY_FIELD] = tag
				if not key:
					values[self.key_field] = i["key"]
				if values:
					self.client.update("Item", code, values)
				return code
		raise PushProblem(f"Item code '{i['cloud_code']}' is already used in the GST service by another company"
		                  + (f", and so is '{candidates[-1]}'" if len(candidates) > 1 else "")
		                  + ". Set an Item Code Prefix for this company in AITS GST Settings.")

	def _create_item(self, i: dict, code: str, tag: str | None) -> str:
		if not i["create_if_missing"]:
			raise PushProblem(f"Item '{code}' does not exist in the GST service and 'Create missing Items' is off.")

		stock_settings = self.client.get_doc("Stock Settings", "Stock Settings") or {}
		valuation_method = stock_settings.get("valuation_method")
		if not valuation_method:
			raise PushProblem("GST service Stock Settings has no default Valuation Method, which new Items require. Set one there and push again.")
		doc = {
			"item_code": code, "item_name": i["item_name"], "description": i["description"],
			"item_group": i["item_group"], "stock_uom": i["stock_uom"], "is_stock_item": i["is_stock_item"],
			"valuation_method": valuation_method, "gst_hsn_code": i["gst_hsn_code"], self.key_field: i["key"],
		}
		if tag:
			doc[COMPANY_FIELD] = tag
		if i["uoms"]:
			doc["uoms"] = [{"uom": i["stock_uom"], "conversion_factor": 1}, *i["uoms"]]
		if i.get("item_tax_template"):
			# The cloud only accepts an Item Tax Template on a line if the item (or its group) lists it.
			doc["taxes"] = [{"item_tax_template": i["item_tax_template"]}]
		return self.client.insert("Item", doc)["name"]

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
