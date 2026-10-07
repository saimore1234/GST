"""Plain-dict sample data shared by the unit tests (no database)."""

import copy

COMPANY = "Tint Tech Coatings Private Limited"
GSTIN = "24AAACT1234A1Z5"
CUSTOMER_GSTIN = "27AAPFU0939F1ZV"

COMPANY_CFG = {
	"company": COMPANY,
	"cloud_company": "Tint Tech Coatings Pvt Ltd (Cloud)",
	"company_gstin": GSTIN,
	"sap_company_code": "COFFERS",
	"auto_create_customer": 1,
	"auto_create_item": 1,
	"create_items_as_stock_items": 0,
	"item_code_prefix": "",
	"customer_group": "All Customer Groups",
	"territory": "All Territories",
	"item_group": "All Item Groups",
}

_INVOICE = {
	"name": "ACC-SINV-2026-00001",
	"docstatus": 1,
	"company": COMPANY,
	"customer": "CUST-0001",
	"posting_date": "2026-10-07",
	"posting_time": "10:15:00",
	"due_date": "2026-11-06",
	"currency": "INR",
	"conversion_rate": 1,
	"po_no": "PO-77",
	"remarks": "No Remarks",
	"company_gstin": GSTIN,
	"billing_address_gstin": CUSTOMER_GSTIN,
	"gst_category": "Registered Regular",
	"place_of_supply": "27-Maharashtra",
	"customer_address": "CUST-0001-Billing",
	"shipping_address_name": "CUST-0001-Shipping",
	"taxes_and_charges": "Output GST Out-state - TTC",
	"grand_total": 1180.0,
	"rounded_total": 1180.0,
	"vehicle_no": "mh 12-ab 1234",
	"items": [
		{"idx": 1, "item_code": "PAINT-01", "item_name": "Enamel Paint", "description": "Enamel Paint 1L",
		 "qty": 10, "uom": "Nos", "conversion_factor": 1, "rate": 100.0, "gst_hsn_code": "3208.10",
		 "item_tax_template": "GST 18% - TTC"},
	],
	"taxes": [
		{"account_head": "IGST - TTC", "description": "IGST", "base_tax_amount": 180.0, "tax_amount": 180.0},
	],
}

CUSTOMER = {"name": "CUST-0001", "customer_name": "Acme Builders", "customer_type": "Company",
            "gst_category": "Registered Regular", "gstin": CUSTOMER_GSTIN}

BILLING = {"name": "CUST-0001-Billing", "address_title": "Acme Builders", "address_type": "Billing",
           "address_line1": "1 MG Road", "city": "Pune", "state": "Maharashtra", "pincode": "411001",
           "country": "India", "gstin": CUSTOMER_GSTIN, "gst_category": "Registered Regular"}
SHIPPING = dict(BILLING, name="CUST-0001-Shipping", address_type="Shipping", address_line1="Plot 9, MIDC")

ITEM_MASTERS = {"PAINT-01": {"item_name": "Enamel Paint", "stock_uom": "Nos", "gst_hsn_code": "32081090",
                             "uoms": [{"uom": "Nos", "conversion_factor": 1}, {"uom": "Box", "conversion_factor": 12}]}}

TEMPLATE_MAP = {("Sales Taxes and Charges Template", "Output GST Out-state - TTC"): "Output GST Out-state - TTCC",
                ("Item Tax Template", "GST 18% - TTC"): "GST 18% - TTCC"}


def invoice(**overrides):
	doc = copy.deepcopy(_INVOICE)
	doc.update(overrides)
	return doc


def plan_kwargs(**overrides):
	kwargs = {
		"invoice_key": "COFFERS|101",
		"key_field": "sap_b1_key",
		"site_code": "DEV",
		"customer": copy.deepcopy(CUSTOMER),
		"billing_address": copy.deepcopy(BILLING),
		"shipping_address": copy.deepcopy(SHIPPING),
		"item_masters": copy.deepcopy(ITEM_MASTERS),
		"template_map": dict(TEMPLATE_MAP),
	}
	kwargs.update(overrides)
	return kwargs
