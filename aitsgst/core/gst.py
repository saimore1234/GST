"""GSTIN format check and the fixed GST state-code list.

Ported from the SAP B1 Web Portal (GstinValidator / GstStates) so both
integrations accept and reject exactly the same values.
"""

import re

_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_HSN = re.compile(r"^(\d{4}|\d{6}|\d{8})$")

STATES = {
	"01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
	"05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan",
	"09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
	"13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
	"17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand",
	"21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
	"26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
	"30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
	"34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
	"37": "Andhra Pradesh", "38": "Ladakh", "96": "Other Countries", "97": "Other Territory",
}


def normalize_gstin(gstin: str | None) -> str | None:
	if not gstin or not str(gstin).strip():
		return None
	return str(gstin).strip().upper()


def is_valid_gstin(gstin: str | None) -> bool:
	"""Structure only (15 chars, PAN in the middle, 'Z' in position 14) - not a registry lookup."""
	g = normalize_gstin(gstin)
	return bool(g and _GSTIN.match(g))


def state_code_of_gstin(gstin: str | None) -> str | None:
	return normalize_gstin(gstin)[:2] if is_valid_gstin(gstin) else None


def is_valid_place_of_supply(value: str | None) -> bool:
	"""India Compliance stores place_of_supply as "24-Gujarat"."""
	if not value or "-" not in value:
		return False
	code, _, name = value.partition("-")
	return STATES.get(code) == name


def hsn_digits(code: str | None) -> str | None:
	"""SAP often stores HSN formatted ("7210.41.00"); the GST portal wants digits only."""
	if not code:
		return None
	cleaned = re.sub(r"[\s.\-]", "", str(code))
	return cleaned or None


def is_standard_hsn(code: str | None) -> bool:
	return bool(code and _HSN.match(code))
