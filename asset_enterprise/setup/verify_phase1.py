"""Phase 1 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase1.run"""

import frappe


def run():
	ok = True

	# 1. New doctypes exist
	for dt in [
		"Transaction Category",
		"Scrapping Type",
		"Scrapping Type Account",
		"Asset Settings",
		"Asset Settings Authority Role",
		"Asset Settings Tolerance",
		"Composite Merge Log Entry",
		"PI Asset Allocation",
	]:
		exists = frappe.db.exists("DocType", dt)
		print(f"doctype {dt:35s} {'OK' if exists else 'MISSING'}")
		ok = ok and bool(exists)

	# 2. Seeds
	tc = frappe.db.count("Transaction Category")
	st = frappe.db.count("Scrapping Type")
	print(f"seeds   Transaction Category={tc} (want 6)  Scrapping Type={st} (want 9)")
	ok = ok and tc == 6 and st == 9

	# 3. Custom fields spot checks
	checks = [
		("Asset", "merge_log"),
		("Asset", "remaining_useful_life_months"),
		("Asset", "replacement_of_asset"),
		("Asset Category Account", "capitalization_clearing_account"),
		("Asset Category Account", "disposal_account_override"),
		("Asset Capitalization", "transaction_type"),
		("Asset Capitalization", "fully_depreciated_treatment"),
		("Asset Repair", "reversal_of_repair"),
		("Asset Value Adjustment", "adjusted_life_months"),
		("Depreciation Schedule", "daily_rate"),
		("Asset Movement Item", "target_cost_center"),
		("Purchase Invoice", "pi_asset_allocation"),
		("Purchase Receipt Item", "asset_linked"),
		("Company", "default_capitalization_clearing_account"),
	]
	for dt, fieldname in checks:
		exists = frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname})
		print(f"field   {dt}.{fieldname:40s} {'OK' if exists else 'MISSING'}")
		ok = ok and bool(exists)

	# 4. Float types on life fields (C116/C119)
	for dt, fieldname in [
		("Asset", "remaining_useful_life_months"),
		("Asset Value Adjustment", "adjusted_life_months"),
		("Asset Capitalization", "extended_life_months"),
	]:
		ft = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname}, "fieldtype")
		print(f"float   {dt}.{fieldname:40s} {ft} {'OK' if ft == 'Float' else 'WRONG'}")
		ok = ok and ft == "Float"

	# 5. Property setter — Superseded status
	options = frappe.db.get_value(
		"Property Setter",
		{"doc_type": "Asset Depreciation Schedule", "field_name": "status", "property": "options"},
		"value",
	)
	has_superseded = options and "Superseded" in options
	print(f"psetter Asset Depreciation Schedule.status -> {options!r} {'OK' if has_superseded else 'MISSING'}")
	ok = ok and has_superseded

	# 6. Account resolution chain smoke (expect controlled throw, not crash)
	from asset_enterprise.accounts import get_disposal_account, get_last_period_tolerance

	from asset_enterprise.setup.test_fixtures import pick_company
	company = pick_company()
	try:
		acct = get_disposal_account(company, scrapping_type="Damage")
		print(f"chain   disposal (Damage, {company}) -> {acct}")
	except frappe.ValidationError as e:
		print(f"chain   disposal (Damage, {company}) -> controlled throw OK ({str(e)[:60]}...)")
	tol = get_last_period_tolerance(company)
	print(f"chain   last-period tolerance ({company}) -> {tol}")

	print("PHASE 1:", "PASS" if ok else "FAIL")
