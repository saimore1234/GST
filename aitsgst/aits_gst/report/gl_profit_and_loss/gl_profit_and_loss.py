# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Profit & Loss that can show only GL (leaf) accounts.

All figures come from ERPNext's standard Profit and Loss Statement: its execute() builds the
rows, the Total Income / Total Expense rows, Profit for the year, the chart, the report summary
and the Growth / Margin views exactly as the standard report does. This report only removes
group-account rows from what is displayed, so every amount, total and the net profit always
reconcile with the standard report for the same filters.
"""

import frappe
from frappe.utils import nowdate

from erpnext.accounts.report.profit_and_loss_statement.profit_and_loss_statement import (
	execute as standard_execute,
)
from erpnext.accounts.utils import get_fiscal_year


def execute(filters=None):
	filters = frappe._dict(filters or {})
	set_default_fiscal_years(filters)

	columns, data, message, chart, report_summary, primitive_summary = standard_execute(filters)

	if show_only_gl_accounts(filters):
		data = get_gl_account_rows(data)

	return columns, data, message, chart, report_summary, primitive_summary


def show_only_gl_accounts(filters):
	# "Show Group Accounts" unchecked hides the groups as well
	return bool(filters.get("only_gl_accounts")) or not filters.get("show_group_accounts", 1)


def get_gl_account_rows(data):
	"""Drop group and subgroup account rows; keep leaf accounts and the standard total rows.

	Account rows from financial_statements.prepare_data always carry `is_group`; the rows the
	standard report adds (Total Income / Total Expense, the blank separators and Profit for the
	year) never do, so they are kept and still hold the standard totals.
	"""
	rows = []
	for row in data:
		if row and "is_group" in row:
			if row.get("is_group"):
				continue
			# Without their group rows the tree would nest leaves under each other by indent
			row["indent"] = 0
		rows.append(row)
	return rows


def set_default_fiscal_years(filters):
	if filters.get("from_fiscal_year") and filters.get("to_fiscal_year"):
		return

	# boolean=True: returns the matching fiscal years (or False) instead of throwing
	fiscal_years = get_fiscal_year(nowdate(), company=filters.get("company"), as_dict=True, boolean=True)
	if not fiscal_years:
		frappe.throw(frappe._("Please select From Fiscal Year and To Fiscal Year."))

	filters.from_fiscal_year = filters.get("from_fiscal_year") or fiscal_years[0].name
	filters.to_fiscal_year = filters.get("to_fiscal_year") or fiscal_years[0].name
