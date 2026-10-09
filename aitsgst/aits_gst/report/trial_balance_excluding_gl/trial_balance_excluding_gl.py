# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, cstr, flt, formatdate, getdate

import erpnext
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
	get_accounting_dimensions,
	get_dimension_with_children,
)
from erpnext.accounts.report.financial_statements import (
	filter_accounts,
	filter_out_zero_value_rows,
	get_cost_centers_with_children,
	set_gl_entries_by_account,
)
from erpnext.accounts.report.utils import convert_to_presentation_currency, get_currency
from erpnext.accounts.utils import get_zero_cutoff

value_fields = (
	"opening_debit",
	"opening_credit",
	"debit",
	"credit",
	"closing_debit",
	"closing_credit",
)


def execute(filters=None):
	if not filters:
		filters = frappe._dict()

	# Set default fiscal year if not provided
	if not filters.get("fiscal_year"):
		# Try to get default fiscal year for the company
		fiscal_years = frappe.db.get_all(
			"Fiscal Year",
			filters={"company": filters.get("company"), "is_default": 1},
			fields=["name", "year_start_date", "year_end_date"],
		)

		if not fiscal_years and filters.get("company"):
			# Get any fiscal year for the company
			fiscal_years = frappe.db.get_all(
				"Fiscal Year",
				filters={"company": filters.get("company")},
				fields=["name", "year_start_date", "year_end_date"],
				limit=1,
			)

		if fiscal_years:
			filters["fiscal_year"] = fiscal_years[0]["name"]
			if not filters.get("from_date"):
				filters["from_date"] = fiscal_years[0]["year_start_date"]
			if not filters.get("to_date"):
				filters["to_date"] = fiscal_years[0]["year_end_date"]

	validate_filters(filters)
	filters.excluded_accounts = get_excluded_accounts(filters)
	data = get_data(filters)
	columns = get_columns()
	return columns, data


def validate_filters(filters):
	if not filters.get("fiscal_year"):
		frappe.throw(_("Fiscal Year is required. Please select a Fiscal Year."))

	fiscal_year = frappe.get_cached_value(
		"Fiscal Year", filters.get("fiscal_year"), ["year_start_date", "year_end_date"], as_dict=True
	)
	if not fiscal_year:
		frappe.throw(_("Fiscal Year {0} does not exist").format(filters.get("fiscal_year")))
	else:
		filters.year_start_date = getdate(fiscal_year.year_start_date)
		filters.year_end_date = getdate(fiscal_year.year_end_date)

	if not filters.get("from_date"):
		filters.from_date = filters.year_start_date

	if not filters.get("to_date"):
		filters.to_date = filters.year_end_date

	filters.from_date = getdate(filters.from_date)
	filters.to_date = getdate(filters.to_date)

	if filters.from_date > filters.to_date:
		frappe.throw(_("From Date cannot be greater than To Date"))

	if (filters.from_date < filters.year_start_date) or (filters.from_date > filters.year_end_date):
		frappe.msgprint(
			_("From Date should be within the Fiscal Year. Assuming From Date = {0}").format(
				formatdate(filters.year_start_date)
			)
		)
		filters.from_date = filters.year_start_date

	if (filters.to_date < filters.year_start_date) or (filters.to_date > filters.year_end_date):
		frappe.msgprint(
			_("To Date should be within the Fiscal Year. Assuming To Date = {0}").format(
				formatdate(filters.year_end_date)
			)
		)
		filters.to_date = filters.year_end_date


def get_excluded_accounts(filters):
	"""Return the set of account names to drop: each selected account plus all its descendants."""
	selected = filters.get("exclude_accounts")
	if not selected:
		return set()

	if isinstance(selected, str):
		selected = frappe.parse_json(selected) if selected.startswith("[") else selected.split(",")

	selected = [cstr(a).strip() for a in selected if cstr(a).strip()]
	if not selected:
		return set()

	excluded = set()
	for acc in frappe.get_all(
		"Account",
		filters={"name": ("in", selected), "company": filters.company},
		fields=["name", "lft", "rgt"],
	):
		excluded.update(
			frappe.get_all(
				"Account",
				filters={"company": filters.company, "lft": (">=", acc.lft), "rgt": ("<=", acc.rgt)},
				pluck="name",
			)
		)

	return excluded


def get_data(filters):
	accounts = frappe.db.sql(
		"""select name, account_number, parent_account, account_name, root_type, report_type, is_group, lft, rgt
		from `tabAccount` where company=%s order by lft""",
		filters.company,
		as_dict=True,
	)
	company_currency = filters.presentation_currency or erpnext.get_company_currency(filters.company)

	ignore_is_opening = frappe.db.get_single_value(
		"Accounts Settings", "ignore_is_opening_check_for_reporting"
	)

	# Drop excluded accounts (and their children) before building the tree,
	# so they don't appear as rows and don't roll up into parent or total balances
	if filters.excluded_accounts:
		accounts = [acc for acc in accounts if acc.name not in filters.excluded_accounts]

	if not accounts:
		return []

	accounts, accounts_by_name, parent_children_map = filter_accounts(accounts)

	# Filter out group accounts if only_gl_accounts is checked
	if filters.get("only_gl_accounts"):
		# Only keep leaf accounts (is_group = 0)
		accounts = [acc for acc in accounts if not acc.get("is_group")]
		# Rebuild accounts_by_name with only GL accounts
		accounts_by_name = {acc["name"]: acc for acc in accounts}
		# Rebuild parent_children_map to maintain proper structure for GL accounts only
		parent_children_map = {}
		for acc in accounts:
			parent = acc.get("parent_account")
			if parent:
				parent_children_map.setdefault(parent, []).append(acc)
			else:
				parent_children_map.setdefault(None, []).append(acc)

	gl_entries_by_account = {}

	opening_balances = get_opening_balances(filters, ignore_is_opening)

	set_gl_entries_by_account(
		filters.company,
		filters.from_date,
		filters.to_date,
		filters,
		gl_entries_by_account,
		root_lft=None,
		root_rgt=None,
		ignore_closing_entries=not flt(filters.with_period_closing_entry_for_current_period),
		ignore_opening_entries=True,
		group_by_account=True,
	)

	calculate_values(
		accounts,
		gl_entries_by_account,
		opening_balances,
		filters.get("show_net_values"),
		ignore_is_opening=ignore_is_opening,
	)

	# Only accumulate values if we're showing group accounts
	# For only_gl_accounts, we skip accumulation to avoid parent totals
	if not filters.get("only_gl_accounts"):
		accumulate_values_into_parents(accounts, accounts_by_name)

	data = prepare_data(accounts, filters, parent_children_map, company_currency)

	# Filter out zero values if needed
	if not filters.get("show_zero_values"):
		data = filter_out_zero_value_rows(
			data, parent_children_map, show_zero_values=filters.get("show_zero_values")
		)

	return data


def get_opening_balances(filters, ignore_is_opening):
	balance_sheet_opening = get_rootwise_opening_balances(filters, "Balance Sheet", ignore_is_opening)
	pl_opening = get_rootwise_opening_balances(filters, "Profit and Loss", ignore_is_opening)

	balance_sheet_opening.update(pl_opening)
	return balance_sheet_opening


def get_rootwise_opening_balances(filters, report_type, ignore_is_opening):
	gle = []

	last_period_closing_voucher = ""
	ignore_closing_balances = frappe.db.get_single_value(
		"Accounts Settings", "ignore_account_closing_balance"
	)

	if not ignore_closing_balances:
		last_period_closing_voucher = frappe.db.get_all(
			"Period Closing Voucher",
			filters={"docstatus": 1, "company": filters.company, "period_end_date": ("<", filters.from_date)},
			fields=["period_end_date", "name"],
			order_by="period_end_date desc",
			limit=1,
		)

	accounting_dimensions = get_accounting_dimensions(as_list=False)

	if last_period_closing_voucher:
		gle = get_opening_balance(
			"Account Closing Balance",
			filters,
			report_type,
			accounting_dimensions,
			period_closing_voucher=last_period_closing_voucher[0].name,
			ignore_is_opening=ignore_is_opening,
		)

		# Report getting generate from the mid of a fiscal year
		if getdate(last_period_closing_voucher[0].period_end_date) < getdate(add_days(filters.from_date, -1)):
			start_date = add_days(last_period_closing_voucher[0].period_end_date, 1)
			gle += get_opening_balance(
				"GL Entry",
				filters,
				report_type,
				accounting_dimensions,
				start_date=start_date,
				ignore_is_opening=ignore_is_opening,
			)
	else:
		gle = get_opening_balance(
			"GL Entry", filters, report_type, accounting_dimensions, ignore_is_opening=ignore_is_opening
		)

	opening = frappe._dict()
	for d in gle:
		opening.setdefault(
			d.account,
			{
				"account": d.account,
				"opening_debit": 0.0,
				"opening_credit": 0.0,
			},
		)
		opening[d.account]["opening_debit"] += flt(d.debit)
		opening[d.account]["opening_credit"] += flt(d.credit)

	return opening


def get_opening_balance(
	doctype,
	filters,
	report_type,
	accounting_dimensions,
	period_closing_voucher=None,
	start_date=None,
	ignore_is_opening=0,
):
	closing_balance = frappe.qb.DocType(doctype)

	account_filters = {"company": filters.company, "report_type": report_type}
	# Apply only_gl_accounts filter if set
	if filters.get("only_gl_accounts"):
		account_filters["is_group"] = 0

	accounts = frappe.db.get_all("Account", filters=account_filters, pluck="name")

	# Skip excluded accounts in the opening balance query
	if filters.get("excluded_accounts"):
		accounts = [acc for acc in accounts if acc not in filters.excluded_accounts]

	if not accounts:
		return []

	opening_balance = (
		frappe.qb.from_(closing_balance)
		.select(
			closing_balance.account,
			closing_balance.account_currency,
			Sum(closing_balance.debit).as_("debit"),
			Sum(closing_balance.credit).as_("credit"),
			Sum(closing_balance.debit_in_account_currency).as_("debit_in_account_currency"),
			Sum(closing_balance.credit_in_account_currency).as_("credit_in_account_currency"),
		)
		.where((closing_balance.company == filters.company) & (closing_balance.account.isin(accounts)))
		.groupby(closing_balance.account)
	)

	if period_closing_voucher:
		opening_balance = opening_balance.where(
			closing_balance.period_closing_voucher == period_closing_voucher
		)
	else:
		if start_date:
			opening_balance = opening_balance.where(
				(closing_balance.posting_date >= start_date)
				& (closing_balance.posting_date < filters.from_date)
			)

			if not ignore_is_opening:
				opening_balance = opening_balance.where(closing_balance.is_opening == "No")
		else:
			if not ignore_is_opening:
				opening_balance = opening_balance.where(
					(closing_balance.posting_date < filters.from_date) | (closing_balance.is_opening == "Yes")
				)
			else:
				opening_balance = opening_balance.where(closing_balance.posting_date < filters.from_date)

	if doctype == "GL Entry":
		opening_balance = opening_balance.where(closing_balance.is_cancelled == 0)

	if (
		not filters.get("show_unclosed_fy_pl_balances")
		and report_type == "Profit and Loss"
		and doctype == "GL Entry"
	):
		opening_balance = opening_balance.where(closing_balance.posting_date >= filters.year_start_date)

	if not flt(filters.get("with_period_closing_entry_for_opening")):
		if doctype == "Account Closing Balance":
			opening_balance = opening_balance.where(closing_balance.is_period_closing_voucher_entry == 0)
		else:
			opening_balance = opening_balance.where(closing_balance.voucher_type != "Period Closing Voucher")

	if filters.get("cost_center"):
		opening_balance = opening_balance.where(
			closing_balance.cost_center.isin(get_cost_centers_with_children(filters.get("cost_center")))
		)

	if filters.get("project"):
		opening_balance = opening_balance.where(closing_balance.project.isin(filters.get("project")))

	if frappe.db.count("Finance Book"):
		if filters.get("include_default_book_entries"):
			company_fb = frappe.get_cached_value("Company", filters.company, "default_finance_book")

			if filters.get("finance_book") and company_fb and cstr(filters.get("finance_book")) != cstr(company_fb):
				frappe.throw(
					_("To use a different finance book, please uncheck 'Include Default FB Entries'")
				)

			opening_balance = opening_balance.where(
				(closing_balance.finance_book.isin([cstr(filters.get("finance_book")), cstr(company_fb), ""]))
				| (closing_balance.finance_book.isnull())
			)
		else:
			opening_balance = opening_balance.where(
				(closing_balance.finance_book.isin([cstr(filters.get("finance_book")), ""]))
				| (closing_balance.finance_book.isnull())
			)

	if accounting_dimensions:
		for dimension in accounting_dimensions:
			if filters.get(dimension.fieldname):
				if frappe.get_cached_value("DocType", dimension.document_type, "is_tree"):
					filters[dimension.fieldname] = get_dimension_with_children(
						dimension.document_type, filters.get(dimension.fieldname)
					)
					opening_balance = opening_balance.where(
						closing_balance[dimension.fieldname].isin(filters[dimension.fieldname])
					)
				else:
					opening_balance = opening_balance.where(
						closing_balance[dimension.fieldname].isin(filters[dimension.fieldname])
					)

	gle = opening_balance.run(as_dict=1)

	if filters and filters.get("presentation_currency"):
		convert_to_presentation_currency(gle, get_currency(filters))

	return gle


def calculate_values(accounts, gl_entries_by_account, opening_balances, show_net_values, ignore_is_opening=0):
	init = {
		"opening_debit": 0.0,
		"opening_credit": 0.0,
		"debit": 0.0,
		"credit": 0.0,
		"closing_debit": 0.0,
		"closing_credit": 0.0,
	}

	for d in accounts:
		d.update(init.copy())

		# add opening
		d["opening_debit"] = opening_balances.get(d.name, {}).get("opening_debit", 0)
		d["opening_credit"] = opening_balances.get(d.name, {}).get("opening_credit", 0)

		for entry in gl_entries_by_account.get(d.name, []):
			if cstr(entry.is_opening) != "Yes" or ignore_is_opening:
				d["debit"] += flt(entry.debit)
				d["credit"] += flt(entry.credit)

		d["closing_debit"] = d["opening_debit"] + d["debit"]
		d["closing_credit"] = d["opening_credit"] + d["credit"]

		if show_net_values:
			prepare_opening_closing(d)


def calculate_total_row(accounts, company_currency):
	total_row = {
		"account": "'" + _("Total") + "'",
		"account_name": "'" + _("Total") + "'",
		"warn_if_negative": True,
		"opening_debit": 0.0,
		"opening_credit": 0.0,
		"debit": 0.0,
		"credit": 0.0,
		"closing_debit": 0.0,
		"closing_credit": 0.0,
		"parent_account": None,
		"indent": 0,
		"has_value": True,
		"currency": company_currency,
	}

	for d in accounts:
		if not d.get("parent_account"):
			for field in value_fields:
				total_row[field] += d.get(field, 0.0)

	return total_row


def accumulate_values_into_parents(accounts, accounts_by_name):
	for d in reversed(accounts):
		if d.get("parent_account"):
			for key in value_fields:
				if d.get(key):
					accounts_by_name[d.parent_account][key] = accounts_by_name[d.parent_account].get(key, 0.0) + d[key]


def prepare_data(accounts, filters, parent_children_map, company_currency):
	data = []

	for d in accounts:
		# Prepare opening closing for group account
		if parent_children_map.get(d.get("account")) and filters.get("show_net_values"):
			prepare_opening_closing(d)

		has_value = False
		row = {
			"account": d.name,
			"parent_account": d.parent_account,
			"indent": d.indent,
			"from_date": filters.from_date,
			"to_date": filters.to_date,
			"currency": company_currency,
			"is_group_account": d.is_group,
			"account_name": (
				f"{d.account_number} - {d.account_name}" if d.get("account_number") else d.account_name
			),
		}

		for key in value_fields:
			row[key] = flt(d.get(key, 0.0))

			if abs(row[key]) >= get_zero_cutoff(company_currency):
				# ignore zero values
				has_value = True

		row["has_value"] = has_value
		data.append(row)

	# Only add total row if not filtering for only GL accounts
	if not filters.get("only_gl_accounts"):
		total_row = calculate_total_row(accounts, company_currency)
		if not filters.get("show_group_accounts"):
			data = hide_group_accounts(data)
		data.extend([{}, total_row])
	else:
		# For only GL accounts, don't add total row and don't hide group accounts
		if not filters.get("show_group_accounts"):
			data = hide_group_accounts(data)

	return data


def get_columns():
	return [
		{
			"fieldname": "account",
			"label": _("Account"),
			"fieldtype": "Link",
			"options": "Account",
			"width": 300,
		},
		{
			"fieldname": "currency",
			"label": _("Currency"),
			"fieldtype": "Link",
			"options": "Currency",
			"hidden": 1,
		},
		{
			"fieldname": "opening_debit",
			"label": _("Opening (Dr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "opening_credit",
			"label": _("Opening (Cr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "debit",
			"label": _("Debit"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "credit",
			"label": _("Credit"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "closing_debit",
			"label": _("Closing (Dr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"fieldname": "closing_credit",
			"label": _("Closing (Cr)"),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
	]


def prepare_opening_closing(row):
	dr_or_cr = "debit" if row.get("root_type") in ["Asset", "Equity", "Expense"] else "credit"
	reverse_dr_or_cr = "credit" if dr_or_cr == "debit" else "debit"

	for col_type in ["opening", "closing"]:
		valid_col = col_type + "_" + dr_or_cr
		reverse_col = col_type + "_" + reverse_dr_or_cr
		row[valid_col] = row.get(valid_col, 0.0) - row.get(reverse_col, 0.0)
		if row[valid_col] < 0:
			row[reverse_col] = abs(row[valid_col])
			row[valid_col] = 0.0
		else:
			row[reverse_col] = 0.0


def hide_group_accounts(data):
	non_group_accounts_data = []
	for d in data:
		if not d.get("is_group_account"):
			d.update(indent=0)
			non_group_accounts_data.append(d)
	return non_group_accounts_data
