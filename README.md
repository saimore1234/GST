## AITS GST

Pushes submitted Sales Invoices from a **local** (self-hosted) ERPNext site to a **cloud**
ERPNext site, generates the e-invoice (IRN) and e-way bill **on the cloud site** through
India Compliance there, and writes the results back onto the local Sales Invoice:
IRN, Ack No / Date, signed QR (rendered as an image), e-way bill no / date / validity,
vehicle details and cancel status.

```
SAP B1 --(sap_b1_integration)--> LOCAL ERPNext Sales Invoice
                                     |  AITS GST: push (draft), then user-confirmed actions
                                     v
                               CLOUD ERPNext + India Compliance --> GST portal (IRN, e-way bill)
                                     |
                                     +--> results written back to the LOCAL invoice (poll / webhook)
```

Requires Frappe / ERPNext v15 and India Compliance on both sites.

### Install (local bench)

```bash
cd ~/frappe-bench
bench get-app <git-url-of-this-repo>      # or: copy into apps/ and `./env/bin/pip install -e apps/aitsgst`
bench --site <local-site> install-app aitsgst
bench --site <local-site> migrate          # also after every update of this app
bench restart                              # production setups (supervisor) only
```

`install-app` creates the DocTypes, the **AITS GST Manager** role, the `aitsgst_*` custom
fields on Sales Invoice (from `fixtures/`) and the **AITS GST e-Invoice** print format.
Make sure the scheduler is enabled (`bench --site <local-site> enable-scheduler`).

### Configure

1. **Cloud site** (one-time, by its administrator):
   - Create an integration user (e.g. `aitsgst@yourco`) with roles *Accounts User*, *Sales User*,
     *Stock User* (to read UOM / Stock Settings) and permission to read *GST Settings*,
     *e-Invoice Log*, *e-Waybill Log*. Generate its API key / secret.
   - Custom field **`sap_b1_key`** (Data, read only, no copy) on *Sales Invoice* (also *Unique*),
     *Customer*, *Address* and *Item*. If the SAP B1 Web Portal was already set up for this site
     (its "setup custom fields" action), these exist already.
   - **Customize Form → Sales Invoice → tick "Allow Rename"**. The cloud names new invoices from its own
     series, but India Compliance uses the invoice name as the IRN / e-way bill *Document No.*, so the app
     renames each cloud draft to the local invoice number before it is submitted. Without this, Prepare /
     Generate stops with a clear message and nothing is submitted.
   - India Compliance GST Settings: API enabled, e-Invoice / e-Waybill enabled, credentials for
     the company GSTIN, and **Sandbox Mode ON** until the checklist below passes.
2. **Local site → AITS GST Settings** (System Manager):
   - GST Service URL (the cloud site root, https only), GST Service API Key / Secret (stored encrypted in a Password field).
   - Local Site Code (used in keys; cannot change after the first push).
   - Companies: local company → GST Service Company (the cloud company name), company GSTIN, optional SAP Company Code
     (then invoices from SAP use the same key as the SAP B1 Web Portal: `{code}|{DocEntry}`).
   - Tax Template Map: only where template names differ between local and cloud.
   - Tick **"I confirm the GST service is a TEST / SANDBOX setup"**. Until this (or "Allow production")
     is ticked, the app makes **no call of any kind** to the cloud.
   - Use **Test Connection** (read-only checks) and fix anything red.
3. Give users the **AITS GST Manager** role.

> **Local India Compliance:** IRNs must be generated only on the cloud. If this local site's
> GST Settings has the API enabled for e-Invoice / e-Waybill, the settings page shows a warning:
> disable them locally, or the same invoice could be registered twice.

### Use

On a submitted local Sales Invoice, **e-Invoice** menu. End users never see the word "cloud": in all
labels, buttons and messages the cloud site is called **the GST service** and its copy of the invoice
**the e-invoice record**; internal fields (cloud invoice name, key, totals check) are hidden on the form
and visible in AITS GST Sync Log.

| Button | What happens |
|---|---|
| Prepare e-Invoice | Background job. Creates a **draft** on cloud (never submits). Looks up the key first, so it never duplicates; creates missing Customer / Address / Item only if enabled; copies tax rows from the cloud's own templates; reconciles totals (Match / Mismatch). |
| Generate e-Invoice | Confirm dialog. Submits the cloud draft, calls India Compliance there, writes IRN / Ack / QR back. Refused on totals mismatch, B2C, missing credentials, or production mode without "Allow production". |
| Generate e-Way Bill | Confirm dialog with transport details (pre-filled from the invoice). |
| Update Vehicle (Part-B) | Confirm dialog; only while an e-way bill is active. |
| Cancel e-Invoice / e-Way Bill | Confirm dialog with reason; only within 24 h. Cancels only the IRN / e-way bill. |
| **Cancel Invoice (with IRN / e-Way Bill)** | Confirm dialog. Checks first (24 h window, reason, linked payments on cloud and here, production gate); if anything fails, nothing is cancelled. Then: IRN + e-way bill on the GST portal → cloud invoice (a cloud draft is deleted) → this invoice. If it stops part-way it says which steps were done; run it again to continue. |
| Refresh e-Invoice Status | Re-reads the cloud invoice and its logs. |

Status badges show on the form; IRN No, Ack No / Date, **e-Invoice QR Code** (an image field,
`aitsgst_qr_image`, a private PNG generated on the local server), e-Way Bill No and validity are on tab
**e-Invoice**. Print with **AITS GST e-Invoice**, or use in any print format:
`<img src="{{ doc.aitsgst_qr_image }}">` and `{{ doc.aitsgst_irn }}`, `{{ doc.aitsgst_ewaybill }}`.

**Cancelling:** the normal Cancel button is refused while the IRN, e-way bill or cloud invoice is still
active - use **Cancel Invoice (with IRN / e-Way Bill)**. When an invoice is cancelled (or deleted) **on
the cloud site**, the status sync marks it, notifies AITS GST Managers and shows a red banner with a
"Cancel this invoice" button. With *Auto-cancel invoice when its e-invoice record is cancelled* (off by
default) the local invoice is cancelled automatically instead.

**Automation (all off or read-only by default):** optional auto-prepare on submit; failed pushes caused by
network errors / cloud 5xx are retried at 5, 10, 20, 40, 80 minutes (Max Retries); the
cloud is polled every *Status Sync Interval* minutes for changes made there (cancel, vehicle update).
Optionally add a cloud **Webhook** (Sales Invoice, on update / on cancel, *Enable Security* with the
same secret as *Webhook Secret*) to `https://<local-site>/api/method/aitsgst.api.status_webhook`; it
only triggers a re-read. Nothing is generated, submitted or cancelled automatically.

**Several client companies on one GST service:** create one Company per client on the GST service and run
**AITS GST Settings → Set up GST Service Fields** (or add by hand a Link field `aitsgst_company` → Company on
Customer, Item and Address). From then on every Customer, Item and Address the app creates is tagged with the
client's company, and only that company's records are ever matched or changed: two clients selling to the
same buyer each get their own Customer (ERPNext names the second "<name> - 1"); an item code already used by
another company becomes "<company abbr>-<code>". Give each client its own GST service API user with **User
Permission Company = <its company>** (Apply To All Document Types), so a client's key cannot see other clients'
data. Without the field, masters are shared (fine for a single client).

**GSTIN autofill** (setting *Fill party details from GSTIN*, on by default): entering a GSTIN on a
Customer, Supplier or Address - in the full form **and** in India Compliance's "+ Add" quick-entry popup -
fills the legal name, GST category and registered address, looked up through the GST service (so the
local site needs no India Compliance API account). Existing values are never overwritten; each GSTIN is
cached for 24 hours. After updating the app run `bench build --app aitsgst` once, so its browser script
is served.

Every action is recorded in **AITS GST Sync Log** (request / response with secrets masked).

### Tests

```bash
bench --site <local-site> set-config allow_tests true   # if not already
bench --site <local-site> run-tests --app aitsgst
```

The suite never calls the network (the cloud is an in-memory fake; `requests` is patched to fail),
never creates Sales Invoices, and never commits.

See [SANDBOX_TEST_CHECKLIST.md](SANDBOX_TEST_CHECKLIST.md) before production.

### License

MIT
