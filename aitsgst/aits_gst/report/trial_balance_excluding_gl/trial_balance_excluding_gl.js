// Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Trial Balance Excluding GL"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "fiscal_year",
			label: __("Fiscal Year"),
			fieldtype: "Link",
			options: "Fiscal Year",
			default: frappe.defaults.get_user_default("fiscal_year"),
			reqd: 1,
			on_change: function (query_report) {
				var fiscal_year = query_report.get_values().fiscal_year;
				if (!fiscal_year) {
					return;
				}
				frappe.model.with_doc("Fiscal Year", fiscal_year, function () {
					var fy = frappe.model.get_doc("Fiscal Year", fiscal_year);
					if (fy && fy.year_start_date && fy.year_end_date) {
						frappe.query_report.set_filter_value({
							from_date: fy.year_start_date,
							to_date: fy.year_end_date,
						});
					}
				});
			},
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "exclude_accounts",
			label: __("Exclude Accounts"),
			fieldtype: "MultiSelectList",
			description: __("Selected accounts (and all their child accounts) are removed from the report and its totals"),
			get_data: function (txt) {
				return frappe.db.get_link_options("Account", txt, {
					company: frappe.query_report.get_filter_value("company"),
				});
			},
		},
		{
			fieldname: "cost_center",
			label: __("Cost Center"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				return frappe.db.get_link_options("Cost Center", txt, {
					company: frappe.query_report.get_filter_value("company"),
				});
			},
		},
		{
			fieldname: "project",
			label: __("Project"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				return frappe.db.get_link_options("Project", txt, {
					company: frappe.query_report.get_filter_value("company"),
				});
			},
		},
		{
			fieldname: "finance_book",
			label: __("Finance Book"),
			fieldtype: "Link",
			options: "Finance Book",
		},
		{
			fieldname: "presentation_currency",
			label: __("Currency"),
			fieldtype: "Select",
			options: erpnext.get_presentation_currency_list(),
		},
		{
			fieldname: "with_period_closing_entry_for_opening",
			label: __("With Period Closing Entry For Opening Balances"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "with_period_closing_entry_for_current_period",
			label: __("Period Closing Entry For Current Period"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "show_zero_values",
			label: __("Show zero values"),
			fieldtype: "Check",
		},
		{
			fieldname: "show_unclosed_fy_pl_balances",
			label: __("Show unclosed fiscal year's P&L balances"),
			fieldtype: "Check",
		},
		{
			fieldname: "include_default_book_entries",
			label: __("Include Default FB Entries"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "show_net_values",
			label: __("Show net values in opening and closing columns"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "show_group_accounts",
			label: __("Show Group Accounts"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "only_gl_accounts",
			label: __("Only GL Accounts (Exclude Groups)"),
			fieldtype: "Check",
			default: 0,
			depends_on: "eval:!doc.show_group_accounts",
			description: __("When checked, only leaf-level GL accounts will be shown (no group/subgroup totals)"),
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		// Bold root accounts and the Total row
		if (data && !data.parent_account && data.account) {
			value = $(`<span>${value}</span>`).css("font-weight", "bold").wrap("<p></p>").parent().html();
		}
		return value;
	},
	tree: true,
	name_field: "account",
	parent_field: "parent_account",
	initial_depth: 3,
};

erpnext.utils.add_dimensions("Trial Balance Excluding GL", 6);
