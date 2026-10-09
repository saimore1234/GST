"""Local Sales Invoice -> draft GST service record Sales Invoice payload, plus every problem that
must block the push. Pure: takes plain dicts, does no I/O.

Adapted from the SAP B1 Web Portal's ErpNextInvoiceMapper. The local invoice
already carries GSTINs, place of supply, HSN and tax templates (India Compliance
set them locally), so this mostly validates and copies. Like the portal mapper it
deliberately calculates NO tax: the cloud computes tax from its own templates and
the result is reconciled against the local totals afterwards.
"""

from dataclasses import dataclass, field

from aitsgst.core.gst import hsn_digits, is_standard_hsn, is_valid_gstin, is_valid_place_of_supply, normalize_gstin

TAX_TEMPLATE = "Sales Taxes and Charges Template"
ITEM_TAX_TEMPLATE = "Item Tax Template"

# India Compliance transport fields copied when set on the local invoice.
TRANSPORT_FIELDS = ("transporter_name", "gst_transporter_id", "mode_of_transport", "gst_vehicle_type",
                    "vehicle_no", "lr_no", "lr_date", "distance")


@dataclass
class PushPlan:
	problems: list = field(default_factory=list)
	warnings: list = field(default_factory=list)
	payload: dict = field(default_factory=dict)
	customer: dict | None = None
	addresses: list = field(default_factory=list)  # [{"role": "customer_address"|"shipping_address_name", ...}]
	items: list = field(default_factory=list)
	uoms: list = field(default_factory=list)
	hsn_codes: dict = field(default_factory=dict)  # code -> description
	cloud_tax_template: str | None = None

	@property
	def is_valid(self) -> bool:
		return not self.problems


def master_key(site_code: str, doctype: str, name: str) -> str:
	return f"{site_code}|{doctype}|{name}"


def map_template(template_map: dict, template_type: str, local_name: str | None) -> str | None:
	if not local_name:
		return None
	return template_map.get((template_type, local_name)) or local_name


def build_push_plan(
	si: dict,
	company_cfg: dict | None,
	*,
	invoice_key: str,
	key_field: str,
	site_code: str,
	customer: dict | None,
	billing_address: dict | None,
	shipping_address: dict | None,
	item_masters: dict,
	template_map: dict | None = None,
	return_against_cloud: str | None = None,
) -> PushPlan:
	plan = PushPlan()
	p, w = plan.problems, plan.warnings
	template_map = template_map or {}
	cfg = company_cfg or {}

	# ---------------------------------------------------------- document state
	if si.get("docstatus") != 1:
		p.append("Only submitted Sales Invoices can be prepared for e-invoicing (this one is not submitted).")
	if not company_cfg:
		p.append(f"Company '{si.get('company')}' is not mapped (or is disabled) in AITS GST Settings.")
	items = si.get("items") or []
	if not items:
		p.append("Invoice has no items.")

	currency = si.get("currency") or "INR"
	if currency != "INR":
		w.append(f"Foreign-currency invoice ({currency}, rate {si.get('conversion_rate')}). Check export / SEZ e-invoice details in the GST service.")

	# ---------------------------------------------------- company GSTIN safety
	company_gstin = normalize_gstin(si.get("company_gstin"))
	if not is_valid_gstin(company_gstin):
		p.append(f"Company GSTIN on the invoice is missing or invalid ('{company_gstin or 'blank'}').")
	elif cfg and company_gstin != normalize_gstin(cfg.get("company_gstin")):
		p.append(f"Company GSTIN on this invoice ({company_gstin}) does not match the GSTIN mapped for this company "
		         f"({cfg.get('company_gstin')}). Push is blocked.")

	# ------------------------------------------------------- customer / GSTIN
	customer_gstin = normalize_gstin(si.get("billing_address_gstin"))
	registered = bool(customer_gstin)
	if registered and not is_valid_gstin(customer_gstin):
		p.append(f"Customer GSTIN '{customer_gstin}' is not a valid 15-character GSTIN.")
	if not registered:
		w.append("Customer has no GSTIN on the billing address - it will be sent as B2C; no IRN can be generated for it.")

	gst_category = si.get("gst_category") or ("Registered Regular" if registered else "Unregistered")
	place_of_supply = si.get("place_of_supply")
	if not is_valid_place_of_supply(place_of_supply):
		p.append(f"Place of supply '{place_of_supply or 'blank'}' is missing or not in India Compliance's 'NN-State' form.")

	if not customer:
		p.append(f"Customer '{si.get('customer')}' was not found locally.")
	else:
		plan.customer = {
			"name": customer.get("customer_name") or customer.get("name"),
			"key": master_key(site_code, "Customer", customer["name"]),
			"customer_type": customer.get("customer_type") or "Company",
			"gst_category": customer.get("gst_category") or gst_category,
			"gstin": normalize_gstin(customer.get("gstin")) or customer_gstin,
			"customer_group": cfg.get("customer_group") or "All Customer Groups",
			"local_customer_group": customer.get("customer_group"),
			"territory": cfg.get("territory") or "All Territories",
			"create_if_missing": bool(cfg.get("auto_create_customer")),
		}

	# --------------------------------------------------------------- addresses
	if not billing_address:
		(p if registered else w).append(
			"Invoice has no billing address (customer_address). " + ("India Compliance needs it for the e-invoice." if registered else "")
		)
	for role, addr in (("customer_address", billing_address), ("shipping_address_name", shipping_address)):
		if not addr:
			continue
		if not addr.get("address_line1"):
			p.append(f"Address '{addr.get('name')}' has no address line 1.")
		plan.addresses.append({
			"role": role,
			"key": master_key(site_code, "Address", addr["name"]),
			"doc": {
				"address_title": addr.get("address_title") or (plan.customer or {}).get("name"),
				"address_type": addr.get("address_type") or ("Billing" if role == "customer_address" else "Shipping"),
				"address_line1": addr.get("address_line1"),
				"address_line2": addr.get("address_line2"),
				"city": addr.get("city"),
				"state": addr.get("state"),
				"pincode": addr.get("pincode"),
				"country": addr.get("country") or "India",
				"gstin": normalize_gstin(addr.get("gstin")),
				"gst_category": addr.get("gst_category") or gst_category,
				"is_primary_address": 1 if role == "customer_address" else 0,
				"is_shipping_address": 1 if role == "shipping_address_name" else 0,
			},
		})
	if not shipping_address and billing_address:
		w.append("No shipping address; the billing address will be used.")

	# ------------------------------------------------------------ tax template
	taxes = si.get("taxes") or []
	local_template = si.get("taxes_and_charges")
	plan.cloud_tax_template = map_template(template_map, TAX_TEMPLATE, local_template)
	if taxes and not local_template:
		p.append("The invoice has tax rows but no Sales Taxes and Charges Template. Tax rows are copied from "
		         "its own template, so set the template on the local invoice.")
	if not taxes:
		w.append("The invoice has no tax rows.")

	# ------------------------------------------------------------------ lines
	prefix = cfg.get("item_code_prefix") or ""
	lines, seen_items, uoms = [], set(), []
	for row in items:
		label = f"Row {row.get('idx')} ({row.get('item_code') or 'no item'})"
		code = row.get("item_code")
		if not code:
			p.append(f"{label}: no item code.")
			continue
		if not row.get("qty"):
			p.append(f"{label}: quantity is zero.")
		uom = row.get("uom")
		if not uom:
			p.append(f"{label}: no unit of measure.")
		elif uom not in uoms:
			uoms.append(uom)

		master = item_masters.get(code) or {}
		hsn = hsn_digits(row.get("gst_hsn_code") or master.get("gst_hsn_code"))
		if not hsn:
			p.append(f"{label}: no HSN/SAC code.")
		elif not is_standard_hsn(hsn):
			w.append(f"{label}: HSN/SAC '{hsn}' is not 4, 6 or 8 digits.")
		else:
			plan.hsn_codes.setdefault(hsn, (row.get("item_name") or code)[:140])

		cloud_item_tax = map_template(template_map, ITEM_TAX_TEMPLATE, row.get("item_tax_template"))
		cloud_code = prefix + code
		rate = row.get("rate") or 0
		lines.append({
			"item_code": cloud_code,
			"item_name": (row.get("item_name") or code)[:140],
			"description": row.get("description") or row.get("item_name") or code,
			"qty": row.get("qty"),
			"uom": uom,
			"conversion_factor": row.get("conversion_factor") or 1,
			# The local rate is already after any line discount: send it as the list price too, so the
			# cloud cannot re-derive a different rate from its own price list.
			"price_list_rate": rate,
			"discount_percentage": 0,
			"rate": rate,
			"gst_hsn_code": hsn,
			"item_tax_template": cloud_item_tax,
		})

		if code not in seen_items:
			seen_items.add(code)
			stock_uom = master.get("stock_uom") or uom
			if stock_uom and stock_uom not in uoms:
				uoms.append(stock_uom)
			plan.items.append({
				"local_code": code,
				"cloud_code": cloud_code,
				"key": master_key(site_code, "Item", code),
				"item_name": (master.get("item_name") or row.get("item_name") or code)[:140],
				"description": master.get("description") or row.get("description") or code,
				"stock_uom": stock_uom,
				"uoms": [
					{"uom": u.get("uom"), "conversion_factor": u.get("conversion_factor")}
					for u in master.get("uoms") or [] if u.get("uom") and u.get("uom") != stock_uom
				],
				"gst_hsn_code": hsn,
				"item_tax_template": cloud_item_tax,
				"create_if_missing": bool(cfg.get("auto_create_item")),
				"is_stock_item": 1 if cfg.get("create_items_as_stock_items") else 0,
				"item_group": cfg.get("item_group") or "All Item Groups",
			})
	plan.uoms = uoms

	# ---------------------------------------------------------------- returns
	if si.get("is_return"):
		if not si.get("return_against"):
			p.append("Credit note has no 'Return Against' invoice.")
		elif not return_against_cloud:
			p.append(f"Credit note is against {si.get('return_against')}, which has not been prepared for e-invoicing yet. Push that invoice first.")

	# ---------------------------------------------------------------- payload
	remarks = f"Local ERPNext {si.get('name')}."
	if si.get("remarks") and si.get("remarks") != "No Remarks":
		remarks += " " + str(si["remarks"]).strip()

	payload = {
		"doctype": "Sales Invoice",
		"company": cfg.get("cloud_company"),
		"customer": (plan.customer or {}).get("name"),
		"posting_date": str(si.get("posting_date")),
		"posting_time": str(si.get("posting_time")) if si.get("posting_time") else None,
		"set_posting_time": 1,
		"due_date": str(si.get("due_date") or si.get("posting_date")),
		"currency": currency,
		"conversion_rate": si.get("conversion_rate") or 1,
		"ignore_pricing_rule": 1,
		"update_stock": 0,
		"po_no": si.get("po_no"),
		"po_date": str(si["po_date"]) if si.get("po_date") else None,
		"remarks": remarks[:1000],
		key_field: invoice_key,
		"gst_category": gst_category,
		"place_of_supply": place_of_supply,
		"company_gstin": company_gstin,
		"billing_address_gstin": customer_gstin,
		"is_reverse_charge": si.get("is_reverse_charge") or 0,
		"is_export_with_gst": si.get("is_export_with_gst") or 0,
		"taxes_and_charges": plan.cloud_tax_template,
		"disable_rounded_total": si.get("disable_rounded_total") or 0,
		"items": lines,
	}
	if si.get("sap_b1_docnum"):
		payload["sap_b1_docnum"] = str(si["sap_b1_docnum"])
	if si.get("discount_amount"):
		payload["apply_discount_on"] = si.get("apply_discount_on") or "Grand Total"
		payload["discount_amount"] = si.get("discount_amount")
		if si.get("additional_discount_percentage"):
			payload["additional_discount_percentage"] = si.get("additional_discount_percentage")
	if si.get("is_return"):
		payload["is_return"] = 1
		payload["return_against"] = return_against_cloud
	for f in TRANSPORT_FIELDS:
		if si.get(f):
			payload[f] = str(si[f]) if f == "lr_date" else si[f]
	if payload.get("vehicle_no"):
		payload["vehicle_no"] = str(payload["vehicle_no"]).replace(" ", "").replace("-", "").upper()

	plan.payload = {k: v for k, v in payload.items() if v is not None}
	return plan
