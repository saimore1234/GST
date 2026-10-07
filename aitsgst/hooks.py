app_name = "aitsgst"
app_title = "AITS GST"
app_publisher = "AITS"
app_description = "Push local ERPNext Sales Invoices to a cloud ERPNext site for e-invoice / e-way bill, and sync the results back"
app_email = "admin@aitsind.com"
app_license = "mit"

required_apps = ["erpnext", "india_compliance"]

before_install = "aitsgst.install.before_install"
after_install = "aitsgst.install.after_install"

doctype_js = {"Sales Invoice": "public/js/sales_invoice.js"}

doc_events = {
	"Sales Invoice": {
		"on_submit": "aitsgst.events.on_submit",
		"before_cancel": "aitsgst.events.before_cancel",
	}
}

# Every 5 minutes: due push retries; the status poll itself runs at the interval set in AITS GST Settings.
scheduler_events = {
	"cron": {
		"*/5 * * * *": ["aitsgst.tasks.run_due_jobs"],
	}
}

jinja = {"methods": ["aitsgst.utils.qr.get_qr_data_uri"]}

# Custom fields on Sales Invoice and the AITS GST Manager role ship as fixtures,
# so `bench migrate` keeps them in sync on every site the app is installed on.
fixtures = [
	{"dt": "Custom Field", "filters": [["module", "=", "AITS GST"]]},
	{"dt": "Role", "filters": [["name", "=", "AITS GST Manager"]]},
]
