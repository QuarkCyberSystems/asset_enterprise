"""Throwaway asset fixtures for smoke verification.

ALWAYS call inside a savepoint that the caller rolls back — these are
never meant to persist on a live site.
"""

import contextlib

import frappe
from frappe.utils import add_months, get_first_day, nowdate

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
# D-053 "Gross" / "the invoiced price" and review r2 B-1 / M-1 / S-A: the
# reversal, sale, return and invoice-difference shapes. Each run returns
# the settleable source the ruling prescribes (`settleable`) and the
# project's actual Expense after it (`actual`), which differ once money
# comes back (proceeds stay on the project as a credit).
CONTROL_LATER_SHAPES = (
	"scrap_restore", "ava_reversal", "sale", "sale_cancel", "sale_return", "blank_sale",
	"invoice_down", "invoice_up",
)
SALE_PROCEEDS = 5_000


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


def _customer():
	name = frappe.db.get_value("Customer", {"customer_name": "AE Control Buyer"}, "name")
	if name:
		return name
	return frappe.get_doc({"doctype": "Customer", "customer_name": "AE Control Buyer",
		"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
		"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name")}).insert(
		ignore_permissions=True).name


def _supplier():
	name = frappe.db.get_value("Supplier", {"supplier_name": "AE Smoke Supplier"}, "name")
	if name:
		return name
	return frappe.get_doc({"doctype": "Supplier", "supplier_name": "AE Smoke Supplier"}).insert(
		ignore_permissions=True).name


def _with_dimensions(row, doctype, dimensions):
	meta = frappe.get_meta(doctype)
	row.update({f: v for f, v in (dimensions or {}).items() if meta.has_field(f)})
	return row


def _sell(asset_name, company, amount, dimensions=None):
	"""Sell through a Sales Invoice (core's disposal map, AE's wrapper),
	the invoice header and row carrying `dimensions`."""
	item = frappe.db.get_value("Asset", asset_name, "item_code")
	si = frappe.get_doc(_with_dimensions({
		"doctype": "Sales Invoice", "company": company, "customer": _customer(),
		"posting_date": nowdate(), "due_date": nowdate(),
		"items": [_with_dimensions({"item_code": item, "asset": asset_name, "qty": 1, "rate": amount},
			"Sales Invoice Item", dimensions)],
	}, "Sales Invoice", dimensions))
	si.flags.ignore_permissions = True
	si.insert()
	si.submit()
	return si


def control_purchase(fixture, amount, acquired_under):
	"""A Control Category asset bought through a Purchase Receipt under
	`acquired_under`; returns (asset, receipt)."""
	company = fixture.company
	item = frappe.get_doc("Item", fixture.item)
	if not item.auto_create_assets:
		item.auto_create_assets = 1
		item.asset_naming_series = frappe.get_meta("Asset").get_field("naming_series").options.split("\n")[0]
		item.flags.ignore_permissions = True
		item.save()
	pr = frappe.get_doc(_with_dimensions({
		"doctype": "Purchase Receipt", "company": company, "supplier": _supplier(),
		"posting_date": nowdate(),
		"items": [_with_dimensions({"item_code": fixture.item, "qty": 1, "rate": amount,
			"asset_location": _ensure_location()}, "Purchase Receipt Item", acquired_under)],
	}, "Purchase Receipt", acquired_under))
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	asset = frappe.db.get_value("Asset", {"purchase_receipt": pr.name}, "name")
	if frappe.db.get_value("Asset", asset, "docstatus") == 0:
		doc = frappe.get_doc("Asset", asset)
		doc.flags.ignore_permissions = True
		doc.submit()
	return asset, pr


def control_invoice(pr, price, dimensions=None):
	"""A Purchase Invoice against the receipt at `price` - AE posts the
	difference as an Invoice Adjustment AVA plus the delta transfer."""
	row = pr.items[0]
	pi = frappe.get_doc(_with_dimensions({
		"doctype": "Purchase Invoice", "company": pr.company, "supplier": pr.supplier,
		"posting_date": nowdate(),
		"items": [_with_dimensions({"item_code": row.item_code, "qty": 1, "rate": price,
			"purchase_receipt": pr.name, "pr_detail": row.name}, "Purchase Invoice Item", dimensions)],
	}, "Purchase Invoice", dimensions))
	pi.flags.ignore_permissions = True
	pi.insert()
	pi.submit()
	return pi


def _later_shape(fixture, shape, acquired_under, moved_to, amount):
	"""The CONTROL_LATER_SHAPES. Dated today where a same-period rule
	applies (restore, sale); the acquisition a month earlier."""
	from frappe.utils import getdate

	from asset_enterprise import disposal

	company = fixture.company
	day0 = getdate(get_first_day(add_months(nowdate(), -1)))
	settleable, actual = amount, amount
	if shape in ("invoice_down", "invoice_up"):
		asset, pr = control_purchase(fixture, amount, acquired_under)
		price = amount - 1_000 if shape == "invoice_down" else amount + 1_000
		pi = control_invoice(pr, price, acquired_under)
		ava = frappe.db.get_value("Asset Value Adjustment",
			{"asset": asset, "transaction_type": "Invoice Adjustment", "docstatus": 1}, "name")
		return frappe._dict(asset=asset, acquisition=pr.name, vouchers=[pr.name, pi.name, ava],
			shape=shape, amount=amount, settleable=price, actual=price, acquired_under=acquired_under)

	dims = {} if shape == "blank_sale" else acquired_under
	asset = control_asset(fixture, amount, day0, dimensions=dims)
	acquisition = frappe.db.get_value("Journal Entry Account",
		{"reference_type": "Asset", "reference_name": asset, "docstatus": 1}, "parent")
	vouchers = [acquisition]
	if shape == "scrap_restore":
		from asset_enterprise.restore import restore_asset

		vouchers.append(disposal.scrap_asset(asset, scrap_date=nowdate(), scrapping_type="Damage"))
		restore_asset(asset)
		vouchers.append(frappe.db.get_value("Asset", asset, "scrap_reversal_journal_entry"))
	elif shape == "ava_reversal":
		ava = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": asset, "company": company, "date": nowdate(),
			"transaction_type": "Initial Impairment", "current_asset_value": amount,
			"new_asset_value": amount - 3_000, "difference_account": fixture.depreciation_expense_account,
		})
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		vouchers.append(ava.journal_entry)
		ava.reload()
		ava.cancel()
		reversal = frappe.db.get_value("Asset Value Adjustment", {"reversal_of_ava": ava.name}, "journal_entry")
		vouchers.append(reversal)
	else:  # the sales
		sale_dims = (moved_to or {}) if shape == "blank_sale" else acquired_under
		si = _sell(asset, company, SALE_PROCEEDS, {k: v for k, v in sale_dims.items() if k != "target_cost_center"})
		vouchers.append(si.name)
		actual = amount - SALE_PROCEEDS  # the proceeds come back to the project as a credit
		if shape == "sale_cancel":
			si.reload()
			si.cancel()
			actual = amount
		elif shape == "sale_return":
			from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

			ret = make_sales_return(si.name)
			ret.flags.ignore_permissions = True
			ret.insert()
			ret.submit()
			vouchers.append(ret.name)
			actual = amount
		elif shape == "blank_sale":
			settleable = actual = 0  # bought under no project: nothing reaches one
	return frappe._dict(asset=asset, acquisition=acquisition, vouchers=vouchers, shape=shape, amount=amount,
		settleable=settleable, actual=actual, acquired_under=dims)


def control_shape(fixture, shape, acquired_under, moved_to=None, amount=12_000):
	"""Run one D-053 shape on a new Control Category asset and return
	frappe._dict(asset, acquisition, vouchers, shape).

	`acquired_under` / `moved_to` are {dimension field: value}; moved_to
	also takes `target_cost_center`. Dates are relative to today so the
	one-day charge is due, and the prior-fiscal-year shape charges a day
	of the previous fiscal year posted today (the §4.7 PYA split)."""
	from frappe.utils import add_days, get_first_day, getdate

	from asset_enterprise import disposal

	if shape in CONTROL_LATER_SHAPES:
		return _later_shape(fixture, shape, acquired_under, moved_to, amount)
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
	return frappe._dict(asset=asset, acquisition=acquisition, vouchers=vouchers, shape=shape, amount=amount,
		settleable=amount, actual=amount, acquired_under=acquired_under)


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
