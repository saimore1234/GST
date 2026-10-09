"""REST client for the GST service / Frappe v15 API.

Ported from the SAP B1 Web Portal's ErpNextClient:
  * auth is "Authorization: token {api_key}:{api_secret}" on every request;
  * the credentials never appear in a log line or an exception message;
  * Frappe error bodies (_server_messages / exception / exc_type) are reduced
    to one short, tag-free line before they are surfaced.

Reads (GET) are retried twice on transient failures. Writes are NEVER retried
here: a timeout on a write is ambiguous (the cloud may have saved it), so the
services re-read the cloud and decide, instead of blindly sending again.
"""

import json
import re
import time
from urllib.parse import quote, urlparse

import requests

from aitsgst.core.masking import mask_text

_MAX_MESSAGE = 500
_HTML_TAG = re.compile(r"<[^>]*>")
_TRANSIENT_STATUS = {429, 502, 503, 504}


class CloudError(Exception):
	"""A failed cloud call. `message` is sanitised and safe to show to users.

	`ambiguous` is True when the cloud may still have acted on the request
	(timeout, dropped connection, 5xx) - callers must re-read before reporting failure.
	"""

	def __init__(self, message: str, status_code: int | None = None, ambiguous: bool = False):
		super().__init__(message)
		self.message = message
		self.status_code = status_code
		self.ambiguous = ambiguous

	@property
	def transient(self) -> bool:
		return self.ambiguous or self.status_code in _TRANSIENT_STATUS


class CloudClient:
	def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: int = 60,
	             session: requests.Session | None = None, sleep=time.sleep, read_retries: int = 2):
		parsed = urlparse(base_url or "")
		if parsed.scheme != "https" or not parsed.hostname or parsed.path.strip("/"):
			raise CloudError("The GST service URL must be an https site root, e.g. https://yoursite.m.erpnext.com")
		if not api_key or not api_secret:
			raise CloudError("The GST service API key / secret are not configured.")

		self.base_url = base_url.rstrip("/")
		self._auth = f"token {api_key}:{api_secret}"
		self._secrets = (api_key, api_secret)
		self.timeout = timeout
		self.session = session or requests.Session()
		self._sleep = sleep
		self._read_retries = read_retries

	def __repr__(self):
		return f"<CloudClient {self.base_url}>"  # never the credentials

	# ------------------------------------------------------------------ generic
	def get(self, path: str, params: dict | None = None):
		"""Parsed JSON body, or None for 404."""
		attempt = 0
		while True:
			try:
				status, body = self._send("GET", path, params=params)
				if status in _TRANSIENT_STATUS and attempt < self._read_retries:
					raise CloudError(f"GST service is busy ({status}).", status, ambiguous=True)
				break
			except CloudError as e:
				if not e.transient or attempt >= self._read_retries:
					raise
				attempt += 1
				self._sleep(2 ** (attempt - 1))
		if status == 404:
			return None
		return self._unwrap(status, body)

	def get_pdf(self, path: str, params: dict | None = None) -> bytes:
		"""A PDF body (e.g. a print). Read-only, but not retried: a slow render is not a flaky one."""
		response = self._request("GET", path, params=params, accept="application/pdf")
		content = response.content or b""
		if 200 <= response.status_code < 300 and content.startswith(b"%PDF"):
			return content
		if 200 <= response.status_code < 300:
			raise CloudError("GST service did not return a PDF.", response.status_code)
		self._unwrap(response.status_code, response.text or "")
		raise CloudError(f"GST service error ({response.status_code}).", response.status_code)

	def post(self, path: str, body: dict):
		status, text = self._send("POST", path, json_body=body)
		return self._unwrap(status, text, require_body=True)

	def put(self, path: str, body: dict):
		status, text = self._send("PUT", path, json_body=body)
		return self._unwrap(status, text, require_body=True)

	# ------------------------------------------------------------- doc helpers
	def get_doc(self, doctype: str, name: str) -> dict | None:
		node = self.get(f"/api/resource/{quote(doctype, safe='')}/{quote(name, safe='')}")
		return (node or {}).get("data") if node is not None else None

	def get_list(self, doctype: str, filters=None, fields=("name",), limit: int = 20, order_by: str | None = None) -> list:
		params = {"fields": json.dumps(list(fields)), "limit_page_length": str(limit)}
		if filters:
			params["filters"] = json.dumps(filters)
		if order_by:
			params["order_by"] = order_by
		node = self.get(f"/api/resource/{quote(doctype, safe='')}", params)
		return (node or {}).get("data") or []

	def insert(self, doctype: str, doc: dict) -> dict:
		node = self.post(f"/api/resource/{quote(doctype, safe='')}", doc)
		data = node.get("data")
		if not data:
			raise CloudError(f"GST service returned no document for the new {doctype}.")
		return data

	def update(self, doctype: str, name: str, values: dict) -> dict:
		node = self.put(f"/api/resource/{quote(doctype, safe='')}/{quote(name, safe='')}", values)
		return node.get("data") or {}

	def call(self, method: str, **kwargs):
		"""POST a whitelisted method; returns its `message`."""
		node = self.post(f"/api/method/{method}", kwargs)
		return node.get("message")

	def call_get(self, method: str, **params):
		node = self.get(f"/api/method/{method}", {k: v for k, v in params.items() if v is not None})
		return (node or {}).get("message")

	# ----------------------------------------------------------------- transport
	def _send(self, method: str, path: str, params=None, json_body=None) -> tuple[int, str]:
		response = self._request(method, path, params=params, json_body=json_body)
		return response.status_code, response.text or ""

	def _request(self, method: str, path: str, params=None, json_body=None, accept: str = "application/json"):
		url = self.base_url + path
		headers = {"Authorization": self._auth, "Accept": accept}
		try:
			response = self.session.request(
				method, url, params=params, json=json_body, headers=headers, timeout=self.timeout, allow_redirects=False
			)
		except requests.Timeout:
			raise CloudError("GST service did not respond in time.", ambiguous=True)
		except requests.exceptions.SSLError:
			raise CloudError("The GST service's HTTPS certificate could not be verified. Check the GST Service URL.")
		except requests.ConnectionError as e:
			if _is_name_resolution_error(e):
				# Nothing was sent, so this is not ambiguous.
				raise CloudError(f"Host name '{urlparse(url).hostname}' was not found. Check the GST Service URL "
				                 ".")
			# The connection may have dropped after the request was sent, so this is ambiguous for writes.
			raise CloudError("Could not reach the GST service. Check the URL and network access.", ambiguous=True)
		except requests.RequestException as e:
			raise CloudError(f"Request to GST service failed ({type(e).__name__}).")

		if 300 <= response.status_code < 400:
			# Never follow redirects: one could downgrade to http or leak the token to another host.
			raise CloudError(f"GST service redirected the request ({response.status_code}). Check the GST Service URL.",
			                 response.status_code)
		return response

	def _unwrap(self, status: int, body: str, require_body: bool = False):
		if 200 <= status < 300:
			if not body.strip():
				if require_body:
					raise CloudError("GST service returned an empty response.", status)
				return None
			try:
				return json.loads(body)
			except ValueError:
				raise CloudError("GST service returned a response that is not valid JSON.", status)

		message = self._clean(extract_message(body))
		if status == 401:
			raise CloudError("GST service rejected the API key/secret (401). Check AITS GST Settings.", status)
		if status == 403:
			raise CloudError(f"GST service denied permission for the API user (403). {message}", status)
		raise CloudError(f"GST service error ({status}): {message}", status, ambiguous=status >= 500)

	def _clean(self, text: str) -> str:
		return mask_text(text, *self._secrets)


def _is_name_resolution_error(error: Exception) -> bool:
	text = str(error)
	return any(s in text for s in ("NameResolutionError", "Name or service not known", "nodename nor servname",
	                               "getaddrinfo failed", "Temporary failure in name resolution"))


def extract_message(body: str) -> str:
	"""Reduces a Frappe error body to one short plain-text line."""
	if not body or not body.strip():
		return "no detail returned."
	try:
		root = json.loads(body)
		parts = []
		raw = root.get("_server_messages") if isinstance(root, dict) else None
		if isinstance(raw, str):
			for item in json.loads(raw):
				try:
					msg = json.loads(item).get("message") if isinstance(item, str) else None
				except ValueError:
					msg = item
				if msg:
					parts.append(str(msg))
		if isinstance(root, dict):
			for key in ("exception", "message", "exc_type"):
				if not parts and isinstance(root.get(key), str) and root.get(key):
					parts.append(root[key])
		if parts:
			return _plain(" ".join(parts))
	except (ValueError, TypeError, AttributeError):
		pass  # not JSON (e.g. an HTML error page) - fall through to stripped raw text
	return _plain(body)


def _plain(text: str) -> str:
	text = re.sub(r"\s+", " ", _HTML_TAG.sub(" ", text)).strip()
	return text if len(text) <= _MAX_MESSAGE else text[:_MAX_MESSAGE] + "..."
