"""One GST service, several client companies: masters tagged with aitsgst_company are only ever matched
or changed by their own company."""

import json
import pathlib
import re
import unittest

from aitsgst.services.push import COMPANY_FIELD, PushService
from aitsgst.tests import fakes
from aitsgst.tests import samples as s

NAME = "ACC-SINV-2026-00001"
MINE = s.COMPANY_CFG["cloud_company"]
OTHER = "Other Client Pvt Ltd"


class TestMultiCompany(unittest.TestCase):
	def setUp(self):
		self.ctx = fakes.make_ctx()
		self.cloud, self.store = self.ctx.client, self.ctx.store
		fakes.seed_cloud_setup(self.cloud)
		self.store.add_invoice()
		self.svc = PushService(self.ctx)

	def push(self):
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Created", result)
		return next(iter(self.cloud.docs["Sales Invoice"].values()))

	def customers(self):
		return list(self.cloud.docs["Customer"].values())

	# ------------------------------------------------------------ customers
	def test_everything_created_is_tagged_with_the_company(self):
		si = self.push()
		[customer] = self.customers()
		self.assertEqual(customer[COMPANY_FIELD], MINE)
		item = self.cloud.docs["Item"]["PAINT-01"]
		self.assertEqual(item[COMPANY_FIELD], MINE)
		for addr in self.cloud.docs["Address"].values():
			if addr["name"] != "TTC-Billing":  # the company's own address is not created by the app
				self.assertEqual(addr[COMPANY_FIELD], MINE)
		self.assertEqual(si["customer"], customer["name"])

	def test_other_clients_customer_with_same_name_and_gstin_is_not_touched(self):
		other = self.cloud.add("Customer", name="Acme Builders", customer_name="Acme Builders", gstin=s.CUSTOMER_GSTIN,
		                       **{COMPANY_FIELD: OTHER, "sap_b1_key": "OTHERSITE|Customer|CUST-0001"})
		si = self.push()
		mine = [c for c in self.customers() if c.get(COMPANY_FIELD) == MINE]
		self.assertEqual(len(mine), 1)
		self.assertEqual(si["customer"], mine[0]["name"])
		self.assertNotEqual(si["customer"], "Acme Builders")
		self.assertEqual(self.cloud.docs["Customer"]["Acme Builders"], other)  # unchanged

	def test_reuses_own_company_customer_from_another_site(self):
		# e.g. the same client company invoicing from a second local site
		self.cloud.add("Customer", name="Acme Builders", customer_name="Acme Builders",
		               **{COMPANY_FIELD: MINE, "sap_b1_key": "SECONDSITE|Customer|C9"})
		si = self.push()
		self.assertEqual(si["customer"], "Acme Builders")
		self.assertEqual(len(self.customers()), 1)

	def test_claims_untagged_customer_this_site_created(self):
		self.cloud.add("Customer", name="Acme Builders", customer_name="Acme Builders", sap_b1_key="DEV|Customer|CUST-0001")
		si = self.push()
		self.assertEqual(si["customer"], "Acme Builders")
		self.assertEqual(self.cloud.docs["Customer"]["Acme Builders"][COMPANY_FIELD], MINE)

	def test_does_not_claim_untagged_customer_of_another_site(self):
		self.cloud.add("Customer", name="Acme Builders", customer_name="Acme Builders", sap_b1_key="OTHERSITE|Customer|X")
		si = self.push()
		self.assertNotEqual(si["customer"], "Acme Builders")
		self.assertNotIn(COMPANY_FIELD, self.cloud.docs["Customer"]["Acme Builders"])

	# ---------------------------------------------------------------- items
	def test_item_code_taken_by_other_company_gets_company_prefix(self):
		self.cloud.add("Item", name="PAINT-01", item_code="PAINT-01", **{COMPANY_FIELD: OTHER, "sap_b1_key": "OTHERSITE|Item|PAINT-01"})
		si = self.push()
		self.assertEqual(si["items"][0]["item_code"], "TTCC-PAINT-01")
		self.assertEqual(self.cloud.docs["Item"]["TTCC-PAINT-01"][COMPANY_FIELD], MINE)
		self.assertNotIn("TTCC", self.cloud.docs["Item"]["PAINT-01"].get(COMPANY_FIELD))  # other company's item untouched

	def test_second_push_reuses_prefixed_item_by_key(self):
		self.cloud.add("Item", name="PAINT-01", item_code="PAINT-01", **{COMPANY_FIELD: OTHER})
		self.push()
		self.store.invoices[NAME].update(aitsgst_push_status=None, aitsgst_cloud_invoice=None)
		del self.cloud.docs["Sales Invoice"][next(iter(self.cloud.docs["Sales Invoice"]))]
		si = self.push()
		self.assertEqual(si["items"][0]["item_code"], "TTCC-PAINT-01")
		self.assertEqual(len(self.cloud.docs["Item"]), 2)

	def test_item_codes_all_taken_blocks_clearly(self):
		self.cloud.add("Item", name="PAINT-01", item_code="PAINT-01", **{COMPANY_FIELD: OTHER})
		self.cloud.add("Item", name="TTCC-PAINT-01", item_code="TTCC-PAINT-01", **{COMPANY_FIELD: OTHER})
		result = self.svc.push(NAME)
		self.assertEqual(result["outcome"], "Blocked")
		self.assertIn("Item Code Prefix", result["problems"][0])
		self.assertEqual(self.cloud.count("insert", "Sales Invoice"), 0)

	# ---------------------------------------------------------- shared mode
	def test_without_company_field_nothing_is_tagged(self):
		self.cloud.missing_fields = {dt: {COMPANY_FIELD} for dt in ("Customer", "Item", "Address")}
		self.push()
		self.assertNotIn(COMPANY_FIELD, self.customers()[0])
		self.assertNotIn(COMPANY_FIELD, self.cloud.docs["Item"]["PAINT-01"])


class TestSyncLogActions(unittest.TestCase):
	def test_every_logged_action_is_an_allowed_option(self):
		app = pathlib.Path(__file__).resolve().parent.parent
		options = set(json.loads((app / "aits_gst/doctype/aits_gst_sync_log/aits_gst_sync_log.json").read_text())["fields"][0]["options"].split("\n"))
		used = set()
		for path in list(app.glob("*.py")) + list((app / "services").glob("*.py")):
			used |= set(re.findall(r'(?:log\.write|SyncLog\(\)\.write)\(\s*"([^"]+)"', path.read_text()))
		self.assertTrue(used)
		self.assertEqual(used - options, set())
