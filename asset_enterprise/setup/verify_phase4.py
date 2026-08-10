"""Phase 4 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase4.run

Everything mutating runs inside one savepoint and rolls back.
"""

import traceback

import frappe
from frappe.utils import flt, nowdate


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()


def _run():
	ok = True
	from asset_enterprise.setup.test_fixtures import pick_company
	company = pick_company()

	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.overrides.asset_repair import is_fully_depreciated
	from asset_enterprise.setup.test_fixtures import make_test_asset

	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	ft_before = frappe.db.count("Financial Treatment")
	frappe.db.savepoint("phase4_verify")
	try:
		# Master switch ON inside the savepoint.
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)

		asset = make_test_asset(company, gross=120_000, submit=False, with_depreciation=True)
		asset.submit()
		expense_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
		)
		cost_center = frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0}, "name"
		)

		# ------------------------------------------- AVA forward (GAP-030)
		ava = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment",
				"asset": asset.name,
				"company": company,
				"date": nowdate(),
				"new_asset_value": 140_000,
				"difference_account": expense_account,
				"cost_center": cost_center,
			}
		)
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()

		ft = frappe.db.get_value(
			"Financial Treatment",
			{"source_doctype": "Asset Value Adjustment", "source_name": ava.name},
			["name", "transaction_category", "hav_delta", "status"],
			as_dict=True,
		)
		hav = recalculate_asset_values(asset.name, save=False)["historical_asset_value"]
		fwd_ok = (
			ft
			and ft.transaction_category == "Revaluation"
			and flt(ft.hav_delta) == 20_000
			and ft.status == "Posted"
			and hav == 140_000
		)
		print(
			f"ava    forward: FT={ft and ft.name} cat={ft and ft.transaction_category} "
			f"hav_delta={ft and ft.hav_delta} HAV={hav} {'OK' if fwd_ok else 'FAIL'}"
		)
		ok = ok and bool(fwd_ok)
		orig_je = frappe.db.get_value("Asset Value Adjustment", ava.name, "journal_entry")

		# ------------------------------------------- AVA reverse (VR-034)
		ava.reload()
		ava.cancel()

		reversal_name = frappe.db.get_value(
			"Asset Value Adjustment", {"reversal_of_ava": ava.name, "docstatus": 1}, "name"
		)
		orig_ft_status = frappe.db.get_value("Financial Treatment", ft.name, "status")
		mirror = frappe.db.get_value(
			"Financial Treatment",
			{"reversal_reference": ft.name},
			["name", "hav_delta", "status"],
			as_dict=True,
		)
		hav_after = recalculate_asset_values(asset.name, save=False)["historical_asset_value"]
		je_docstatus = frappe.db.get_value("Journal Entry", orig_je, "docstatus") if orig_je else None
		reversed_by = frappe.db.get_value("Asset Value Adjustment", ava.name, "reversed_by_ava")

		rev_ok = (
			reversal_name
			and orig_ft_status == "Reversed"
			and mirror
			and flt(mirror.hav_delta) == -20_000
			and hav_after == 120_000
			and je_docstatus == 1
			and reversed_by == reversal_name
		)
		print(
			f"ava    reverse: reversal={reversal_name} origFT={orig_ft_status} "
			f"mirror_delta={mirror and mirror.hav_delta} HAV back={hav_after} "
			f"origJE_docstatus={je_docstatus} backref={'OK' if reversed_by == reversal_name else reversed_by} "
			f"{'OK' if rev_ok else 'FAIL'}"
		)
		ok = ok and bool(rev_ok)

		# --------------------------------- Reversal AVA loop guard
		if reversal_name:
			try:
				frappe.get_doc("Asset Value Adjustment", reversal_name).cancel()
				print("ava    loop-guard: FAIL (reversal AVA cancelled)")
				ok = False
			except frappe.ValidationError as e:
				guard = "cannot be cancelled" in str(e)
				print(f"ava    loop-guard throw: {'OK' if guard else 'FAIL'}")
				ok = ok and guard

		# --------------------------------- Asset cancel gate (GAP-027)
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": company,
				"posting_date": nowdate(),
				"accounts": [
					{"account": expense_account, "debit_in_account_currency": 10, "cost_center": cost_center},
					{"account": expense_account, "credit_in_account_currency": 10, "cost_center": cost_center},
				],
			}
		)
		je.flags.ignore_permissions = True
		je.submit()
		row = frappe.db.get_value(
			"Depreciation Schedule",
			{"parent": frappe.db.get_value(
				"Asset Depreciation Schedule",
				{"asset": asset.name, "status": "Active", "docstatus": 1}, "name")},
			"name",
		)
		frappe.db.set_value("Depreciation Schedule", row, "journal_entry", je.name, update_modified=False)
		try:
			asset.reload()
			asset.cancel()
			print("asset  cancel gate: FAIL (cancelled with posted depreciation)")
			ok = False
		except frappe.ValidationError as e:
			gate = "posted depreciation entries" in str(e) and "GA-0001-01" in str(e)
			print(f"asset  cancel gate throw (GA-0001-01 message): {'OK' if gate else 'FAIL: ' + str(e)[:80]}")
			ok = ok and gate

		# --------------------------------- Repair gates (VR-038 helpers)
		fully = is_fully_depreciated(asset.name)
		print(f"repair is_fully_depreciated(fresh asset) = {fully} (want False) {'OK' if not fully else 'FAIL'}")
		ok = ok and not fully

		guard_doc = frappe.get_doc(
			{
				"doctype": "Asset Repair",
				"asset": asset.name,
				"company": company,
				"failure_date": nowdate(),
				"repair_status": "Completed",
				"transaction_type": "Reversal",
				"reversal_of_repair": None,
			}
		)
		guard_doc.name = "AE-GUARD-TEST"
		try:
			guard_doc.on_cancel()
			print("repair loop-guard: FAIL (no throw)")
			ok = False
		except frappe.ValidationError as e:
			guard = "cannot be cancelled" in str(e)
			print(f"repair loop-guard throw: {'OK' if guard else 'FAIL'}")
			ok = ok and guard

	finally:
		frappe.db.rollback(save_point="phase4_verify")
		leftovers = frappe.db.count("Asset", {"asset_name": "AE Smoke Asset"}) + (
			frappe.db.count("Financial Treatment") - ft_before
		)
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={leftovers} switch={switch} {'OK' if leftovers == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and leftovers == 0 and switch == switch_before

	print("PHASE 4:", "PASS" if ok else "FAIL")
