import unittest

from aitsgst.core.payload import build_push_plan
from aitsgst.core.reconcile import classify, reconcile
from aitsgst.tests import samples as s


def plan(si=None, cfg=s.COMPANY_CFG, **kw):
	return build_push_plan(si or s.invoice(), cfg, **s.plan_kwargs(**kw))


class TestPayload(unittest.TestCase):
	def test_clean_invoice(self):
		p = plan()
		self.assertTrue(p.is_valid, p.problems)
		pl = p.payload
		self.assertEqual(pl["company"], s.COMPANY_CFG["cloud_company"])
		self.assertEqual(pl["customer"], "Acme Builders")
		self.assertEqual(pl["sap_b1_key"], "COFFERS|101")
		self.assertEqual(pl["taxes_and_charges"], "Output GST Out-state - TTCC")  # mapped
		self.assertEqual(pl["place_of_supply"], "27-Maharashtra")
		self.assertEqual(pl["set_posting_time"], 1)
		self.assertEqual(pl["update_stock"], 0)
		self.assertEqual(pl["vehicle_no"], "MH12AB1234")
		self.assertNotIn("docstatus", pl)  # always a draft on cloud

		line = pl["items"][0]
		self.assertEqual(line["gst_hsn_code"], "320810")  # digits only
		self.assertEqual(line["item_tax_template"], "GST 18% - TTCC")
		self.assertEqual((line["rate"], line["price_list_rate"], line["discount_percentage"]), (100.0, 100.0, 0))

	def test_master_plans_and_keys(self):
		p = plan()
		self.assertEqual(p.customer["key"], "DEV|Customer|CUST-0001")
		self.assertEqual([a["role"] for a in p.addresses], ["customer_address", "shipping_address_name"])
		self.assertEqual(p.addresses[0]["key"], "DEV|Address|CUST-0001-Billing")
		item = p.items[0]
		self.assertEqual(item["key"], "DEV|Item|PAINT-01")
		self.assertEqual(item["uoms"], [{"uom": "Box", "conversion_factor": 12}])  # stock UOM excluded
		self.assertEqual(item["is_stock_item"], 0)
		self.assertEqual(p.uoms, ["Nos"])
		self.assertIn("320810", p.hsn_codes)

	def test_item_prefix(self):
		p = plan(cfg=dict(s.COMPANY_CFG, item_code_prefix="TT-"))
		self.assertEqual(p.payload["items"][0]["item_code"], "TT-PAINT-01")
		self.assertEqual(p.items[0]["cloud_code"], "TT-PAINT-01")

	def test_unmapped_template_uses_local_name(self):
		p = plan(template_map={})
		self.assertEqual(p.payload["taxes_and_charges"], "Output GST Out-state - TTC")

	def test_blocks_draft_and_unmapped_company(self):
		p = plan(s.invoice(docstatus=0), cfg=None)
		self.assertFalse(p.is_valid)
		self.assertTrue(any("submitted" in x for x in p.problems))
		self.assertTrue(any("not mapped" in x for x in p.problems))

	def test_blocks_gstin_mismatch(self):
		p = plan(s.invoice(company_gstin="27AAACT1234A1Z5"))
		self.assertTrue(any("does not match" in x for x in p.problems))

	def test_blocks_bad_customer_gstin_and_pos(self):
		p = plan(s.invoice(billing_address_gstin="27BAD", place_of_supply="Maharashtra"))
		self.assertTrue(any("Customer GSTIN" in x for x in p.problems))
		self.assertTrue(any("Place of supply" in x for x in p.problems))

	def test_unregistered_customer_warns(self):
		si = s.invoice(billing_address_gstin=None, gst_category="Unregistered")
		p = plan(si)
		self.assertTrue(p.is_valid, p.problems)
		self.assertTrue(any("no GSTIN" in x for x in p.warnings))
		self.assertNotIn("billing_address_gstin", p.payload)

	def test_line_problems(self):
		si = s.invoice()
		si["items"].append({"idx": 2, "item_code": "X", "qty": 0, "uom": None, "rate": 5})
		si["items"].append({"idx": 3, "item_code": None, "qty": 1})
		p = plan(si, item_masters={})
		self.assertTrue(any("Row 2" in x and "quantity" in x for x in p.problems))
		self.assertTrue(any("Row 2" in x and "unit of measure" in x for x in p.problems))
		self.assertTrue(any("Row 2" in x and "HSN" in x for x in p.problems))
		self.assertTrue(any("Row 3" in x and "item code" in x for x in p.problems))

	def test_taxes_without_template_blocked(self):
		p = plan(s.invoice(taxes_and_charges=None))
		self.assertTrue(any("no Sales Taxes and Charges Template" in x for x in p.problems))

	def test_credit_note_needs_pushed_original(self):
		si = s.invoice(is_return=1, return_against="ACC-SINV-2026-00000")
		self.assertTrue(any("Push that invoice first" in x for x in plan(si).problems))
		p = plan(si, return_against_cloud="SINV-CLOUD-9")
		self.assertTrue(p.is_valid, p.problems)
		self.assertEqual((p.payload["is_return"], p.payload["return_against"]), (1, "SINV-CLOUD-9"))

	def test_header_discount_copied(self):
		p = plan(s.invoice(discount_amount=50, apply_discount_on="Net Total", additional_discount_percentage=5))
		self.assertEqual((p.payload["discount_amount"], p.payload["apply_discount_on"]), (50, "Net Total"))

	def test_missing_billing_address_blocks_b2b(self):
		p = plan(billing_address=None)
		self.assertTrue(any("no billing address" in x for x in p.problems))


class TestReconcile(unittest.TestCase):
	def test_match(self):
		status, detail, total = reconcile(s.invoice(), {"grand_total": 1180.02, "taxes": [{"account_head": "IGST - X", "base_tax_amount": 180}]})
		self.assertEqual(status, "Match", detail)
		self.assertEqual(total, 1180.02)

	def test_mismatch_total_and_tax(self):
		cloud = {"grand_total": 1100, "taxes": [{"account_head": "Output Tax CGST - X", "base_tax_amount": 90},
		                                       {"account_head": "Output Tax SGST - X", "base_tax_amount": 90}]}
		status, detail, _ = reconcile(s.invoice(), cloud)
		self.assertEqual(status, "Mismatch")
		self.assertIn("Total", detail)
		self.assertIn("IGST: invoice 180.00 vs GST service 0.00", detail)
		self.assertIn("CGST: invoice 0.00 vs GST service 90.00", detail)

	def test_rounded_totals_accepted(self):
		local = s.invoice(grand_total=1179.6, rounded_total=1180)
		cloud = {"grand_total": 1179.5, "rounded_total": 1180, "taxes": [{"account_head": "IGST", "base_tax_amount": 180}]}
		self.assertEqual(reconcile(local, cloud)[0], "Match")

	def test_classify(self):
		self.assertEqual(classify("3300012 - IGST on Sales - TTCPL"), "IGST")
		self.assertEqual(classify("Output UTGST - X"), "SGST")
		self.assertEqual(classify("Freight", "Freight charges"), "Other")
