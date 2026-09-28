"""Phase 15 verification — client tickets of 28/09/2026. Run:
bench --site <site> execute asset_enterprise.setup.verify_phase15.run

  FA-010  a receipt whose asset row carries Valuation charges submits;
          its assets carry the charges and sum to the row exactly when
          the charge does not divide; over-allocation is still refused
          at the row's capitalised value, on the receipt and the asset
  FA-009  a submitted asset with depreciation off reads "Pending
          Depreciation Setup", or "Non-Depreciable" in a Non Depreciable
          Category; Enable Depreciation leaves the pending status and is
          refused on a non-depreciable category; the category flag
          restates its assets and is refused over depreciating ones; the
          new statuses cancel, cascade from the receipt, sell (and
          return), transfer, take a value adjustment, scrap and restore
          like "Submitted"; a draft or Disposed asset cannot be sold;
          migrate and switching Enterprise Assets on restate old rows

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_months, flt, getdate, nowdate

from asset_enterprise.setup.test_fixtures import (
	_ensure_location,
	_find_account,
	_move,
	_sell,
	_supplier,
	ensure_enterprise_test_defaults,
	make_test_asset,
	pick_company,
	pick_plain_account,
)
from asset_enterprise.status import NON_DEPRECIABLE, PENDING_DEPRECIATION_SETUP


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _refused(fn, *args, **kwargs):
	"""The refusal text, or None when `fn` went through."""
	try:
		fn(*args, **kwargs)
		return None
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or type(e).__name__


def _status(asset):
	return frappe.db.get_value("Asset", asset, "status")


def _receiving_item():
	"""AE-SMOKE-ITEM, created by make_test_asset, set to create its
	assets at receipt."""
	item = frappe.get_doc("Item", "AE-SMOKE-ITEM")
	if not item.auto_create_assets:
		item.auto_create_assets = 1
		item.asset_naming_series = frappe.get_meta("Asset").get_field("naming_series").options.split("\n")[0]
		item.flags.ignore_permissions = True
		item.save()
	return item.name


def _receipt(company, qty, rate, freight, submit=True):
	"""A receipt of the smoke asset item with `freight` as an Actual
	charge carried into valuation."""
	pr = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": company, "supplier": _supplier(),
		"posting_date": nowdate(),
		"items": [{"item_code": _receiving_item(), "qty": qty, "rate": rate,
			"asset_location": _ensure_location()}],
		"taxes": [{"category": "Valuation", "add_deduct_tax": "Add", "charge_type": "Actual",
			"account_head": pick_plain_account(company, "Expense"), "description": "Freight",
			"tax_amount": freight}],
	})
	pr.flags.ignore_permissions = True
	pr.insert()
	if submit:
		pr.submit()
	return pr


def _non_depreciable_category(company):
	category = frappe.get_doc({
		"doctype": "Asset Category",
		"asset_category_name": f"AE Land {frappe.generate_hash(length=6)}",
		"non_depreciable_category": 1,
		"accounts": [{"company_name": company,
			"fixed_asset_account": _find_account(company, "Fixed Asset"),
			"accumulated_depreciation_account": _find_account(company, "Accumulated Depreciation"),
			"depreciation_expense_account": _find_account(company, "Depreciation")
				or _find_account(company, "Expense Account", "Expense")}],
	}).insert(ignore_permissions=True)
	item = frappe.get_doc({
		"doctype": "Item", "item_code": f"{category.name} Item", "item_name": f"{category.name} Item",
		"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
		"is_fixed_asset": 1, "is_stock_item": 0, "asset_category": category.name,
	}).insert(ignore_permissions=True)
	return category.name, item.name


def _land(company, category, item, gross=50_000):
	asset = frappe.get_doc({
		"doctype": "Asset", "company": company, "item_code": item, "asset_name": "AE Smoke Land",
		"asset_category": category, "location": _ensure_location(),
		"purchase_amount": gross, "net_purchase_amount": gross,
		"available_for_use_date": add_months(nowdate(), -1), "purchase_date": add_months(nowdate(), -1),
		"calculate_depreciation": 0,
	})
	asset.flags.ignore_permissions = True
	asset.insert()
	asset.submit()
	return asset.name


def _run():
	ok = True
	company = pick_company()
	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	smoke_before = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
	frappe.db.savepoint("phase15_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		ensure_enterprise_test_defaults()
		make_test_asset(company)  # seeds category + item

		# ============ FA-010: receipt charges ride on the asset ==========
		pr = _receipt(company, 1, 12_000, 12_000)
		row = pr.items[0]
		assets = frappe.get_all(
			"Asset", filters={"purchase_receipt_item": row.name},
			fields=["name", "docstatus", "net_purchase_amount", "status"],
		)
		c = (
			len(assets) == 1 and flt(assets[0].net_purchase_amount) == 24_000
			and assets[0].docstatus == 1
		)
		print(
			f"fa010  12,000 row + 12,000 freight submits; the asset carries 24,000: "
			f"{'OK' if c else 'FAIL'} ({assets})"
		)
		ok = ok and c
		a = assets[0].name if assets else None

		pr2 = _receipt(company, 2, 5_000, 3_000)
		values = sorted(frappe.get_all(
			"Asset", filters={"purchase_receipt_item": pr2.items[0].name}, pluck="net_purchase_amount"
		))
		c = values == [6_500, 6_500]
		print(f"fa010  two units, 3,000 freight shared: each asset 6,500: {'OK' if c else 'FAIL'} ({values})")
		ok = ok and c

		# a charge that does not divide: the assets still sum to the row
		# and every one of them submits (chief review 28/09, BLOCK)
		for qty, rate, freight, want in ((9, 1_000, 0.05, 9_000.05), (3, 100, 100, 400.00)):
			uneven = _receipt(company, qty, rate, freight)
			rows = frappe.get_all(
				"Asset", filters={"purchase_receipt_item": uneven.items[0].name},
				fields=["docstatus", "net_purchase_amount", "total_asset_cost"],
			)
			total = round(sum(flt(r.net_purchase_amount) for r in rows), 2)
			spread = max(flt(r.net_purchase_amount) for r in rows) - min(flt(r.net_purchase_amount) for r in rows)
			c = (
				len(rows) == qty and all(r.docstatus == 1 for r in rows) and total == want
				and round(spread, 2) <= 0.01
				and all(flt(r.total_asset_cost) == flt(r.net_purchase_amount) for r in rows)
			)
			print(
				f"fa010  {qty} x {rate} + {freight} freight: {qty} assets submitted, summing {total} "
				f"(want {want}): {'OK' if c else 'FAIL'} ({sorted({flt(r.net_purchase_amount) for r in rows})})"
			)
			ok = ok and c

		from asset_enterprise.invoice_diff import _validate_pr_over_allocation, pr_row_capitalised_value

		c = pr_row_capitalised_value(frappe._dict(
			base_net_amount=1000, item_tax_amount=200, landed_cost_voucher_amount=50)) == 1250
		print(f"fa010  the row's capitalised value is amount + charges + landed cost: {'OK' if c else 'FAIL'}")
		ok = ok and c

		row.reload()
		frappe.db.set_value("Asset", a, "net_purchase_amount", 24_000.5, update_modified=False)
		receipt_refusal = _refused(_validate_pr_over_allocation, row, [a])
		asset_doc = frappe.get_doc("Asset", a)
		asset_refusal = _refused(asset_doc._validate_pr_row_allocation)
		frappe.db.set_value("Asset", a, "net_purchase_amount", 24_000, update_modified=False)
		asset_doc.reload()
		c = (
			bool(receipt_refusal) and "24000" in receipt_refusal.replace(",", "")
			and bool(asset_refusal)
			and _refused(_validate_pr_over_allocation, row, [a]) is None
			and _refused(asset_doc._validate_pr_row_allocation) is None
		)
		print(
			f"fa010  above the row's 24,000 is still over-allocation, on the receipt and the asset: "
			f"{'OK' if c else 'FAIL'} ({receipt_refusal and receipt_refusal[:110]})"
		)
		ok = ok and c

		# ============ FA-009: statuses ===================================
		c = _status(a) == PENDING_DEPRECIATION_SETUP
		asset_doc.set_status()
		c = c and _status(a) == PENDING_DEPRECIATION_SETUP
		print(f"fa009  a receipt asset with depreciation off is Pending Depreciation Setup, and stays so: {'OK' if c else 'FAIL'} ({_status(a)})")
		ok = ok and c

		options = frappe.get_meta("Asset").get_field("status").options.split("\n")
		c = PENDING_DEPRECIATION_SETUP in options and NON_DEPRECIABLE in options
		print(f"fa009  both statuses are on the Asset status list: {'OK' if c else 'FAIL'}")
		ok = ok and c

		from asset_enterprise.depreciation import enable_depreciation

		enable_depreciation(
			a, 24, 1, depreciation_start_date=add_months(nowdate(), 1),
			available_for_use_date=nowdate(),
		)
		c = _status(a) == "Submitted"
		print(f"fa009  Enable Depreciation moves it to Submitted: {'OK' if c else 'FAIL'} ({_status(a)})")
		ok = ok and c

		category, item = _non_depreciable_category(company)
		land = _land(company, category, item)
		c = _status(land) == NON_DEPRECIABLE
		refusal = _refused(enable_depreciation, land, 24, 1, depreciation_start_date=nowdate())
		c = c and bool(refusal) and "Non Depreciable" in refusal
		print(f"fa009  an asset of a Non Depreciable category is Non-Depreciable; Enable Depreciation refused: {'OK' if c else 'FAIL'} ({_status(land)})")
		ok = ok and c

		cat = frappe.get_doc("Asset Category", category)
		cat.non_depreciable_category = 0
		cat.save(ignore_permissions=True)
		c = _status(land) == PENDING_DEPRECIATION_SETUP
		cat.reload()
		cat.non_depreciable_category = 1
		cat.save(ignore_permissions=True)
		c = c and _status(land) == NON_DEPRECIABLE
		print(f"fa009  unticking / ticking the category flag restates its assets: {'OK' if c else 'FAIL'}")
		ok = ok and c

		make_test_asset(company, gross=12_000, submit=True, with_depreciation=True)
		smoke = frappe.get_doc("Asset Category", "AE Smoke Category")
		smoke.non_depreciable_category = 1
		refusal = _refused(smoke.save, ignore_permissions=True)
		c = bool(refusal) and "depreciate" in refusal
		print(f"fa009  the flag is refused on a category with depreciating assets: {'OK' if c else 'FAIL'} ({refusal and refusal[:90]})")
		ok = ok and c

		# the new statuses behave as "Submitted" everywhere core checks it
		loose = make_test_asset(company, gross=9_000, submit=True)
		loose.reload()
		c = loose.status == PENDING_DEPRECIATION_SETUP
		loose.cancel()
		c = c and _status(loose.name) == "Cancelled"
		print(f"fa009  a Pending Depreciation Setup asset cancels: {'OK' if c else 'FAIL'}")
		ok = ok and c

		pr2_assets = frappe.get_all("Asset", filters={"purchase_receipt": pr2.name}, pluck="name")
		c = all(_status(n) == PENDING_DEPRECIATION_SETUP for n in pr2_assets)
		pr2.reload()
		pr2.cancel()
		c = c and all(frappe.db.get_value("Asset", n, "docstatus") == 2 for n in pr2_assets)
		print(f"fa009  cancelling the receipt cascades to its pending assets: {'OK' if c else 'FAIL'}")
		ok = ok and c

		sold = make_test_asset(company, gross=8_000, submit=True)
		_sell(sold.name, company, 8_500)
		c = _status(sold.name) == "Sold"
		print(f"fa009  a Pending Depreciation Setup asset can be sold: {'OK' if c else 'FAIL'} ({_status(sold.name)})")
		ok = ok and c

		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		si = frappe.get_all("Sales Invoice Item", filters={"asset": sold.name}, pluck="parent")[0]
		ret = make_return_doc("Sales Invoice", si)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		c = _status(sold.name) == PENDING_DEPRECIATION_SETUP
		print(f"fa009  a sales return brings it back as Pending Depreciation Setup: {'OK' if c else 'FAIL'} ({_status(sold.name)})")
		ok = ok and c

		draft = make_test_asset(company, gross=4_000)
		c = "cannot be sold" in (_refused(_sell, draft.name, company, 4_000) or "")
		merged = make_test_asset(company, gross=4_000, submit=True)
		frappe.db.set_value("Asset", merged.name, "status", "Disposed", update_modified=False)
		c = c and "cannot be sold" in (_refused(_sell, merged.name, company, 4_000) or "")
		print(f"fa009  a draft or a Disposed asset cannot be sold: {'OK' if c else 'FAIL'}")
		ok = ok and c

		moved = make_test_asset(company, gross=6_000, submit=True)
		target = frappe.get_doc({"doctype": "Location", "location_name": f"AE Smoke Loc {frappe.generate_hash(length=5)}"}).insert(
			ignore_permissions=True).name
		_move(moved.name, company, nowdate(), source_location=moved.location, target_location=target)
		c = (
			frappe.db.get_value("Asset", moved.name, "location") == target
			and _status(moved.name) == PENDING_DEPRECIATION_SETUP
		)
		print(f"fa009  a Pending Depreciation Setup asset transfers: {'OK' if c else 'FAIL'}")
		ok = ok and c

		ava = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": moved.name, "company": company,
			"date": nowdate(), "transaction_type": "Upward Revaluation",
			"current_asset_value": 6_000, "new_asset_value": 7_000,
			"difference_account": pick_plain_account(company, "Liability"),
		})
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		c = _status(moved.name) == PENDING_DEPRECIATION_SETUP
		print(f"fa009  a value adjustment on it keeps Pending Depreciation Setup: {'OK' if c else 'FAIL'} ({_status(moved.name)})")
		ok = ok and c

		gone = _land(company, category, item, gross=3_000)
		frappe.get_doc("Asset", gone).cancel()
		c = _status(gone) == "Cancelled"
		print(f"fa009  a Non-Depreciable asset cancels: {'OK' if c else 'FAIL'}")
		ok = ok and c

		from asset_enterprise import disposal
		from asset_enterprise.restore import restore_asset

		disposal.scrap_asset(land, scrapping_type="Damage")
		c = _status(land) == "Scrapped"
		restore_asset(land)
		c = c and _status(land) == NON_DEPRECIABLE
		print(f"fa009  a scrapped Non-Depreciable asset restores to Non-Depreciable: {'OK' if c else 'FAIL'} ({_status(land)})")
		ok = ok and c

		from asset_enterprise.overrides.asset_category import restate_all_not_depreciating

		frappe.db.set_value("Asset", land, "status", "Submitted", update_modified=False)
		keep = make_test_asset(company, gross=7_000, submit=True)
		frappe.db.set_value("Asset", keep.name, "status", "Submitted", update_modified=False)
		restate_all_not_depreciating()
		restate_all_not_depreciating()  # idempotent
		c = (
			_status(land) == NON_DEPRECIABLE
			and _status(keep.name) == PENDING_DEPRECIATION_SETUP
			and _status(a) == "Submitted"  # depreciating: untouched, so core's posting query still sees it
			and _status(merged.name) == "Disposed"
		)
		print(f"fa009  the migrate restatement restates old rows, leaves the rest: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# switching Enterprise Assets on restates at once
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 0)
		frappe.db.set_value("Asset", keep.name, "status", "Submitted", update_modified=False)
		settings = frappe.get_doc("Asset Settings")
		settings.enable_enterprise_assets = 1
		settings.flags.ignore_permissions = True
		settings.save()
		c = _status(keep.name) == PENDING_DEPRECIATION_SETUP
		print(f"fa009  switching Enterprise Assets on restates: {'OK' if c else 'FAIL'} ({_status(keep.name)})")
		ok = ok and c

	finally:
		frappe.db.rollback(save_point="phase15_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")}) - smoke_before
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 15:", "PASS" if ok else "FAIL")
