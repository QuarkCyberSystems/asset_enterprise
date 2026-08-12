"""Phase 12 verification — TC-traceability gap closure. Run:
bench --site <site> execute asset_enterprise.setup.verify_phase12.run

Automates the design test cases the earlier suites covered only
implicitly: TC-006 (PR-row allocation), TC-020 (EOM dating asserted
explicitly), TC-021 (PYA JE posts to the PYA account), TC-044b (AVA
reversal with subsequent depreciation), TC-047c (repair reversal
returns consumed stock via Material Receipt).
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate

from asset_enterprise.setup.test_fixtures import make_test_asset, pick_company, pick_plain_account


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()


def _run():
	ok = True
	company = pick_company()

	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import _post_one, enable_depreciation

	switch_before = frappe.db.get_single_value(
		"Asset Settings", "enable_enterprise_assets", cache=False
	)
	frappe.db.savepoint("phase12_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)

		def first_unposted_row(asset_name):
			r = frappe.db.sql(
				"""select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
				   ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
				   ds.daily_rate, ds.days_in_period
				from `tabDepreciation Schedule` ds
				join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
				where ads.asset = %s and ads.status='Active' and ads.docstatus=1
				  and ifnull(ds.journal_entry,'')='' order by ds.schedule_date limit 1""",
				asset_name, as_dict=True,
			)
			return r[0] if r else None

		# ------------------------------ TC-006: PR-row allocation limits
		base = make_test_asset(company, gross=1, submit=False)  # masters
		item = frappe.get_doc("Item", "AE-SMOKE-ITEM")
		item.auto_create_assets = 1
		item.asset_naming_series = (
			frappe.get_meta("Asset").get_field("naming_series").options.split("\n")[0]
		)
		item.flags.ignore_permissions = True
		item.save()
		supplier = frappe.db.get_value(
			"Supplier", {"supplier_name": "AE Smoke Supplier"}, "name"
		) or (
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "AE Smoke Supplier"})
			.insert(ignore_permissions=True)
			.name
		)
		pr = frappe.get_doc(
			{
				"doctype": "Purchase Receipt", "company": company, "supplier": supplier,
				"posting_date": nowdate(),
				"items": [{"item_code": "AE-SMOKE-ITEM", "qty": 1, "rate": 10_000,
					"asset_location": base.location}],
			}
		)
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		extra = frappe.get_doc(
			{
				"doctype": "Asset", "company": company, "item_code": "AE-SMOKE-ITEM",
				"asset_name": "AE Smoke Over-Alloc", "asset_category": "AE Smoke Category",
				"location": base.location, "purchase_receipt": pr.name,
				"purchase_receipt_item": pr.items[0].name, "purchase_date": nowdate(),
				"available_for_use_date": nowdate(), "purchase_amount": 5_000,
				"net_purchase_amount": 5_000, "asset_quantity": 1,
				"calculate_depreciation": 0,
			}
		)
		extra.flags.ignore_permissions = True
		try:
			extra.insert()
			print("tc006  FAIL (over-allocated asset accepted against PR row)")
			ok = False
		except frappe.ValidationError as e:
			g = "VR-004" in str(e) or "quantity" in str(e).lower() or "exceed" in str(e).lower()
			print(f"tc006  PR-row over-allocation blocked: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# ------------------------------ TC-020: explicit EOM date assert
		e1 = make_test_asset(company, gross=36_000, submit=True)
		enable_depreciation(
			e1.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
			depreciation_start_date=add_days(get_first_day(nowdate()), 14),  # mid-month start
		)
		rows = frappe.db.sql(
			"""select ds.schedule_date from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent=ads.name
			where ads.asset=%s and ads.status='Active' order by ds.schedule_date""",
			e1.name,
		)
		non_eom = [
			r[0] for r in rows[:-1] if getdate(r[0]) != get_last_day(r[0])
		]  # final row may be end-of-life
		tc20_ok = rows and not non_eom
		print(f"tc020  {len(rows)} rows, non-EOM (excl. final)={non_eom} {'OK' if tc20_ok else 'FAIL'}")
		ok = ok and tc20_ok

		# ------------------------------ TC-021: PYA JE posts to PYA account
		from erpnext.accounts.utils import get_fiscal_year

		prior_year = str(getdate(nowdate()).year - 1)
		try:
			get_fiscal_year(f"{prior_year}-12-31", as_dict=True)
		except Exception:
			frappe.get_doc(
				{"doctype": "Fiscal Year", "year": prior_year,
				 "year_start_date": f"{prior_year}-01-01",
				 "year_end_date": f"{prior_year}-12-31"}
			).insert(ignore_permissions=True)
		pya_account = pick_plain_account(company, "Expense")
		frappe.db.set_value(
			"Company", company, "default_pya_expense_account", pya_account, update_modified=False
		)
		p1 = make_test_asset(company, gross=12_000, submit=True)
		enable_depreciation(
			p1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=f"{prior_year}-11-01",
		)
		row = first_unposted_row(p1.name)  # dated Nov 30 of prior FY
		_post_one(row, getdate(nowdate()))
		posted = frappe.db.get_value(
			"Depreciation Schedule", row.row_name, ["journal_entry", "is_pya_entry"], as_dict=True
		)
		debit_account = frappe.db.get_value(
			"Journal Entry Account",
			{"parent": posted.journal_entry, "debit_in_account_currency": (">", 0)},
			"account",
		)
		tc21_ok = posted.is_pya_entry == 1 and debit_account == pya_account
		print(
			f"tc021  prior-FY row posted: is_pya={posted.is_pya_entry} debit={debit_account} "
			f"(want PYA {pya_account}) {'OK' if tc21_ok else 'FAIL'}"
		)
		ok = ok and tc21_ok

		# --------------- TC-044b: AVA reversal with subsequent depreciation
		v1 = make_test_asset(company, gross=24_000, submit=True)
		enable_depreciation(
			v1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(add_months(nowdate(), -2)),
		)
		_post_one(first_unposted_row(v1.name), getdate(nowdate()))  # month -2
		ava = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment", "asset": v1.name, "company": company,
				"date": nowdate(), "transaction_type": "Upward Revaluation",
				"current_asset_value": 24_000, "new_asset_value": 34_000,
				"difference_account": pick_plain_account(company, "Liability"),
			}
		)
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		_post_one(first_unposted_row(v1.name), getdate(nowdate()))  # SUBSEQUENT depreciation
		hav_before_cancel = flt(
			recalculate_asset_values(v1.name, save=False)["historical_asset_value"]
		)
		ava.reload()
		ava.cancel()  # always-reverse despite subsequent posting
		values = recalculate_asset_values(v1.name, save=False)
		posted_rows = frappe.db.sql(
			"""select count(*) from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent=ads.name
			where ads.asset=%s and ads.status='Active' and ifnull(ds.journal_entry,'')!=''""",
			v1.name,
		)[0][0]
		reversal_exists = frappe.db.exists(
			"Asset Value Adjustment", {"reversal_of_ava": ava.name, "docstatus": 1}
		)
		tc44_ok = (
			hav_before_cancel == 34_000
			and flt(values["historical_asset_value"]) == 24_000
			and posted_rows == 2  # both depreciation JEs stay posted
			and bool(reversal_exists)
		)
		print(
			f"tc044b HAV 34000->{values['historical_asset_value']} (want 24000) posted-rows={posted_rows} "
			f"(want 2, immutable) reversal-AVA={bool(reversal_exists)} {'OK' if tc44_ok else 'FAIL'}"
		)
		ok = ok and tc44_ok

		# --------------- TC-047c: repair reversal returns consumed stock
		stock_item = "AE-SMOKE-STOCK"
		if not frappe.db.exists("Item", stock_item):
			frappe.get_doc(
				{
					"doctype": "Item", "item_code": stock_item, "item_name": stock_item,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					"is_stock_item": 1,
				}
			).insert(ignore_permissions=True)
		warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		se_in = frappe.get_doc(
			{
				"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
				"company": company, "posting_date": nowdate(),
				"items": [{"item_code": stock_item, "qty": 10, "t_warehouse": warehouse,
					"basic_rate": 200, "allow_zero_valuation_rate": 0}],
			}
		)
		se_in.flags.ignore_permissions = True
		se_in.insert()
		se_in.submit()

		r1 = make_test_asset(company, gross=30_000, submit=True)
		repair = frappe.get_doc(
			{
				"doctype": "Asset Repair", "asset": r1.name, "company": company,
				"failure_date": add_days(nowdate(), -1), "completion_date": frappe.utils.now(),
				"repair_status": "Completed", "repair_cost": 0,
				"capitalize_repair_cost": 1,
				"cost_center": frappe.db.get_value("Company", company, "cost_center"),
				"stock_items": [
					{"item_code": stock_item, "consumed_quantity": 4, "warehouse": warehouse,
					 "valuation_rate": 200, "total_value": 800}
				],
			}
		)
		repair.flags.ignore_permissions = True
		repair.insert()
		repair.submit()
		qty_after_repair = flt(
			frappe.db.get_value(
				"Bin", {"item_code": stock_item, "warehouse": warehouse}, "actual_qty"
			)
		)
		repair.reload()
		repair.cancel()  # -> Reversal Repair + Material Receipt return
		qty_after_reversal = flt(
			frappe.db.get_value(
				"Bin", {"item_code": stock_item, "warehouse": warehouse}, "actual_qty"
			)
		)
		return_se = frappe.db.sql(
			"""select se.name from `tabStock Entry` se
			join `tabStock Entry Detail` sed on sed.parent = se.name
			where se.docstatus = 1 and se.stock_entry_type = 'Material Receipt'
			  and sed.item_code = %s and sed.qty = 4 and se.name != %s
			order by se.creation desc limit 1""",
			(stock_item, se_in.name),
		)
		tc47_ok = (
			qty_after_repair == 6 and qty_after_reversal == 10 and bool(return_se)
		)
		print(
			f"tc047c stock {qty_after_repair} after repair (want 6), {qty_after_reversal} after "
			f"reversal (want 10), return SE={return_se and return_se[0][0]} {'OK' if tc47_ok else 'FAIL'}"
		)
		ok = ok and tc47_ok

	finally:
		frappe.db.rollback(save_point="phase12_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 12:", "PASS" if ok else "FAIL")
