"""Masks secrets before anything is stored in AITS GST Sync Log or an error message."""

import json
import re

MASK = "***"
_SENSITIVE_KEY = re.compile(r"secret|password|passwd|token|api_?key|authorization|signature|cookie|sid", re.I)
_TOKEN_HEADER = re.compile(r"(token|bearer|basic)\s+[^\s\"',]+", re.I)
_MAX_LOG_CHARS = 60_000


def mask(value, _depth=0):
	"""Returns a deep copy with sensitive keys replaced and auth headers scrubbed from strings."""
	if _depth > 20:
		return MASK
	if isinstance(value, dict):
		return {
			k: (MASK if isinstance(k, str) and _SENSITIVE_KEY.search(k) and v not in (None, "") else mask(v, _depth + 1))
			for k, v in value.items()
		}
	if isinstance(value, list | tuple):
		return [mask(v, _depth + 1) for v in value]
	if isinstance(value, str):
		return _TOKEN_HEADER.sub(lambda m: f"{m.group(1)} {MASK}", value)
	return value


def mask_text(text: str | None, *secrets: str | None) -> str | None:
	"""Scrubs auth headers plus any literal secret values from free text."""
	if text is None:
		return None
	text = mask(str(text))
	for secret in secrets:
		if secret and len(secret) >= 4:
			text = text.replace(secret, MASK)
	return text


def to_log_json(value) -> str | None:
	if value is None:
		return None
	text = json.dumps(mask(value), indent=1, default=str, sort_keys=True)
	if len(text) > _MAX_LOG_CHARS:
		text = text[:_MAX_LOG_CHARS] + "\n... (truncated)"
	return text
