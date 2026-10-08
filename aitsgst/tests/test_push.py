import json
import unittest

from aitsgst.services.push import PushService, invoice_key, retry_delay
from aitsgst.tests import fakes
from aitsgst.tests import samples as s

NAME = "ACC-SINV-2026-00001"


class TestPush(unittest.TestCase):
	def setUp(self):
		self.ctx = fakes.make_ctx()
		self.cloud, self.store, self.log = self.ctx.client, self.ctx.store, self.ctx.log
		fakes.seed_cloud_setup(self.cloud)
		self.store.add_invoice()
		self.svc = PushService(self.ctx)

	def inv(self):
		return self.store.invoices[NAME]

	# ------------------------------------------------------------------ keys
	def test_invoice_key(self):
		cfg = s.COMPANY_CFG
		self.assertEqual(invoice_key({"name": "X", "sap_b1_docentry": " 101 "}, cfg, "DEV"), "COFFERS|101")
		self.assertEqual(invoice_key({"name": "X"}, cfg, "DEV"), "DEV|X")
		self.assertEqual(invoice_key({"name": "X", "sap_b1_docentry": "5"}, dict(cfg, sap_company_code=None), "DEV"), "DEV|X")

	def test_retry_backoff(self):
		self.assertEqual([retry_delay(n).seconds // 60 for n in range(4)], [5, 10, 20, 40])

	# --------------------------------------------------------------- creating
	def test_creates_draft_with_masters(self):
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Created", result)

		[cloud_si] = self.cloud.docs["Sales Invoice"].values()
		self.assertEqual(cloud_si["docstatus"], 0)  # never submitted by a push
		self.assertEqual(cloud_si["sap_b1_key"], "DEV|" + NAME)
		self.assertEqual(cloud_si["company_address"], "TTC-Billing")
		self.assertEqual(cloud_si["taxes"][0]["account_head"], "Output Tax IGST - TTCC")  # cloud's own accounts
		self.assertEqual(json.loads(cloud_si["items"][0]["item_tax_rate"]), {"Output Tax IGST - TTCC": 18})

		[customer] = self.cloud.docs["Customer"].values()
		self.assertEqual((customer["customer_name"], customer["sap_b1_key"]), ("Acme Builders", "DEV|Customer|CUST-0001"))
		self.assertEqual(cloud_si["customer"], customer["name"])
		self.assertEqual(len(self.cloud.docs["Address"]), 3)  # company + billing + shipping
		item = self.cloud.docs["Item"]["PAINT-01"]
		self.assertEqual((item["valuation_method"], item["is_stock_item"]), ("FIFO", 0))
		self.assertEqual(item["taxes"], [{"item_tax_template": "GST 18% - TTCC"}])
		self.assertIn("320810", self.cloud.docs["GST HSN Code"])

		inv = self.inv()
		self.assertEqual(inv["aitsgst_push_status"], "Ready")
		self.assertEqual(inv["aitsgst_cloud_invoice"], cloud_si["name"])
		self.assertEqual(inv["aitsgst_recon_status"], "Match")
		self.assertEqual(self.log.last()["status"], "Success")

	def test_tax_difference_is_flagged_not_hidden(self):
		self.cloud.igst_rate = 12  # cloud template disagrees with the local invoice
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Created")  # the draft exists, but...
		self.assertEqual(self.inv()["aitsgst_recon_status"], "Mismatch")
		self.assertIn("IGST: invoice 180.00 vs GST service 120.00", self.inv()["aitsgst_recon_detail"])

	def test_second_push_creates_nothing(self):
		self.svc.push(NAME)
		inserts = self.cloud.count("insert")
		self.assertEqual(self.svc.push(NAME)["outcome"], "AlreadyReady")
		self.assertEqual(self.cloud.count("insert"), inserts)

	def test_adopts_invoice_already_on_cloud(self):
		# e.g. pushed earlier by the SAP B1 Web Portal with the same key
		self.inv()["sap_b1_docentry"] = "101"
		self.cloud.add("Sales Invoice", name="SINV-PORTAL-1", sap_b1_key="COFFERS|101", grand_total=1180,
		               taxes=[{"account_head": "IGST", "base_tax_amount": 180}])
		result = self.svc.push(NAME)
		self.assertEqual((result["outcome"], result["cloud_invoice"]), ("Adopted", "SINV-PORTAL-1"))
		self.assertEqual(self.cloud.count("insert", "Sales Invoice"), 0)

	def test_cancelled_cloud_invoice_is_not_adopted(self):
		self.cloud.add("Sales Invoice", name="SINV-OLD", sap_b1_key="DEV|" + NAME, docstatus=2)
		self.assertEqual(self.svc.push(NAME)["outcome"], "Created")

	def test_ambiguous_timeout_adopts_saved_invoice(self):
		self.cloud.fail("insert", "Sales Invoice", fakes.timeout(), after=True)
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Adopted")
		self.assertEqual(len(self.cloud.docs["Sales Invoice"]), 1)  # no duplicate

	def test_timeout_before_save_schedules_retry(self):
		self.cloud.fail("insert", "Sales Invoice", fakes.timeout())
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Failed")
		self.assertEqual(self.inv()["aitsgst_push_status"], "Failed")
		entry = self.log.last()
		self.assertEqual(entry["status"], "Retry Scheduled")
		self.assertEqual(entry["next_retry_at"], fakes.NOW + retry_delay(0))

	def test_retries_exhausted_gives_up(self):
		self.cloud.fail("insert", "Sales Invoice", fakes.timeout())
		self.svc.push(NAME, retry_count=3)
		self.assertEqual(self.log.last()["status"], "Gave Up")

	def test_validation_error_is_not_retried(self):
		self.cloud.fail("insert", "Sales Invoice", fakes.validation("HSN mismatch"))
		result = self.svc.push(NAME)
		self.assertIn("HSN mismatch", result["error"])
		self.assertEqual(self.log.last()["status"], "Failed")
		self.assertIsNone(self.log.last().get("next_retry_at"))

	# ---------------------------------------------------------------- blocking
	def test_blocked_invoice_sends_nothing(self):
		self.inv()["docstatus"] = 0
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Blocked")
		self.assertEqual(self.cloud.calls, [])
		self.assertEqual(self.inv()["aitsgst_push_status"], "Blocked")

	def test_missing_uom_blocks(self):
		del self.cloud.docs["UOM"]["Nos"]
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Blocked")
		self.assertIn("Unit of measure 'Nos'", result["problems"][0])
		self.assertEqual(self.cloud.count("insert", "Sales Invoice"), 0)

	def test_missing_cloud_tax_template_blocks(self):
		self.ctx.cfg.template_map = {}
		del self.cloud.docs["Company"][s.COMPANY_CFG["cloud_company"]]  # no abbreviation to guess with
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Blocked")
		self.assertIn("Output GST Out-state - TTC", result["problems"][0])

	def test_templates_resolved_by_company_abbr_without_map(self):
		# local "... - TTC" -> GST service "... - TTCC", found automatically from the cloud company's abbr
		self.ctx.cfg.template_map = {}
		self.cloud.add("Company", name=s.COMPANY_CFG["cloud_company"], abbr="TTCC")
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Created", result)
		[cloud_si] = self.cloud.docs["Sales Invoice"].values()
		self.assertEqual(cloud_si["taxes_and_charges"], "Output GST Out-state - TTCC")
		self.assertEqual(cloud_si["items"][0]["item_tax_template"], "GST 18% - TTCC")
		self.assertEqual(self.cloud.docs["Item"]["PAINT-01"]["taxes"], [{"item_tax_template": "GST 18% - TTCC"}])

	def test_mapped_template_wins_over_abbr_guess(self):
		self.cloud.add("Company", name=s.COMPANY_CFG["cloud_company"], abbr="ZZZ")
		self.assertEqual(self.svc.push(NAME)["outcome"], "Created")
		[cloud_si] = self.cloud.docs["Sales Invoice"].values()
		self.assertEqual(cloud_si["taxes_and_charges"], "Output GST Out-state - TTCC")  # from the map

	def test_group_customer_group_falls_back_to_selling_settings_default(self):
		# configured default "All Customer Groups" is a tree folder, which ERPNext rejects for a Customer
		self.svc.push(NAME)
		[customer] = self.cloud.docs["Customer"].values()
		self.assertEqual(customer["customer_group"], "Commercial")

	def test_configured_leaf_customer_group_is_used(self):
		self.cloud.add("Customer Group", name="Dealers", is_group=0)
		self.ctx.cfg.companies[s.COMPANY]["customer_group"] = "Dealers"
		self.svc.push(NAME)
		[customer] = self.cloud.docs["Customer"].values()
		self.assertEqual(customer["customer_group"], "Dealers")

	def test_no_usable_customer_group_blocks_clearly(self):
		self.cloud.docs["Selling Settings"]["Selling Settings"]["customer_group"] = None
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Blocked")
		self.assertIn("non-group Customer Group", result["problems"][0])
		self.assertEqual(self.cloud.count("insert", "Customer"), 0)

	def test_customer_creation_off(self):
		self.ctx.cfg.companies[s.COMPANY]["auto_create_customer"] = 0
		result = self.svc.push(NAME)
		self.assertIn("does not exist in the GST service", result["problems"][0])

	def test_adopts_hand_made_customer_by_name(self):
		self.cloud.add("Customer", name="CUST-CLOUD-7", customer_name="Acme Builders")
		self.svc.push(NAME)
		self.assertEqual(self.cloud.docs["Customer"]["CUST-CLOUD-7"]["sap_b1_key"], "DEV|Customer|CUST-0001")
		self.assertEqual(len(self.cloud.docs["Customer"]), 1)

	def test_customer_linked_elsewhere_is_conflict_in_shared_mode(self):
		# GST service WITHOUT the company field: masters are shared, so a foreign-keyed customer is a conflict
		self.cloud.missing_fields = {dt: {"aitsgst_company"} for dt in ("Customer", "Item", "Address")}
		self.cloud.add("Customer", name="Acme Builders", customer_name="Acme Builders", sap_b1_key="OTHER|C1")
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Blocked")
		self.assertIn("linked to a different record", result["problems"][0])

	def test_deleted_cloud_invoice_is_not_recreated(self):
		self.inv().update(aitsgst_push_status="Ready", aitsgst_cloud_invoice="SINV-GONE")
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Blocked")
		self.assertEqual(self.cloud.count("insert"), 0)
