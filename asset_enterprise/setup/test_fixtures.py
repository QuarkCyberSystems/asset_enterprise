"""Throwaway asset fixtures for smoke verification.

ALWAYS call inside a savepoint that the caller rolls back — these are
never meant to persist on a live site.
"""

import contextlib

import frappe
from frappe.utils import add_months, nowdate

# asset_values.recalculate_asset_values sources operational values from
# the legacy fold or from posted GL, per site_config
# asset_enterprise_gl_values_ready. A check whose expectation belongs to
# one source runs under value_mode(<that source>) so its verdict does not
# depend on how the site running it is configured.
LEGACY, GL = "legacy", "gl"
_MODE_FLAG = {LEGACY: 0, GL: 1}


@contextlib.contextmanager
def value_mode(mode):
	"""Run the body with asset values sourced as `mode` (LEGACY or GL),
	restoring the site's own setting afterwards (in-process only; site_config.json is
	never written)."""
	key = "asset_enterprise_gl_values_ready"
	had, before = key in frappe.conf, frappe.conf.get(key)
	frappe.conf[key] = _MODE_FLAG[mode]
	try:
		yield
	finally:
		if had:
			frappe.conf[key] = before
		else:
			frappe.conf.pop(key, None)


def pick_company():
	"""Deterministic company for smoke/UAT runs: prefer Badia Cement
	(multi-company sites carry unrelated test companies)."""
	return (
		frappe.db.get_value("Company", {"company_name": ("like", "%Badia%")}, "name")
		or frappe.db.get_value("Company", {}, "name")
	)


def pick_plain_account(company, root_type):
	"""Non-group account of the given root type that a plain JE can
	post to (no party/stock/bank semantics) — live CoAs list
	receivables first."""
	rows = frappe.db.sql(
		"""select name from tabAccount where company = %s and root_type = %s
		   and is_group = 0 and ifnull(account_type, '') not in
		   ('Receivable', 'Payable', 'Stock', 'Bank', 'Cash',
		    'Depreciation', 'Accumulated Depreciation', 'Fixed Asset')
		   limit 1""",
		(company, root_type),
	)
	return rows[0][0] if rows else None


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

	# GAP-001: submitting an existing asset posts the suspense JE — seed
	# a company default so smoke fixtures submit cleanly (savepoint-only).
	if not frappe.db.get_value("Company", company, "default_asset_suspense_account"):
		suspense = pick_plain_account(company, "Liability")
		frappe.db.set_value(
			"Company", company, "default_asset_suspense_account", suspense, update_modified=False
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


def ensure_enterprise_test_defaults():
	"""Idempotent defaults for a disposable test site, never an install hook."""
	company = pick_company()
	if not company:
		frappe.throw("Create the test company before seeding enterprise defaults.")
	centres = frappe.get_all("Cost Center", filters={"company": company, "is_group": 0}, pluck="name")
	if len(centres) < 2:
		parent = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
		frappe.get_doc({
			"doctype": "Cost Center", "company": company,
			"cost_center_name": "AE Test Transfer", "parent_cost_center": parent,
			"is_group": 0,
		}).insert(ignore_permissions=True)
	for field, root in (
		("pya_expense_account", "Expense"),
		("asset_invoice_difference_account", "Expense"),
		("post_disposal_invoice_diff_account", "Expense"),
	):
		field = "default_" + field
		if not frappe.db.get_value("Company", company, field):
			account = pick_plain_account(company, root)
			if not account:
				frappe.throw(f"Test company {company} lacks a plain {root} account")
			frappe.db.set_value("Company", company, field, account)
	return company


def dimension_fixture(fieldname, company, doctype="Asset Movement Item"):
	"""Create an isolated project in the caller's rollback-only savepoint."""
	link = frappe.get_meta(doctype).get_field(fieldname)
	if not link:
		return None
	if link.options == "Project Accounting":
		return frappe.get_doc({
			"doctype": "Project Accounting", "project_name": "AE Dimension " + frappe.generate_hash(length=8),
			"company": company, "project_type": "Opex", "status": "Open",
		}).insert(ignore_permissions=True).name
	if link.options == "Project":
		return frappe.get_doc({
			"doctype": "Project", "project_name": "AE Dimension " + frappe.generate_hash(length=8),
			"company": company,
		}).insert(ignore_permissions=True).name
	filters = {"company": company} if frappe.get_meta(link.options).has_field("company") else {}
	return frappe.db.get_value(link.options, filters, "name")


# ------------------------------------------------ Control Category (D-053)
# The five shapes review 2026-09-26 probed, posted through this app's real
# paths (Existing-Asset booking, enable_depreciation, post_schedule_entries,
# Asset Movement, scrap_asset). asset_enterprise's E-28 asserts the legs;
# project_accounting's phase35 asserts the settlement source on the same
# scenarios (PA -> AE is the permitted edge). Writes: the caller owns the
# savepoint or the throwaway site.
CONTROL_SHAPES = ("plain", "prior_fiscal_year", "transfer", "leave_project", "scrap")


def _expense_account(company, label):
	parent = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Expense", "is_group": 1, "parent_account": ("is", "set")},
		"name", order_by="lft desc",
	)
	return frappe.get_doc({
		"doctype": "Account", "account_name": f"{label} {frappe.generate_hash(length=6)}",
		"company": company, "parent_account": parent, "is_group": 0, "account_type": "Expense Account",
	}).insert(ignore_permissions=True).name


def control_category_fixture(company, label="AE Control"):
	"""A Control Category with distinct Expense-root cost, accumulated and
	depreciation-expense accounts (D-033), its own Prior Year Adjustment
	account and a suspense account for the Existing-Asset booking."""
	accounts = frappe._dict(
		fixed_asset_account=_expense_account(company, f"{label} Cost"),
		accumulated_depreciation_account=_expense_account(company, f"{label} Accumulated"),
		depreciation_expense_account=_expense_account(company, f"{label} Depreciation"),
		pya_expense_account=_expense_account(company, f"{label} PYA"),
		asset_suspense_account=pick_plain_account(company, "Liability"),
	)
	category = frappe.get_doc({
		"doctype": "Asset Category",
		"asset_category_name": f"{label} {frappe.generate_hash(length=6)}",
		"is_control_category": 1,
		"accounts": [{"company_name": company, **accounts}],
	}).insert(ignore_permissions=True)
	item = frappe.get_doc({
		"doctype": "Item", "item_code": f"{category.name} Item", "item_name": f"{category.name} Item",
		"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
		"is_fixed_asset": 1, "is_stock_item": 0, "asset_category": category.name,
	}).insert(ignore_permissions=True)
	return frappe._dict(category=category.name, item=item.name, company=company, **accounts)


def control_asset(fixture, amount, available_for_use_date, dimensions=None, cost_center=None):
	"""A submitted Existing Asset in the fixture's category, acquired under
	`dimensions`; its booking entry is the acquisition."""
	asset = frappe.get_doc({
		"doctype": "Asset", "company": fixture.company, "asset_name": f"{fixture.category} asset",
		"asset_category": fixture.category, "item_code": fixture.item, "location": _ensure_location(),
		"purchase_amount": amount, "net_purchase_amount": amount,
		"available_for_use_date": available_for_use_date, "purchase_date": available_for_use_date,
		"asset_type": "Existing Asset", "calculate_depreciation": 0,
		"cost_center": cost_center or frappe.get_cached_value("Company", fixture.company, "cost_center"),
		**(dimensions or {}),
	})
	asset.flags.ignore_permissions = True
	asset.insert()
	asset.submit()
	return asset.name


def _control_charge(asset_name, charge_date, posting_date):
	from asset_enterprise.depreciation import enable_depreciation, post_schedule_entries

	enable_depreciation(asset_name, total_number_of_depreciations=1, depreciation_start_date=charge_date)
	schedule = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset_name, "status": "Active", "docstatus": 1}, "name")
	posted = post_schedule_entries(schedule, date=str(posting_date))
	if len(posted) != 1:
		frappe.throw(f"control charge on {asset_name}: {len(posted)} row(s) posted, want 1")
	return frappe.db.get_value("Depreciation Schedule", posted[0], "journal_entry")


def _move(asset_name, company, on_date, **row):
	doc = frappe.get_doc({"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
		"transaction_date": str(on_date), "assets": [{"asset": asset_name, **row}]})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def control_shape(fixture, shape, acquired_under, moved_to=None, amount=12_000):
	"""Run one D-053 shape on a new Control Category asset and return
	frappe._dict(asset, acquisition, vouchers, shape).

	`acquired_under` / `moved_to` are {dimension field: value}; moved_to
	also takes `target_cost_center`. Dates are relative to today so the
	one-day charge is due, and the prior-fiscal-year shape charges a day
	of the previous fiscal year posted today (the §4.7 PYA split)."""
	from frappe.utils import add_days, get_first_day, getdate

	from asset_enterprise import disposal

	company = fixture.company
	if shape == "prior_fiscal_year":
		from erpnext.accounts.utils import get_fiscal_year

		start = get_fiscal_year(nowdate(), company=company, as_dict=True).year_start_date
		day0 = add_days(getdate(start), -7)
		charge_on, post_on = day0, getdate(nowdate())
	else:
		day0 = getdate(get_first_day(add_months(nowdate(), -1)))
		charge_on = post_on = add_days(day0, 5)
	asset = control_asset(fixture, amount, day0, dimensions=acquired_under)
	acquisition = frappe.db.get_value("Journal Entry Account",
		{"reference_type": "Asset", "reference_name": asset, "docstatus": 1}, "parent")
	vouchers = [acquisition]
	if shape == "transfer":
		vouchers.append(_move(asset, company, add_days(day0, 2), **(moved_to or {})))
	elif shape == "leave_project":
		vouchers.append(_move(asset, company, add_days(day0, 2), leave_project=1))
	if shape == "scrap":
		vouchers.append(disposal.scrap_asset(asset, scrap_date=str(add_days(day0, 3)), scrapping_type="Damage"))
	else:
		vouchers.append(_control_charge(asset, charge_on, post_on))
	return frappe._dict(asset=asset, acquisition=acquisition, vouchers=vouchers, shape=shape, amount=amount)


def control_asset_legs(asset_name, fields):
	"""Every live GL row carrying the asset, with its account's root type,
	cost centre and the named dimension fields."""
	return frappe.db.sql(
		f"""select gle.name, gle.voucher_type, gle.voucher_no, gle.account, acc.root_type,
		          gle.debit, gle.credit, gle.cost_center
		          {''.join(f', gle.`{f}`' for f in fields)}
		   from `tabGL Entry` gle join `tabAccount` acc on acc.name = gle.account
		   where gle.is_cancelled = 0 and gle.asset = %s
		   order by gle.posting_date, gle.creation""",
		asset_name, as_dict=True,
	)
