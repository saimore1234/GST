from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from aitsgst import api
from aitsgst.core.cloud_client import CloudError
from aitsgst.services.push import COMPANY_FIELD
from aitsgst.tests import fakes


def _no_network(*args, **kwargs):
	raise AssertionError("A network call was attempted during tests")


@patch("requests.Session.request", _no_network)
class TestSetupFields(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.cloud = fakes.FakeCloud()
		self.cloud.missing_fields = {"Customer": {COMPANY_FIELD}, "Item": {COMPANY_FIELD}, "Address": {COMPANY_FIELD, "sap_b1_key"}}

	def run_setup(self, confirm=1):
		with patch("aitsgst.api.load_config", return_value=fakes.make_cfg()), patch("aitsgst.api.build_client", return_value=self.cloud), \
				patch("aitsgst.api.SyncLog") as log:
			result = api.setup_service_fields(confirm=confirm)
		return result, log

	def test_creates_only_missing_fields(self):
		result, _ = self.run_setup()
		self.assertTrue(result["success"])
		created = {r["field"] for r in result["results"] if r["status"] == "created"}
		self.assertEqual(created, {"Customer.aitsgst_company", "Item.aitsgst_company", "Address.aitsgst_company", "Address.sap_b1_key"})
		self.assertEqual(self.cloud.count("insert", "Custom Field"), 4)
		link = next(d for d in self.cloud.docs["Custom Field"].values() if d["dt"] == "Customer")
		self.assertEqual((link["fieldtype"], link["options"]), ("Link", "Company"))
		again, _ = self.run_setup()
		self.assertEqual({r["status"] for r in again["results"]}, {"already present"})

	def test_permission_error_is_explained(self):
		self.cloud.fail("insert", "Custom Field", CloudError("GST service denied permission for the API user (403).", 403), times=10)
		result, _ = self.run_setup()
		self.assertFalse(result["success"])
		self.assertTrue(any("System Manager" in r["status"] for r in result["results"]))

	def test_needs_confirmation_and_system_manager(self):
		with self.assertRaises(frappe.ValidationError):
			self.run_setup(confirm=0)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			self.run_setup()
