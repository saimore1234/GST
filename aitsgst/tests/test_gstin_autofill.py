"""GSTIN autofill endpoint: validation, permission, on/off, cache, error handling. The GST service is
the in-memory FakeCloud; no network call is made."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from aitsgst import api
from aitsgst.core.cloud_client import CloudError
from aitsgst.tests import fakes

GSTIN = "27AAPFU0939F1ZV"
INFO = {
	"gstin": GSTIN, "business_name": "Acme Builders Private Limited", "gst_category": "Registered Regular", "status": "Active",
	"permanent_address": {"address_line1": "1, Mg Road", "address_line2": "PU 4 Commercial, ", "city": "Pune", "state": "Maharashtra",
	                      "pincode": "411001", "country": "India"},
	"all_addresses": [{"address_line1": "1, Mg Road", "city": "Pune", "state": "Maharashtra", "pincode": "411001", "country": "India"}],
	"some_internal_field": "not passed to the browser",
}


def _no_network(*args, **kwargs):
	raise AssertionError("A network call was attempted during tests")


@patch("requests.Session.request", _no_network)
class TestGstinAutofill(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(f"aitsgst:gstin:{GSTIN}")
		self.cloud = fakes.FakeCloud()
		self.cloud.methods[api.GSTIN_INFO_METHOD] = lambda gstin: dict(INFO) if gstin == GSTIN else {}
		self.ctx = SimpleNamespace(client=self.cloud)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(f"aitsgst:gstin:{GSTIN}")

	def call(self, gstin=GSTIN, enabled=True):
		with patch("aitsgst.api._gstin_autofill_enabled", return_value=enabled), patch("aitsgst.api.get_context", return_value=self.ctx):
			return api.get_gstin_details(gstin)

	def test_returns_details(self):
		result = self.call(" 27aapfu0939f1zv ")  # trimmed and upper-cased
		self.assertEqual(result["business_name"], "Acme Builders Private Limited")
		self.assertEqual(result["gst_category"], "Registered Regular")
		self.assertEqual(result["permanent_address"]["pincode"], "411001")
		self.assertEqual(result["permanent_address"]["address_line2"], "PU 4 Commercial")  # stray ", " removed
		self.assertNotIn("some_internal_field", result)

	def test_cached_so_the_service_is_asked_once(self):
		self.call()
		self.call()
		self.assertEqual(self.cloud.count("call", api.GSTIN_INFO_METHOD), 1)

	def test_invalid_gstin_rejected_without_calling_the_service(self):
		for bad in ("27AAPFU0939F1Z", "NOTAGSTIN", ""):
			with self.assertRaises(frappe.ValidationError):
				self.call(bad)
		self.assertEqual(self.cloud.calls, [])

	def test_disabled_does_nothing(self):
		self.assertEqual(self.call(enabled=False), {"disabled": True})
		self.assertEqual(self.cloud.calls, [])

	def test_guest_not_allowed(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			self.call()

	def test_service_error_is_shown_and_not_cached(self):
		self.cloud.fail("call", api.GSTIN_INFO_METHOD, CloudError("GST service error (417): GSTIN not found"))
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.call()
		self.assertIn("GSTIN not found", str(ctx.exception))
		self.assertEqual(self.call()["business_name"], "Acme Builders Private Limited")  # retried, not cached as failure

	def test_empty_answer_not_cached(self):
		self.cloud.methods[api.GSTIN_INFO_METHOD] = lambda gstin: {}
		self.call()
		self.call()
		self.assertEqual(self.cloud.count("call", api.GSTIN_INFO_METHOD), 2)

	def test_hooked_on_party_and_address_forms(self):
		doctype_js = frappe.get_hooks("doctype_js")
		for doctype in ("Customer", "Supplier", "Address"):
			self.assertIn("public/js/gstin_autofill.js", doctype_js.get(doctype, []), doctype)
