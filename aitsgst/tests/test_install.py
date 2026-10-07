import frappe
from frappe.tests.utils import FrappeTestCase

from aitsgst.setup.custom_fields import FIELDS


class TestInstall(FrappeTestCase):
	def test_doctypes_exist(self):
		for name in ("AITS GST Settings", "AITS GST Company", "AITS GST Template Map", "AITS GST Sync Log"):
			self.assertTrue(frappe.db.exists("DocType", name), name)

	def test_role_exists(self):
		self.assertTrue(frappe.db.exists("Role", "AITS GST Manager"))

	def test_custom_fields_applied(self):
		meta = frappe.get_meta("Sales Invoice", cached=False)
		for field in FIELDS:
			self.assertIsNotNone(meta.get_field(field["fieldname"]), field["fieldname"])

	def test_no_collision_with_india_compliance(self):
		# Our fields are all prefixed; India Compliance's own fields stay untouched.
		for field in FIELDS:
			self.assertTrue(field["fieldname"].startswith("aitsgst_"), field["fieldname"])

	def test_data_fields_are_app_owned(self):
		for field in FIELDS:
			if field["fieldtype"] in ("Tab Break", "Section Break", "Column Break", "HTML"):
				continue
			self.assertEqual(field.get("read_only"), 1, field["fieldname"])
			self.assertEqual(field.get("no_copy"), 1, field["fieldname"])
			self.assertEqual(field.get("allow_on_submit"), 1, field["fieldname"])
