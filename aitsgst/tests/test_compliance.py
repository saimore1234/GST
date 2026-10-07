import unittest

from aitsgst.core.cloud_client import CloudError
from aitsgst.services.compliance import EINV, EWB, Blocked, ComplianceService, ConfirmationRequired
from aitsgst.tests import fakes
from aitsgst.tests import samples as s

NAME = "ACC-SINV-2026-00001"
CLOUD = "SINV-CLOUD-1"
IRN = "a" * 64
ROAD = {"mode_of_transport": "Road", "vehicle_no": "mh-12 ab 1234", "distance": 120}


class Base(unittest.TestCase):
	def setUp(self):
		self.ctx = fakes.make_ctx()
		self.cloud, self.store, self.log = self.ctx.client, self.ctx.store, self.ctx.log
		self.store.add_invoice(aitsgst_push_status="Pushed", aitsgst_cloud_invoice=CLOUD, aitsgst_recon_status="Match")
		self.cloud.add("Sales Invoice", name=CLOUD, docstatus=0, company_gstin=s.GSTIN, billing_address_gstin=s.CUSTOMER_GSTIN,
		               gst_category="Registered Regular", posting_date="2026-10-07")
		self.cloud.add("GST Settings", name="GST Settings", enable_api=1, enable_e_invoice=1, enable_e_waybill=1, sandbox_mode=1,
		               credentials=[{"gstin": s.GSTIN, "service": "e-Invoice"}, {"gstin": s.GSTIN, "service": "e-Waybill"}])
		self._install_ic_methods()
		self.sleeps = []
		self.svc = ComplianceService(self.ctx, sleep=self.sleeps.append)

	def _install_ic_methods(self):
		docs, cloud = self.cloud.docs, self.cloud

		def gen_einv(docname):
			assert docs["Sales Invoice"][docname]["docstatus"] == 1, "IC requires a submitted invoice"
			docs["Sales Invoice"][docname].update(irn=IRN, einvoice_status="Generated")
			cloud.add("e-Invoice Log", name=IRN, acknowledgement_number="112410000000001",
			          acknowledged_on="2026-10-07 11:30:00", signed_qr_code="eyJhbGciOi.SIGNED.QR")

		def gen_ewb(doctype, docname, values):
			docs["Sales Invoice"][docname].update(ewaybill="331000000001", e_waybill_status="Generated", **values)
			cloud.add("e-Waybill Log", name="331000000001", created_on="2026-10-07 11:40:00", valid_upto="2026-10-08 23:59:00")

		def cancel_einv(docname, values):
			d = docs["Sales Invoice"][docname]
			d.update(einvoice_status="Cancelled")
			if d.get("ewaybill"):
				d.update(ewaybill=None, e_waybill_status="Cancelled")

		def cancel_ewb(doctype, docname, values):
			docs["Sales Invoice"][docname].update(ewaybill=None, e_waybill_status="Cancelled")

		def update_vehicle(doctype, docname, values):
			docs["Sales Invoice"][docname].update(vehicle_no=values["vehicle_no"], mode_of_transport=values["mode_of_transport"])
			docs["e-Waybill Log"]["331000000001"]["valid_upto"] = "2026-10-09 23:59:00"

		self.cloud.methods.update({
			f"{EINV}.generate_e_invoice": gen_einv, f"{EWB}.generate_e_waybill": gen_ewb,
			f"{EINV}.cancel_e_invoice": cancel_einv, f"{EWB}.cancel_e_waybill": cancel_ewb,
			f"{EWB}.update_vehicle_info": update_vehicle,
		})

	def inv(self):
		return self.store.invoices[NAME]

	def cloud_inv(self):
		return self.cloud.docs["Sales Invoice"][CLOUD]

	def generated(self):
		self.svc.generate_e_invoice(NAME, confirm=True)


class TestEInvoice(Base):
	def test_generates_and_writes_back(self):
		result = self.svc.generate_e_invoice(NAME, confirm=True)
		self.assertEqual(result["outcome"], "Generated")
		self.assertEqual(self.cloud_inv()["docstatus"], 1)  # draft submitted first
		inv = self.inv()
		self.assertEqual(inv["aitsgst_irn"], IRN)
		self.assertEqual(inv["aitsgst_ack_no"], "112410000000001")
		self.assertEqual(str(inv["aitsgst_ack_date"]), "2026-10-07 11:30:00")
		self.assertEqual(inv["aitsgst_signed_qr_code"], "eyJhbGciOi.SIGNED.QR")
		self.assertEqual((inv["aitsgst_einvoice_status"], inv["aitsgst_cloud_docstatus"]), ("Generated", "Submitted"))

	def test_requires_confirmation_and_sends_nothing(self):
		with self.assertRaises(ConfirmationRequired):
			self.svc.generate_e_invoice(NAME, confirm=False)
		self.assertEqual(self.cloud.count("call"), 0)
		self.assertEqual(self.cloud.count("update"), 0)  # not even submitted

	def test_production_cloud_blocked_unless_allowed(self):
		self.cloud.docs["GST Settings"]["GST Settings"]["sandbox_mode"] = 0
		with self.assertRaises(Blocked) as ctx:
			self.svc.generate_e_invoice(NAME, confirm=True)
		self.assertTrue(any("PRODUCTION" in p for p in ctx.exception.problems))
		self.assertEqual(self.cloud.count("call") + self.cloud.count("update"), 0)

		self.ctx.cfg.allow_production = True
		self.assertEqual(self.svc.generate_e_invoice(NAME, confirm=True)["outcome"], "Generated")

	def test_blocks_b2c_mismatch_and_missing_credentials(self):
		self.cloud_inv().update(billing_address_gstin=None, gst_category="Unregistered")
		self.inv()["aitsgst_recon_status"] = "Mismatch"
		self.cloud.docs["GST Settings"]["GST Settings"]["credentials"] = []
		with self.assertRaises(Blocked) as ctx:
			self.svc.generate_e_invoice(NAME, confirm=True)
		text = " ".join(ctx.exception.problems)
		self.assertIn("B2B", text)
		self.assertIn("do not match the local invoice", text)
		self.assertIn("no e-Invoice API credentials", text)
		self.assertEqual(self.log.last()["status"], "Blocked")

	def test_not_pushed(self):
		self.inv().update(aitsgst_push_status=None, aitsgst_cloud_invoice=None)
		self.assertRaises(Blocked, self.svc.generate_e_invoice, NAME, True)

	def test_already_generated_on_cloud(self):
		self.generated()
		calls = self.cloud.count("call")
		self.assertEqual(self.svc.generate_e_invoice(NAME, confirm=True)["outcome"], "AlreadyGenerated")
		self.assertEqual(self.cloud.count("call"), calls)

	def test_ambiguous_failure_records_irn(self):
		self.cloud.fail("call", f"{EINV}.generate_e_invoice", fakes.timeout(), after=True)
		result = self.svc.generate_e_invoice(NAME, confirm=True)
		self.assertEqual(result["irn"], IRN)
		self.assertEqual(self.inv()["aitsgst_einvoice_status"], "Generated")

	def test_real_failure_is_recorded(self):
		self.cloud.fail("call", f"{EINV}.generate_e_invoice", fakes.validation("Invalid pincode"))
		with self.assertRaises(CloudError):
			self.svc.generate_e_invoice(NAME, confirm=True)
		inv = self.inv()
		self.assertEqual(inv["aitsgst_einvoice_status"], "Failed")
		self.assertIn("Invalid pincode", inv["aitsgst_last_error"])
		self.assertEqual(inv["aitsgst_cloud_docstatus"], "Submitted")  # the submit did happen
		self.assertEqual(self.log.last()["status"], "Failed")


class TestEWaybill(Base):
	def test_generates_with_clean_values(self):
		result = self.svc.generate_e_waybill(NAME, ROAD, confirm=True)
		self.assertEqual(result["ewaybill"], "331000000001")
		self.assertEqual(self.cloud_inv()["vehicle_no"], "MH12AB1234")  # normalised before sending
		inv = self.inv()
		self.assertEqual((inv["aitsgst_ewb_status"], inv["aitsgst_vehicle_no"]), ("Generated", "MH12AB1234"))
		self.assertEqual(str(inv["aitsgst_ewb_valid_upto"]), "2026-10-08 23:59:00")

	def test_waits_for_log_written_in_background(self):
		# Real India Compliance creates the e-Waybill Log in a background job, after the call returns.
		real = self.cloud.methods[f"{EWB}.generate_e_waybill"]
		log_holder = {}

		def gen_without_log(doctype, docname, values):
			real(doctype=doctype, docname=docname, values=values)
			log_holder["log"] = self.cloud.docs["e-Waybill Log"].pop("331000000001")

		self.cloud.methods[f"{EWB}.generate_e_waybill"] = gen_without_log
		original_sleep = self.svc._sleep

		def sleep_then_log_appears(seconds):
			original_sleep(seconds)
			if len(self.sleeps) == 2:  # the background job finishes during the second wait
				self.cloud.docs["e-Waybill Log"]["331000000001"] = log_holder["log"]

		self.svc._sleep = sleep_then_log_appears
		self.svc.generate_e_waybill(NAME, ROAD, confirm=True)
		self.assertEqual(self.sleeps, [1, 2])
		self.assertEqual(str(self.inv()["aitsgst_ewb_valid_upto"]), "2026-10-08 23:59:00")

	def test_validation(self):
		cases = [
			({"mode_of_transport": "Road"}, "vehicle number"),
			({"mode_of_transport": "Road", "vehicle_no": "MH12@"}, "not valid"),
			({"mode_of_transport": "Rail"}, "transport document"),
			({"mode_of_transport": "Boat", "vehicle_no": "MH12AB1234"}, "Road, Rail"),
			({**ROAD, "distance": 5000}, "4000"),
			({**ROAD, "gst_transporter_id": "SHORT"}, "15 characters"),
		]
		for values, expected in cases:
			with self.assertRaises(Blocked, msg=values) as ctx:
				self.svc.generate_e_waybill(NAME, values, confirm=True)
			self.assertIn(expected, " ".join(ctx.exception.problems))
		self.assertEqual(self.cloud.count("call"), 0)

	def test_requires_confirmation(self):
		self.assertRaises(ConfirmationRequired, self.svc.generate_e_waybill, NAME, ROAD, False)
		self.assertEqual(self.cloud.count("call"), 0)

	def test_update_vehicle(self):
		self.svc.generate_e_waybill(NAME, ROAD, confirm=True)
		values = {"mode_of_transport": "Road", "vehicle_no": "KA01XY9999", "reason": "Due to Break Down",
		          "place_of_change": "Hubli", "state": "karnataka"}
		result = self.svc.update_vehicle(NAME, values, confirm=True)
		self.assertEqual(result["vehicle_no"], "KA01XY9999")
		self.assertEqual(str(self.inv()["aitsgst_ewb_valid_upto"]), "2026-10-09 23:59:00")

	def test_update_vehicle_validation(self):
		with self.assertRaises(Blocked) as ctx:
			self.svc.update_vehicle(NAME, {"vehicle_no": "KA01XY9999", "reason": "Because"}, confirm=True)
		text = " ".join(ctx.exception.problems)
		self.assertIn("no active e-way bill", text)
		self.assertIn("Choose a reason", text)
		self.assertIn("place", text)
		self.assertIn("state", text)


class TestCancel(Base):
	def test_cancel_e_invoice_also_cancels_ewb(self):
		self.generated()
		self.svc.generate_e_waybill(NAME, ROAD, confirm=True)
		self.svc.cancel_e_invoice(NAME, "data entry mistake", "wrong rate", confirm=True)
		inv = self.inv()
		self.assertEqual(inv["aitsgst_einvoice_status"], "Cancelled")
		self.assertEqual(inv["aitsgst_einvoice_cancel_reason"], "Data Entry Mistake: wrong rate")
		self.assertEqual(inv["aitsgst_einvoice_cancelled_by"], "tester@example.com")
		self.assertEqual(inv["aitsgst_ewb_status"], "Cancelled")
		self.assertEqual(self.cloud_inv()["docstatus"], 1)  # the invoice itself is never cancelled by the app

	def test_cancel_window_and_reason(self):
		self.generated()
		self.cloud.docs["e-Invoice Log"][IRN]["acknowledged_on"] = "2026-10-06 09:00:00"  # > 24h before fakes.NOW
		with self.assertRaises(Blocked) as ctx:
			self.svc.cancel_e_invoice(NAME, "Others", None, confirm=True)
		text = " ".join(ctx.exception.problems)
		self.assertIn("within 24 hours", text)
		self.assertIn("remark when the reason is Others", text)
		self.assertEqual(self.cloud.count("call", f"{EINV}.cancel_e_invoice"), 0)

	def test_cancel_requires_confirmation(self):
		self.generated()
		self.assertRaises(ConfirmationRequired, self.svc.cancel_e_invoice, NAME, "Duplicate", None, False)
		self.assertEqual(self.cloud_inv()["einvoice_status"], "Generated")

	def test_cancel_ambiguous_but_done(self):
		self.generated()
		self.cloud.fail("call", f"{EINV}.cancel_e_invoice", fakes.timeout(), after=True)
		self.assertEqual(self.svc.cancel_e_invoice(NAME, "Duplicate", None, confirm=True)["outcome"], "Cancelled")

	def test_cancel_ewb(self):
		self.svc.generate_e_waybill(NAME, ROAD, confirm=True)
		self.svc.cancel_e_waybill(NAME, "Order Cancelled", None, confirm=True)
		self.assertEqual(self.inv()["aitsgst_ewb_status"], "Cancelled")
		self.assertIsNone(self.inv().get("aitsgst_einvoice_status"))  # e-invoice untouched


class TestSyncBack(Base):
	def test_cancel_made_on_cloud_reflects_locally(self):
		self.generated()
		self.cloud_inv()["einvoice_status"] = "Cancelled"
		self.cloud.docs["e-Invoice Log"][IRN].update(cancel_reason_code="Duplicate", cancelled_on="2026-10-07 11:50:00")
		self.assertTrue(self.svc.refresh(NAME)["changed"])
		inv = self.inv()
		self.assertEqual(inv["aitsgst_einvoice_status"], "Cancelled")
		self.assertEqual(inv["aitsgst_einvoice_cancel_reason"], "Duplicate")
		self.assertEqual(inv["aitsgst_einvoice_cancelled_by"], "cloud")
		self.assertFalse(self.svc.refresh(NAME)["changed"])

	def test_ewb_generated_on_cloud_reflects_locally(self):
		self.cloud.methods[f"{EWB}.generate_e_waybill"](doctype="Sales Invoice", docname=CLOUD, values={"vehicle_no": "GJ01AA0001"})
		self.svc.refresh(NAME)
		self.assertEqual((self.inv()["aitsgst_ewaybill"], self.inv()["aitsgst_vehicle_no"]), ("331000000001", "GJ01AA0001"))
