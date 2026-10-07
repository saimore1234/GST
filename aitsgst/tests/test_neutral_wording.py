"""End users must not be shown that a second (cloud) site is involved: every label and
on-screen text says 'GST service' / 'e-invoice record' instead."""

import json
import pathlib
import re
import unittest

from aitsgst.setup.custom_fields import FIELDS
from aitsgst.utils.qr import qr_png

APP = pathlib.Path(__file__).resolve().parent.parent
CLOUD = re.compile(r"cloud", re.I)


class TestNeutralWording(unittest.TestCase):
	def test_sales_invoice_field_labels(self):
		for f in FIELDS:
			self.assertFalse(CLOUD.search(f.get("label") or ""), f["fieldname"])
			self.assertFalse(CLOUD.search(f.get("options") or ""), f["fieldname"])

	def test_form_script_texts(self):
		js = (APP / "public/js/sales_invoice.js").read_text()
		texts = re.findall(r'__\("((?:[^"\\]|\\.)*)"', js) + re.findall(r"`([^`]*)`", js)
		self.assertTrue(texts)
		for text in texts:
			self.assertFalse(CLOUD.search(re.sub(r"aitsgst_cloud_\w+", "", text)), text)

	def test_admin_doctype_labels(self):
		for path in (APP / "aits_gst/doctype").glob("*/*.json"):
			for f in json.loads(path.read_text())["fields"]:
				for key in ("label", "description"):
					self.assertFalse(CLOUD.search(f.get(key) or ""), f"{path.name}:{f['fieldname']}.{key}")

	def test_internal_fields_hidden(self):
		hidden = {f["fieldname"] for f in FIELDS if f.get("hidden")}
		for name in ("aitsgst_cloud_invoice", "aitsgst_cloud_key", "aitsgst_cloud_docstatus", "aitsgst_cloud_grand_total",
		             "aitsgst_recon_status", "aitsgst_recon_detail", "aitsgst_signed_qr_code"):
			self.assertIn(name, hidden)

	def test_qr_image_field_and_png(self):
		field = next(f for f in FIELDS if f["fieldname"] == "aitsgst_qr_image")
		self.assertEqual(field["fieldtype"], "Attach Image")
		self.assertEqual(field["print_hide"], 0)
		self.assertEqual(qr_png("signed.qr")[:8], b"\x89PNG\r\n\x1a\n")
