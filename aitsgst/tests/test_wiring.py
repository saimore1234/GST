"""Frappe-level wiring: permissions, the no-call-until-confirmed gate, webhook signature,
QR + print format rendering, cancel guard, scheduler early exits. No Sales Invoice is
created and no network call is made (requests is patched to fail loudly)."""

import base64
import hashlib
import hmac
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from aitsgst import api, events, tasks
from aitsgst.core.cloud_client import CloudClient
from aitsgst.services.context import GateClosed, build_client, load_config
from aitsgst.utils.qr import get_qr_data_uri


def _no_network(*args, **kwargs):
	raise AssertionError("A network call was attempted during tests")


@patch("requests.Session.request", _no_network)
class TestWiring(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		s = frappe.get_single("AITS GST Settings")
		s.update({"enabled": 1, "cloud_url": "https://sandbox.example.com", "api_key": "testkey", "api_secret": "testsecret-123",
		          "local_site_code": s.local_site_code or "TESTSITE", "cloud_key_field": "sap_b1_key", "request_timeout": 30,
		          "sync_interval_minutes": 15, "max_push_retries": 3, "sandbox_confirmed": 0, "allow_production": 0})
		s.set("companies", [])
		s.save()
		frappe.clear_document_cache("AITS GST Settings", "AITS GST Settings")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_document_cache("AITS GST Settings", "AITS GST Settings")

	# -------------------------------------------------------------- gate
	def test_no_cloud_client_until_sandbox_confirmed(self):
		with self.assertRaises(GateClosed):
			build_client(load_config())

		frappe.db.set_single_value("AITS GST Settings", "sandbox_confirmed", 1)
		frappe.clear_document_cache("AITS GST Settings", "AITS GST Settings")
		client = build_client(load_config())
		self.assertIsInstance(client, CloudClient)
		self.assertNotIn("testsecret-123", repr(client))

	def test_push_endpoint_refuses_when_gate_closed(self):
		name = frappe.get_all("Sales Invoice", limit=1, pluck="name")[0]
		with self.assertRaises(GateClosed):
			api.push_invoice(name)

	def test_disabled_settings_block(self):
		frappe.db.set_single_value("AITS GST Settings", {"enabled": 0, "sandbox_confirmed": 1})
		frappe.clear_document_cache("AITS GST Settings", "AITS GST Settings")
		self.assertRaises(GateClosed, build_client, load_config())

	def test_secret_is_stored_encrypted(self):
		raw = frappe.db.get_single_value("AITS GST Settings", "api_secret")
		self.assertNotEqual(raw, "testsecret-123")
		self.assertEqual(frappe.get_single("AITS GST Settings").get_password("api_secret"), "testsecret-123")

	# ------------------------------------------------------- permissions
	def test_endpoints_need_role(self):
		name = frappe.get_all("Sales Invoice", limit=1, pluck="name")[0]
		frappe.set_user("Guest")
		endpoints = ((api.push_invoice, (name,)), (api.generate_e_invoice, (name, 1)), (api.generate_e_waybill, (name, None, 1)),
		             (api.update_vehicle, (name, None, 1)), (api.cancel_e_invoice, (name, "Duplicate")),
		             (api.cancel_e_waybill, (name, "Duplicate")), (api.refresh_invoice, (name,)),
		             (api.get_transport_defaults, (name,)), (api.test_connection, ()))
		for fn, args in endpoints:
			with self.assertRaises(frappe.PermissionError, msg=fn.__name__):
				fn(*args)

	def test_role_check_itself(self):
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, api.require_role)
		frappe.set_user("Administrator")
		api.require_role()  # no error

	# ----------------------------------------------------------- webhook
	def test_webhook_signature(self):
		body = b'{"name": "SINV-1"}'
		good = base64.b64encode(hmac.new(b"whsecret", body, hashlib.sha256).digest()).decode()
		self.assertTrue(api.verify_signature("whsecret", body, good))
		self.assertFalse(api.verify_signature("whsecret", body + b" ", good))
		self.assertFalse(api.verify_signature("other", body, good))
		self.assertFalse(api.verify_signature("whsecret", body, None))

	# ------------------------------------------------------- QR + print
	def test_qr_data_uri(self):
		uri = get_qr_data_uri("eyJhbGciOiJSUzI1NiJ9.signed.qr")
		self.assertTrue(uri.startswith("data:image/png;base64,"))
		self.assertEqual(base64.b64decode(uri.split(",", 1)[1])[:8], b"\x89PNG\r\n\x1a\n")
		self.assertEqual(get_qr_data_uri(None), "")

	def test_print_format_renders_irn_and_qr(self):
		self.assertTrue(frappe.db.exists("Print Format", "AITS GST e-Invoice"))
		name = frappe.get_all("Sales Invoice", filters={"docstatus": 1}, limit=1, pluck="name")[0]
		doc = frappe.get_doc("Sales Invoice", name)  # in memory only - never saved
		doc.update({"aitsgst_irn": "f" * 64, "aitsgst_ack_no": "112410000000001", "aitsgst_einvoice_status": "Generated",
		            "aitsgst_signed_qr_code": "signed.qr.text", "aitsgst_ewaybill": "331000000001", "aitsgst_ewb_status": "Generated"})
		html = frappe.get_print("Sales Invoice", name, "AITS GST e-Invoice", doc=doc, no_letterhead=1)
		self.assertIn("f" * 64, html)
		self.assertIn("112410000000001", html)
		self.assertIn("data:image/png;base64,", html)
		self.assertIn("331000000001", html)

		doc.aitsgst_einvoice_status = "Cancelled"
		html = frappe.get_print("Sales Invoice", name, "AITS GST e-Invoice", doc=doc, no_letterhead=1)
		self.assertIn("CANCELLED", html)
		self.assertNotIn("data:image/png;base64,", html)  # no valid-looking QR on a cancelled IRN

	# ------------------------------------------------------ cancel guard
	def test_local_cancel_blocked_while_irn_live(self):
		doc = frappe._dict(aitsgst_einvoice_status="Generated", aitsgst_irn="IRN1", aitsgst_ewb_status="Generated", aitsgst_ewaybill="331")
		with self.assertRaises(frappe.ValidationError):
			events.block_cancel_with_live_documents(doc)
		events.block_cancel_with_live_documents(frappe._dict(aitsgst_einvoice_status="Cancelled", aitsgst_ewb_status="Cancelled"))

		# a plain local cancel is refused while the cloud invoice is still active...
		pushed = frappe._dict(aitsgst_cloud_invoice="T/SD/1", aitsgst_cloud_docstatus="Submitted")
		with self.assertRaises(frappe.ValidationError) as ctx:
			events.block_cancel_with_live_documents(pushed)
		self.assertIn("Cancel Everywhere", str(ctx.exception))
		# ...allowed once it is cancelled / deleted there
		for status in ("Cancelled", "Deleted"):
			events.block_cancel_with_live_documents(frappe._dict(pushed, aitsgst_cloud_docstatus=status))

		# Cancel Everywhere's own local cancel passes the hook
		doc = frappe.get_doc({"doctype": "Sales Invoice"})
		doc.update(pushed)
		doc.flags.aitsgst_cancel_everywhere = True
		events.before_cancel(doc)

	# ---------------------------------------------------------- scheduler
	def test_scheduler_idle_when_gate_closed(self):
		with patch("frappe.enqueue") as enqueue:
			tasks.run_due_jobs()
		enqueue.assert_not_called()

	def test_poll_change_detection(self):
		local = frappe._dict(aitsgst_cloud_docstatus="Submitted", aitsgst_irn="I", aitsgst_einvoice_status="Generated",
		                     aitsgst_ewaybill=None, aitsgst_ewb_status=None, aitsgst_vehicle_no=None)
		same = {"name": "S", "docstatus": 1, "irn": "I", "einvoice_status": "Generated"}
		self.assertFalse(tasks._looks_changed(local, same))
		self.assertTrue(tasks._looks_changed(local, {**same, "einvoice_status": "Cancelled"}))
		self.assertTrue(tasks._looks_changed(local, {**same, "docstatus": 2}))
		self.assertTrue(tasks._looks_changed(local, {**same, "ewaybill": "331", "e_waybill_status": "Generated"}))

		with_ewb = frappe._dict(local, aitsgst_ewaybill="331", aitsgst_ewb_status="Generated", aitsgst_ewb_valid_upto=None)
		cloud_ewb = {**same, "ewaybill": "331", "e_waybill_status": "Generated"}
		self.assertTrue(tasks._looks_changed(with_ewb, cloud_ewb))  # validity still missing -> re-read
		with_ewb.aitsgst_ewb_valid_upto = "2026-10-08 23:59:00"
		self.assertFalse(tasks._looks_changed(with_ewb, cloud_ewb))
