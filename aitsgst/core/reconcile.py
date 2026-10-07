"""Compares what the cloud computed with the local invoice: grand total and each GST
type (CGST / SGST / IGST / CESS), in company currency, within 0.05.

Ported from the SAP B1 Web Portal's ErpNextReconciler (there the reference was
SAP's INV4; here it is the local ERPNext invoice's own tax rows).
"""

TOLERANCE = 0.05


def classify(account_head: str | None, description: str | None = None) -> str:
	"""GST account names carry the tax type anywhere ("3300012 - IGST on Sales - TTCPL")."""
	for text in (account_head, description):
		t = (text or "").upper()
		if "IGST" in t:
			return "IGST"
		if "CGST" in t:
			return "CGST"
		if "SGST" in t or "UTGST" in t:
			return "SGST"
		if "CESS" in t:
			return "CESS"
	return "Other"


def tax_summary(doc: dict) -> dict:
	sums = {}
	for row in doc.get("taxes") or []:
		kind = classify(row.get("account_head"), row.get("description"))
		amount = row.get("base_tax_amount")
		if amount is None:
			amount = row.get("tax_amount") or 0
		sums[kind] = sums.get(kind, 0) + float(amount or 0)
	return sums


def reconcile(local: dict, cloud: dict) -> tuple[str, str, float | None]:
	"""Returns (status "Match"|"Mismatch", detail, cloud grand total)."""
	differences, notes = [], []

	local_grand = float(local.get("grand_total") or 0)
	local_rounded = float(local.get("rounded_total") or 0)
	cloud_grand = cloud.get("grand_total")
	cloud_rounded = float(cloud.get("rounded_total") or 0)

	total_ok = cloud_grand is not None and abs(float(cloud_grand) - local_grand) <= TOLERANCE
	# Rounding settings may differ between sites: accept rounded-vs-rounded too.
	total_ok = total_ok or (local_rounded and cloud_rounded and abs(cloud_rounded - local_rounded) <= TOLERANCE)
	if total_ok:
		notes.append(f"Total: local {local_grand:.2f} = cloud {float(cloud_grand or 0):.2f}.")
	else:
		cloud_text = f"{float(cloud_grand):.2f}" if cloud_grand is not None else "n/a"
		differences.append(f"Total: local {local_grand:.2f} vs cloud {cloud_text}.")

	local_tax, cloud_tax = tax_summary(local), tax_summary(cloud)
	for kind in sorted(set(("CGST", "SGST", "IGST", "CESS")) | set(local_tax) | set(cloud_tax)):
		lv, cv = local_tax.get(kind, 0.0), cloud_tax.get(kind, 0.0)
		if lv == 0 and cv == 0:
			continue
		if abs(lv - cv) > TOLERANCE:
			differences.append(f"{kind}: local {lv:.2f} vs cloud {cv:.2f}.")
		else:
			notes.append(f"{kind}: local {lv:.2f} = cloud {cv:.2f}.")

	cloud_total = float(cloud_grand) if cloud_grand is not None else None
	if differences:
		return "Mismatch", f"Differences over {TOLERANCE:.2f}: " + " ".join(differences), cloud_total
	return "Match", " ".join(notes), cloud_total
