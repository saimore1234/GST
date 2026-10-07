import frappe
from frappe.tests.utils import FrappeTestCase

from aitsgst.aits_gst.doctype.aits_gst_settings.aits_gst_settings import validate_cloud_url


class TestSettings(FrappeTestCase):
	def test_https_only(self):
		self.assertEqual(validate_cloud_url(" https://demo.m.erpnext.com/ "), "https://demo.m.erpnext.com")
		self.assertIsNone(validate_cloud_url(""))
		for bad in (
			"http://demo.m.erpnext.com",
			"https://demo.m.erpnext.com/app",
			"https://user:pass@demo.m.erpnext.com",
			"https://demo.m.erpnext.com?x=1",
			"ftp://demo",
			"demo.m.erpnext.com",
		):
			with self.assertRaises(frappe.ValidationError, msg=bad):
				validate_cloud_url(bad)

	def _settings(self):
		doc = frappe.get_single("AITS GST Settings")
		doc.enabled = 0
		doc.cloud_url = "https://demo.example.com"
		doc.local_site_code = doc.local_site_code or "TESTSITE"  # never change a code already used in cloud keys
		doc.cloud_key_field = "sap_b1_key"
		doc.request_timeout = 60
		doc.sync_interval_minutes = 15
		doc.max_push_retries = 5
		doc.set("companies", [])
		doc.set("template_map", [])
		return doc

	def test_rejects_invalid_gstin(self):
		doc = self._settings()
		company = frappe.get_all("Company", limit=1, pluck="name")[0]
		doc.append("companies", {"company": company, "cloud_company": "X", "company_gstin": "NOTAGSTIN"})
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_rejects_duplicate_company(self):
		doc = self._settings()
		company = frappe.get_all("Company", limit=1, pluck="name")[0]
		for _ in range(2):
			doc.append("companies", {"company": company, "cloud_company": "X", "company_gstin": "27AAPFU0939F1ZV"})
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_enable_requires_credentials(self):
		doc = self._settings()
		doc.enabled = 1
		doc.api_key = None
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_rejects_bad_site_code(self):
		doc = self._settings()
		doc.local_site_code = "has|pipe"
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_valid_settings_save_and_normalise(self):
		doc = self._settings()
		company = frappe.get_all("Company", limit=1, pluck="name")[0]
		doc.append("companies", {"company": company, "cloud_company": " Cloud Co ", "company_gstin": "27aapfu0939f1zv"})
		doc.save()
		self.assertEqual(doc.companies[0].company_gstin, "27AAPFU0939F1ZV")
		self.assertEqual(doc.companies[0].cloud_company, "Cloud Co")
