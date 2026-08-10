"""Phase 2 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase2.run

DB-mutating checks run inside a savepoint and are rolled back — nothing
persists on the live site.
"""

import frappe
from frappe.utils import flt


def run():
	ok = True

	# ---------------------------------------------------------- rounding math
	from asset_enterprise.rounding import (
		fa_module_round,
		final_row_amount,
		final_row_drift,
		get_currency_precision,
	)

	from asset_enterprise.setup.test_fixtures import pick_company
	company = pick_company()
	places = get_currency_precision(company)
	print(f"rounding precision({company}) = {places} places")

	checks = [
		(fa_module_round(169863.014, company), 169863.01),
		(fa_module_round(169863.015, company), 169863.02),  # half-up
		(fa_module_round(5479.4520548, company), 5479.45),
	]
	for got, want in checks:
		res = "OK" if got == want else f"FAIL (got {got}, want {want})"
		print(f"rounding fa_module_round -> {want}: {res}")
		ok = ok and got == want

	# §4.10 point 3: final row absorbs drift — 10M asset, 0 salvage,
	# 59 nominal rounded rows posted; final row = base - posted.
	base = 10_000_000.0
	posted = 9_830_136.61
	final = final_row_amount(base, posted, company)
	drift = final_row_drift(final, 169_863.01, company)
	print(f"rounding final_row_amount = {final} (drift vs nominal {drift})")
	ok = ok and final == fa_module_round(base - posted, company)

	# ------------------------------------------------------ FT doctype shape
	for fieldname in [
		"hav_delta",
		"accum_delta",
		"life_delta_months",
		"reversal_reference",
		"transaction_category",
		"status",
	]:
		exists = frappe.db.exists(
			"DocField", {"parent": "Financial Treatment", "fieldname": fieldname}
		)
		print(f"ft field {fieldname:22s} {'OK' if exists else 'MISSING'}")
		ok = ok and bool(exists)

	# ------------------------------------- TCC round-trip (rolled back)
	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values

	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	frappe.db.savepoint("phase2_verify")
	created_fixture = False
	asset_name = frappe.db.get_value("Asset", {"docstatus": ["<", 2]}, "name")
	if not asset_name:
		from asset_enterprise.setup.test_fixtures import make_test_asset

		asset_name = make_test_asset(company).name
		created_fixture = True
		print(f"tcc     built throwaway fixture asset {asset_name} (rolled back below)")
	if True:
		try:
			before = recalculate_asset_values(asset_name, save=False)

			ft = tcc.apply(
				source_doc=("Asset", asset_name),
				category="Addition",
				transaction_type="Phase2 Smoke",
				asset=asset_name,
				amount=1000,
				hav_delta=1000,
				posting_date=frappe.utils.nowdate(),
			)
			mid = recalculate_asset_values(asset_name, save=False)
			delta_ok = flt(mid["historical_asset_value"] - before["historical_asset_value"], 2) == 1000.0
			print(f"tcc     apply(+1000 HAV): {'OK' if delta_ok else 'FAIL'} "
				f"({before['historical_asset_value']} -> {mid['historical_asset_value']})")
			ok = ok and delta_ok

			mirror = tcc.reverse(ft.name, ("Asset", asset_name))
			after = recalculate_asset_values(asset_name, save=False)
			net_ok = flt(after["historical_asset_value"], 2) == flt(before["historical_asset_value"], 2)
			status_ok = frappe.db.get_value("Financial Treatment", ft.name, "status") == "Reversed"
			pair_ok = frappe.db.get_value("Financial Treatment", mirror.name, "reversal_reference") == ft.name
			print(f"tcc     reverse nets to zero: {'OK' if net_ok else 'FAIL'} "
				f"(back to {after['historical_asset_value']})")
			print(f"tcc     original->Reversed: {'OK' if status_ok else 'FAIL'}; "
				f"mirror backref: {'OK' if pair_ok else 'FAIL'}")
			activity = frappe.db.count(
				"Asset Activity", {"asset": asset_name, "transaction_type": ["like", "%Phase2 Smoke%"]}
			)
			print(f"tcc     asset activity rows written: {activity} (want 2)")
			ok = ok and net_ok and status_ok and pair_ok and activity == 2
		finally:
			frappe.db.rollback(save_point="phase2_verify")
			leftover = frappe.db.count("Financial Treatment", {"transaction_type": ["like", "%Phase2 Smoke%"]})
			fixture_left = (
				frappe.db.count("Asset", {"asset_name": "AE Smoke Asset"}) if created_fixture else 0
			)
			clean = leftover == 0 and fixture_left == 0
			print(f"tcc     rollback clean: {'OK' if clean else f'FAIL (ft={leftover}, fixture={fixture_left})'}")
			ok = ok and clean

	print("PHASE 2:", "PASS" if ok else "FAIL")
