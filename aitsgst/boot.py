def set_bootinfo(bootinfo):
	"""Tells the browser whether GSTIN lookups go through the GST service (used by the quick-entry popups)."""
	from aitsgst.api import _gstin_autofill_enabled

	try:
		bootinfo["aitsgst_gstin_autofill"] = _gstin_autofill_enabled()
	except Exception:
		bootinfo["aitsgst_gstin_autofill"] = False  # never break login over this
