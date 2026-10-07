from aitsgst.services.compliance import EINV, EWB, Blocked, ConfirmationRequired
from aitsgst.tests import fakes
from aitsgst.tests.test_compliance import CLOUD, IRN, NAME, ROAD, Base

LINKED = "frappe.desk.form.linked_with.get_submitted_linked_docs"


class TestCancelEverywhere(Base):
	def setUp(self):
		super().setUp()
		docs = self.cloud.docs

		def cancel(doctype, name):
			assert not (docs[doctype][name].get("irn") and docs[doctype][name].get("einvoice_status") == "Generated"), \
				"India Compliance refuses to cancel an invoice with an active IRN"
			docs[doctype][name]["docstatus"] = 2

		def delete(doctype, name):
			assert docs[doctype][name]["docstatus"] == 0
			del docs[doctype][name]

		self.cloud_links = []
		self.cloud.methods.update({
			"frappe.client.cancel": cancel,
			"frappe.client.delete": delete,
			LINKED: lambda doctype, name: {"docs": self.cloud_links, "count": len(self.cloud_links)},
		})

	def everything(self):
		self.generated()
		self.svc.generate_e_waybill(NAME, ROAD, confirm=True)

	def cancel(self, **kw):
		kw.setdefault("reason", "Data Entry Mistake")
		kw.setdefault("confirm", True)
		return self.svc.cancel_everywhere(NAME, **kw)

	def portal_calls(self):
		return [c for c in self.cloud.calls if c[0] == "call" and "cancel_e_" in c[1]]

	# ------------------------------------------------------------ happy paths
	def test_cancels_irn_ewb_cloud_and_local_in_order(self):
		self.everything()
		result = self.cancel()
		self.assertEqual(result["outcome"], "Cancelled")
		self.assertEqual(len(result["steps"]), 3)
		# one portal call: India Compliance cancels the e-way bill together with the IRN
		self.assertEqual(self.portal_calls(), [("call", f"{EINV}.cancel_e_invoice")])
		order = [c[1] for c in self.cloud.calls if c[0] == "call" and c[1] in (f"{EINV}.cancel_e_invoice", "frappe.client.cancel")]
		self.assertEqual(order, [f"{EINV}.cancel_e_invoice", "frappe.client.cancel"])
		self.assertEqual(self.cloud_inv()["docstatus"], 2)

		inv = self.inv()
		self.assertEqual(inv["docstatus"], 2)
		self.assertEqual((inv["aitsgst_einvoice_status"], inv["aitsgst_ewb_status"], inv["aitsgst_cloud_docstatus"]),
		                 ("Cancelled", "Cancelled", "Cancelled"))
		self.assertEqual(inv["aitsgst_einvoice_cancel_reason"], "Data Entry Mistake")
		self.assertEqual(self.log.last()["status"], "Success")

	def test_ewb_only(self):
		self.svc.generate_e_waybill(NAME, ROAD, confirm=True)
		self.cancel(reason="Order Cancelled")
		self.assertEqual(self.portal_calls(), [("call", f"{EWB}.cancel_e_waybill")])
		self.assertEqual(self.inv()["aitsgst_ewb_status"], "Cancelled")
		self.assertEqual(self.inv()["docstatus"], 2)

	def test_no_irn_needs_no_reason(self):
		self.cloud_inv()["docstatus"] = 1
		self.svc.cancel_everywhere(NAME, confirm=True)
		self.assertEqual((self.cloud_inv()["docstatus"], self.inv()["docstatus"]), (2, 2))
		self.assertEqual(self.portal_calls(), [])

	def test_cloud_draft_is_deleted(self):
		self.svc.cancel_everywhere(NAME, confirm=True)
		self.assertNotIn(CLOUD, self.cloud.docs["Sales Invoice"])
		self.assertEqual(self.inv()["aitsgst_cloud_docstatus"], "Deleted")
		self.assertEqual(self.inv()["docstatus"], 2)

	def test_cloud_invoice_already_gone(self):
		del self.cloud.docs["Sales Invoice"][CLOUD]
		self.svc.cancel_everywhere(NAME, confirm=True)
		self.assertEqual(self.inv()["docstatus"], 2)

	# ------------------------------------------------- nothing done when blocked
	def assert_nothing_done(self):
		self.assertEqual(self.portal_calls(), [])
		self.assertEqual(self.cloud.count("call", "frappe.client.cancel") + self.cloud.count("call", "frappe.client.delete"), 0)
		self.assertEqual(self.inv()["docstatus"], 1)

	def test_requires_confirmation(self):
		self.everything()
		with self.assertRaises(ConfirmationRequired) as ctx:
			self.cancel(confirm=False)
		self.assertIn("cancel the IRN and e-way bill", " ".join(ctx.exception.problems))
		self.assert_nothing_done()

	def test_irn_older_than_24h_blocks_everything(self):
		self.everything()
		self.cloud.docs["e-Invoice Log"][IRN]["acknowledged_on"] = "2026-10-06 09:00:00"
		with self.assertRaises(Blocked) as ctx:
			self.cancel()
		self.assertIn("credit note", " ".join(ctx.exception.problems))
		self.assert_nothing_done()

	def test_reason_required_when_portal_involved(self):
		self.everything()
		with self.assertRaises(Blocked):
			self.cancel(reason=None)
		self.assert_nothing_done()

	def test_local_payment_blocks_before_cloud(self):
		self.everything()
		self.store.local_problems = ["Locally, X has submitted documents linked to it (Payment Entry PE-1)."]
		with self.assertRaises(Blocked) as ctx:
			self.cancel()
		self.assertIn("Payment Entry", " ".join(ctx.exception.problems))
		self.assert_nothing_done()

	def test_cloud_payment_blocks(self):
		self.everything()
		self.cloud_links = [{"doctype": "Payment Entry", "name": "ACC-PAY-1"}, {"doctype": "GL Entry", "name": "ignored"}]
		with self.assertRaises(Blocked) as ctx:
			self.cancel()
		text = " ".join(ctx.exception.problems)
		self.assertIn("Payment Entry ACC-PAY-1", text)
		self.assertNotIn("GL Entry", text)
		self.assert_nothing_done()

	def test_production_cloud_blocked(self):
		self.everything()
		self.cloud.docs["GST Settings"]["GST Settings"]["sandbox_mode"] = 0
		with self.assertRaises(Blocked):
			self.cancel()
		self.assert_nothing_done()

	# ------------------------------------------------------- part-way failures
	def test_cloud_cancel_fails_after_irn_then_rerun_continues(self):
		self.everything()
		self.cloud.fail("call", "frappe.client.cancel", fakes.validation("Account frozen"))
		with self.assertRaises(Blocked) as ctx:
			self.cancel()
		text = " ".join(ctx.exception.problems)
		self.assertIn("Done: IRN cancelled", text)
		self.assertIn("Account frozen", text)
		inv = self.inv()
		self.assertEqual(inv["docstatus"], 1)  # local NOT cancelled
		self.assertEqual(inv["aitsgst_einvoice_status"], "Cancelled")  # but the IRN state is recorded truthfully

		result = self.cancel()  # run again: IRN step skipped
		self.assertEqual(len(self.portal_calls()), 1)
		self.assertEqual(result["steps"][0], "e-Invoice record closed")
		self.assertEqual(self.inv()["docstatus"], 2)

	def test_ambiguous_cloud_cancel_that_succeeded(self):
		self.everything()
		self.cloud.fail("call", "frappe.client.cancel", fakes.timeout(), after=True)
		self.assertEqual(self.cancel()["outcome"], "Cancelled")

	def test_local_cancel_failure_is_reported(self):
		self.everything()
		self.store.cancel_error = RuntimeError("Closing period")
		with self.assertRaises(Blocked) as ctx:
			self.cancel()
		self.assertIn("Invoice cancel failed: Closing period", " ".join(ctx.exception.problems))
		self.assertEqual(self.cloud_inv()["docstatus"], 2)
		self.assertEqual(self.inv()["aitsgst_cloud_docstatus"], "Cancelled")
		self.assertEqual(self.log.last()["status"], "Failed")


class TestCloudCancelSyncBack(Base):
	def test_cloud_cancel_triggers_local_handler_once(self):
		self.inv()["aitsgst_cloud_docstatus"] = "Submitted"
		self.cloud_inv()["docstatus"] = 2
		self.svc.refresh(NAME)
		self.assertEqual(self.store.cloud_cancelled_calls, (NAME,))
		self.assertEqual(self.inv()["aitsgst_cloud_docstatus"], "Cancelled")
		self.svc.refresh(NAME)
		self.assertEqual(self.store.cloud_cancelled_calls, (NAME,))  # not again

	def test_no_trigger_when_local_already_cancelled(self):
		self.inv().update(aitsgst_cloud_docstatus="Submitted", docstatus=2)
		self.cloud_inv()["docstatus"] = 2
		self.svc.refresh(NAME)
		self.assertEqual(self.store.cloud_cancelled_calls, ())
