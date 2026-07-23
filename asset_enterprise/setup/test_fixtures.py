"""Throwaway asset fixtures for smoke verification.

ALWAYS call inside a savepoint that the caller rolls back — these are
never meant to persist on a live site.
"""

import frappe
from frappe.utils import add_months, nowdate


def _find_account(company, account_type, root_type=None):
	filters = {"company": company, "account_type": account_type, "is_group": 0}
	if root_type:
		filters["root_type"] = root_type
	return frappe.db.get_value("Account", filters, "name")


def make_test_asset(company, gross=100000, submit=False, with_depreciation=False):
	"""Create Asset Category + Item + Asset for smoke tests. Returns Asset doc."""
	fixed_asset_account = _find_account(company, "Fixed Asset")
	accum_account = _find_account(company, "Accumulated Depreciation")
	depr_expense = _find_account(company, "Depreciation") or _find_account(
		company, "Expense Account", "Expense"
	)
	if not (fixed_asset_account and accum_account and depr_expense):
		frappe.throw(
			f"CoA for {company} lacks Fixed Asset / Accumulated Depreciation / "
			f"Depreciation accounts — cannot build smoke fixture."
		)

	if not frappe.db.exists("Asset Category", "AE Smoke Category"):
		frappe.get_doc(
			{
				"doctype": "Asset Category",
				"asset_category_name": "AE Smoke Category",
				"accounts": [
					{
						"company_name": company,
						"fixed_asset_account": fixed_asset_account,
						"accumulated_depreciation_account": accum_account,
						"depreciation_expense_account": depr_expense,
					}
				],
			}
		).insert(ignore_permissions=True)

	item_code = "AE-SMOKE-ITEM"
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "AE Smoke Fixed Asset Item",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"is_fixed_asset": 1,
				"is_stock_item": 0,
				"asset_category": "AE Smoke Category",
			}
		).insert(ignore_permissions=True)

	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"item_code": item_code,
			"asset_name": "AE Smoke Asset",
			"asset_category": "AE Smoke Category",
			"location": _ensure_location(),
			"is_existing_asset": 1,
			"purchase_amount": gross,
			"net_purchase_amount": gross,
			"opening_accumulated_depreciation": 0,
			"available_for_use_date": add_months(nowdate(), -1),
			"purchase_date": add_months(nowdate(), -1),
			"calculate_depreciation": 1 if with_depreciation else 0,
		}
	)
	if with_depreciation:
		asset.append(
			"finance_books",
			{
				"depreciation_method": "Straight Line",
				"total_number_of_depreciations": 24,
				"frequency_of_depreciation": 1,
				"depreciation_start_date": nowdate(),
				"daily_prorata_based": 1,
			},
		)
	asset.flags.ignore_permissions = True
	asset.insert()
	if submit:
		asset.submit()
	return asset


def _ensure_location():
	loc = frappe.db.get_value("Location", {}, "name")
	if loc:
		return loc
	return (
		frappe.get_doc({"doctype": "Location", "location_name": "AE Smoke Location"})
		.insert(ignore_permissions=True)
		.name
	)
