import json
import unittest

import requests

from aitsgst.core.cloud_client import CloudClient, CloudError, extract_message
from aitsgst.core.masking import mask, mask_text, to_log_json

KEY, SECRET = "k3y-abcdef", "s3cret-123456"


class FakeResponse:
	def __init__(self, status, body=""):
		self.status_code = status
		self.text = body if isinstance(body, str) else json.dumps(body)


class FakeSession:
	"""Records every request; returns queued responses (or raises queued exceptions)."""

	def __init__(self, *responses):
		self.responses = list(responses)
		self.calls = []

	def request(self, method, url, **kwargs):
		self.calls.append({"method": method, "url": url, **kwargs})
		nxt = self.responses.pop(0)
		if isinstance(nxt, Exception):
			raise nxt
		return nxt


def client(*responses, **kw):
	session = FakeSession(*responses)
	return CloudClient("https://cloud.example.com/", KEY, SECRET, session=session, sleep=lambda s: None, **kw), session


class TestCloudClient(unittest.TestCase):
	def test_rejects_non_https_and_paths(self):
		for url in ("http://cloud.example.com", "https://cloud.example.com/app", "", "cloud.example.com"):
			with self.assertRaises(CloudError, msg=url):
				CloudClient(url, KEY, SECRET)
		with self.assertRaises(CloudError):
			CloudClient("https://cloud.example.com", "", SECRET)

	def test_sends_token_auth_and_no_redirects(self):
		c, s = client(FakeResponse(200, {"data": {"name": "SINV-1"}}))
		self.assertEqual(c.get_doc("Sales Invoice", "SINV-1"), {"name": "SINV-1"})
		call = s.calls[0]
		self.assertEqual(call["url"], "https://cloud.example.com/api/resource/Sales%20Invoice/SINV-1")
		self.assertEqual(call["headers"]["Authorization"], f"token {KEY}:{SECRET}")
		self.assertFalse(call["allow_redirects"])

	def test_doc_name_is_url_escaped(self):
		c, s = client(FakeResponse(404, ""))
		self.assertIsNone(c.get_doc("Customer", "A/B & Co?"))
		self.assertTrue(s.calls[0]["url"].endswith("/api/resource/Customer/A%2FB%20%26%20Co%3F"))

	def test_repr_and_errors_never_contain_secret(self):
		c, _ = client(FakeResponse(500, f"Traceback ... token {KEY}:{SECRET} ... {SECRET}"))
		self.assertNotIn(SECRET, repr(c))
		with self.assertRaises(CloudError) as ctx:
			c.post("/api/method/x", {})
		self.assertNotIn(SECRET, ctx.exception.message)
		self.assertNotIn(KEY, ctx.exception.message)
		self.assertTrue(ctx.exception.ambiguous)

	def test_401_and_403(self):
		c, _ = client(FakeResponse(401, "{}"))
		with self.assertRaises(CloudError) as ctx:
			c.get("/api/method/frappe.auth.get_logged_user")
		self.assertIn("401", ctx.exception.message)
		self.assertFalse(ctx.exception.transient)

		c, _ = client(FakeResponse(403, {"exc_type": "PermissionError"}))
		with self.assertRaises(CloudError) as ctx:
			c.get("/x")
		self.assertIn("PermissionError", ctx.exception.message)

	def test_get_retries_transient_then_succeeds(self):
		c, s = client(requests.Timeout(), FakeResponse(503, ""), FakeResponse(200, {"message": "ok"}))
		self.assertEqual(c.call_get("ping"), "ok")
		self.assertEqual(len(s.calls), 3)

	def test_get_gives_up_after_retries(self):
		c, s = client(requests.Timeout(), requests.Timeout(), requests.Timeout())
		with self.assertRaises(CloudError) as ctx:
			c.get("/x")
		self.assertTrue(ctx.exception.ambiguous)
		self.assertEqual(len(s.calls), 3)

	def test_post_is_never_retried(self):
		c, s = client(requests.Timeout(), FakeResponse(200, {"message": "should not be reached"}))
		with self.assertRaises(CloudError) as ctx:
			c.call("india_compliance.gst_india.utils.e_invoice.generate_e_invoice", docname="SINV-1")
		self.assertTrue(ctx.exception.ambiguous)
		self.assertEqual(len(s.calls), 1)

	def test_validation_error_is_not_ambiguous(self):
		c, _ = client(FakeResponse(417, {"exc_type": "ValidationError", "_server_messages": json.dumps([json.dumps({"message": "<b>HSN</b> is mandatory"})])}))
		with self.assertRaises(CloudError) as ctx:
			c.insert("Sales Invoice", {})
		self.assertEqual(ctx.exception.message, "GST service error (417): HSN is mandatory")
		self.assertFalse(ctx.exception.ambiguous)

	def test_connection_errors_are_specific(self):
		dns = requests.ConnectionError("Failed to resolve 'x.frappe.cloud.com' ([Errno -2] Name or service not known)")
		c, _ = client(dns, dns, dns)
		with self.assertRaises(CloudError) as ctx:
			c.get("/x")
		self.assertIn("was not found", ctx.exception.message)
		self.assertFalse(ctx.exception.ambiguous)  # nothing was sent

		c, _ = client(requests.exceptions.SSLError("certificate verify failed"))
		with self.assertRaises(CloudError) as ctx:
			c.post("/x", {})
		self.assertIn("certificate", ctx.exception.message)

		c, _ = client(requests.ConnectionError("Connection reset by peer"))
		with self.assertRaises(CloudError) as ctx:
			c.post("/x", {})
		self.assertTrue(ctx.exception.ambiguous)  # may have been received: services re-read

	def test_redirect_is_refused(self):
		c, _ = client(FakeResponse(301, ""))
		with self.assertRaises(CloudError):
			c.get("/x")

	def test_get_list_encodes_filters(self):
		c, s = client(FakeResponse(200, {"data": [{"name": "SINV-9"}]}))
		rows = c.get_list("Sales Invoice", [["sap_b1_key", "=", "A|1"]], ["name"], 1)
		self.assertEqual(rows, [{"name": "SINV-9"}])
		self.assertEqual(json.loads(s.calls[0]["params"]["filters"]), [["sap_b1_key", "=", "A|1"]])

	def test_invalid_json_and_empty_write_body(self):
		c, _ = client(FakeResponse(200, "<html>"))
		self.assertRaises(CloudError, c.get, "/x")
		c, _ = client(FakeResponse(200, ""))
		self.assertRaises(CloudError, c.post, "/x", {})


class TestExtractMessage(unittest.TestCase):
	def test_server_messages(self):
		body = json.dumps({"_server_messages": json.dumps([json.dumps({"message": "A"}), json.dumps({"message": "B"})])})
		self.assertEqual(extract_message(body), "A B")

	def test_html_page_and_length(self):
		self.assertEqual(extract_message("<html><body><h1>Bad   Gateway</h1></body></html>"), "Bad Gateway")
		self.assertTrue(extract_message("x" * 900).endswith("..."))
		self.assertEqual(extract_message(""), "no detail returned.")


class TestMasking(unittest.TestCase):
	def test_mask_keys_recursively(self):
		data = {"api_key": "abc", "nested": [{"api_secret": "xyz", "irn": "keep"}], "Authorization": "token a:b", "empty_password": ""}
		masked = mask(data)
		self.assertEqual(masked["api_key"], "***")
		self.assertEqual(masked["nested"][0]["api_secret"], "***")
		self.assertEqual(masked["nested"][0]["irn"], "keep")
		self.assertEqual(masked["Authorization"], "***")
		self.assertEqual(data["api_key"], "abc")  # original untouched

	def test_mask_text_scrubs_headers_and_literals(self):
		self.assertEqual(mask_text("sent token abc:def ok"), "sent token *** ok")
		self.assertEqual(mask_text("leaked s3cret-123456 here", "s3cret-123456"), "leaked *** here")

	def test_log_json(self):
		self.assertIn('"***"', to_log_json({"password": "p"}))
		self.assertIsNone(to_log_json(None))
