# AITS GST - sandbox test checklist (before production)

Run this against a **cloud test site** whose India Compliance GST Settings are in **Sandbox Mode**,
with the **sandbox test GSTIN** listed in India Compliance's documentation for the cloud's
India Compliance version. Use a local test company mapped to that GSTIN.
Tick in AITS GST Settings: *I confirm the GST service is a TEST / SANDBOX setup*. Leave *Allow production* OFF.

Record the local invoice name, cloud invoice name and Sync Log entries for each step.

## A. Setup
- [ ] Test Connection: all green (credentials, cloud company, GST Settings sandbox, key field on 4 doctypes, mapped templates).
- [ ] Local GST Settings: e-Invoice / e-Waybill API disabled (no warning on AITS GST Settings).
- [ ] A user WITHOUT AITS GST Manager sees no Cloud GST buttons; calling the API returns "not permitted".

## B. Push
- [ ] B2B intra-state invoice (CGST+SGST): push -> cloud **draft**, Customer / Addresses / Items created once, reconciliation **Match**.
- [ ] B2B inter-state invoice (IGST): same; cloud place of supply = local.
- [ ] Push the same invoice again -> "AlreadyPushed", nothing new on cloud.
- [ ] Invoice with a header discount and one with rounding -> Match (or explained Mismatch).
- [ ] Unmapped tax template on cloud -> **Blocked** with a clear message; nothing created on cloud.
- [ ] Draft (unsubmitted) local invoice -> Blocked.
- [ ] Disconnect network during push (or wrong URL) -> Failed + Retry Scheduled; restore -> retried automatically, exactly one cloud invoice.
- [ ] (If SAP Company Code set) invoice already pushed by the SAP B1 Web Portal -> **Adopted**, not duplicated.

## C. E-invoice
- [ ] Generate without ticking the confirmation -> not possible.
- [ ] Generate -> cloud invoice submitted, IRN / Ack No / Ack Date / QR on the local invoice; QR visible on form and in the **AITS GST e-Invoice** print; scan the QR - data matches the invoice.
- [ ] Generate again -> "AlreadyGenerated", no second call.
- [ ] B2C invoice -> Blocked (no GSTIN).
- [ ] Force a Mismatch (change cloud template rate) -> e-invoice Blocked.
- [ ] Try to cancel the LOCAL invoice while the IRN is live -> refused with instructions.

## D. E-way bill
- [ ] Generate (Road, vehicle no, distance 0) -> e-way bill no, date, valid upto, vehicle on local invoice and print.
- [ ] Road without vehicle and without transporter ID -> Blocked; invalid vehicle no -> Blocked.
- [ ] Update Vehicle (Part-B) with reason, place, state -> new vehicle on local; valid upto refreshed.
- [ ] Rail/Air/Ship without LR no/date -> Blocked.

## E. Cancel & sync-back
- [ ] Cancel e-way bill (reason) -> local status Cancelled, reason/by/on recorded.
- [ ] Cancel e-invoice with an active e-way bill -> both Cancelled locally; QR hidden and "IRN CANCELLED" on print.
- [ ] Cancel attempt older than 24 h (use an old sandbox IRN) -> Blocked with the window shown.
- [ ] Cancel an IRN **directly on the cloud** -> within the sync interval (or via webhook) the local invoice shows Cancelled, "by cloud".
- [ ] Update a vehicle directly on the cloud -> reflected locally.
- [ ] Cloud invoice deleted -> push shows Blocked "no longer exists", nothing recreated.

## F. Safety
- [ ] Turn cloud GST Settings Sandbox Mode OFF -> every e-invoice / e-way bill / cancel action is Blocked ("PRODUCTION ... Allow production is off").
- [ ] Sync Log entries contain no API key / secret / Authorization header.
- [ ] `git grep -i -E "secret|api_key"` in the app shows no real credentials.
- [ ] GST Service URL `http://...` is rejected.
- [ ] As a normal user, the Sales Invoice form, dialogs, messages and timeline never mention "cloud" or show the cloud invoice name.

## Go-live
Only after A-F pass: on the **production** cloud site's GST Settings turn Sandbox Mode off with the
real GSTIN credentials, then tick **Allow production** in AITS GST Settings. Start with one invoice.
