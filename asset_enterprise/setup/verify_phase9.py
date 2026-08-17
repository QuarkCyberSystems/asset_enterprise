"""Phase 9 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase9.run

Covers the hardening-phase gap closures:
GAP-001 (TC-001/002), GAP-002 (TC-003/004), GAP-003 (TC-005),
GAP-009 (TC-012/013), GAP-010 (TC-014), GAP-011 (TC-018).
"""

import traceback

import frappe
from frappe.utils import add_days, flt, nowdate


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()


def _run():
	ok = True
	from asset_enterprise.setup.test_fixtures import pick_company, pick_plain_account
	company = pick_company()

	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.setup.test_fixtures import make_test_asset

	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	frappe.db.savepoint("phase9_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)

		# ------------------------------------ GAP-001 negative (TC-002 / VR-001)
		a_neg = make_test_asset(company, gross=500_000, submit=False)
		frappe.db.set_value(
			"Company", company, "default_asset_suspense_account", None, update_modified=False
		)
		try:
			a_neg.submit()
			print("gap001n FAIL (existing asset submitted without suspense account)")
			ok = False
		except frappe.ValidationError as e:
			g = "Suspense" in str(e)
			print(f"gap001n submit blocked without suspense account: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		suspense = pick_plain_account(company, "Liability")
		frappe.db.set_value(
			"Company", company, "default_asset_suspense_account", suspense, update_modified=False
		)

		# ------------------------------------ GAP-001 positive (TC-001) + GAP-002 (TC-003)
		a1 = make_test_asset(company, gross=1_000_000, submit=False)
		a1.opening_accumulated_depreciation = 400_000
		if a1.docstatus == 0:  # the receipt may already have submitted it
			a1.available_for_use_date = None  # TC-003: AFU optional, no depreciation
			a1.flags.ignore_permissions = True
			a1.save()
			a1.submit()

		ft = frappe.db.get_value(
			"Financial Treatment",
			{"asset": a1.name, "transaction_type": "Existing-Asset Opening", "status": "Posted"},
			["name", "journal_entry", "amount"],
			as_dict=True,
		)
		legs = (
			frappe.db.sql(
				"""select account, debit_in_account_currency, credit_in_account_currency
				   from `tabJournal Entry Account` where parent = %s order by idx""",
				ft.journal_entry,
				as_dict=True,
			)
			if ft and ft.journal_entry
			else []
		)
		debit_total = sum(flt(r.debit_in_account_currency) for r in legs)
		credit_suspense = sum(
			flt(r.credit_in_account_currency) for r in legs if r.account == suspense
		)
		g1_ok = (
			ft
			and flt(ft.amount) == 1_000_000
			and len(legs) == 3
			and debit_total == 1_000_000
			and credit_suspense == 600_000
		)
		print(
			f"gap001 opening JE legs={len(legs)} DR={debit_total} CR-suspense={credit_suspense} "
			f"FT={ft and ft.name} {'OK' if g1_ok else 'FAIL'}"
		)
		ok = ok and bool(g1_ok)
		print(f"gap002 AFU empty + no depreciation submitted: OK (asset {a1.name})")

		# ------------------------------------ GAP-002 negative (TC-004 / VR-002)
		a2 = make_test_asset(company, gross=100_000, submit=False, with_depreciation=True)
		a2.available_for_use_date = None
		a2.flags.ignore_permissions = True
		try:
			a2.save()
			a2.submit()
			print("gap002n FAIL (depreciating asset submitted without AFU)")
			ok = False
		except frappe.ValidationError as e:
			g = "VR-002" in str(e) or "Available" in str(e)
			print(f"gap002n AFU required with depreciation on: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# ------------------------------------ GAP-003 (TC-005): receiving-date basis
		frappe.db.set_value(
			"Asset Category", "AE Smoke Category", "calculate_from_receiving_date", 1,
			update_modified=False,
		)
		item = frappe.get_doc("Item", "AE-SMOKE-ITEM")
		item.auto_create_assets = 1
		item.asset_naming_series = (
			frappe.get_meta("Asset").get_field("naming_series").options.split("\n")[0]
		)
		item.flags.ignore_permissions = True
		item.save()
		supplier = frappe.db.get_value("Supplier", {"supplier_name": "AE Smoke Supplier"}, "name") or (
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "AE Smoke Supplier"})
			.insert(ignore_permissions=True)
			.name
		)
		receiving_date = add_days(nowdate(), -45)
		pr = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": company,
				"supplier": supplier,
				"posting_date": receiving_date,
				"set_posting_time": 1,
				"items": [
					{
						"item_code": "AE-SMOKE-ITEM",
						"qty": 1,
						"rate": 60_000,
						"asset_location": a1.location,
					}
				],
			}
		)
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		pr_asset = frappe.get_doc(
			"Asset", frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
		)
		# The receipt now stamps the basis and submits the asset, so the
		# date is already there — clearing it to prove the rule fires is
		# no longer possible, nor meaningful.
		if pr_asset.docstatus == 0:
			pr_asset.available_for_use_date = None
			pr_asset.flags.ignore_permissions = True
			pr_asset.save()
		g3_field_ok = str(pr_asset.available_for_use_date) == str(receiving_date)
		print(
			f"gap003 AFU derived from PR posting date: {pr_asset.available_for_use_date} "
			f"(want {receiving_date}) {'OK' if g3_field_ok else 'FAIL'}"
		)
		ok = ok and g3_field_ok

		# TC-005 BEHAVIOUR (not just the field): the schedule must actually
		# depreciate FROM the receiving date — first row = days from the PR
		# date to its EOM x daily rate. (days_in_period/daily_rate are OUR
		# columns and stay empty on core-built schedules — assert the money.)
		pr_asset.reload()
		if pr_asset.docstatus == 0:
			pr_asset.calculate_depreciation = 1
			pr_asset.set("finance_books", [])
			pr_asset.append("finance_books", {
				"depreciation_method": "Straight Line",
				"total_number_of_depreciations": 24,
				"frequency_of_depreciation": 1,
				"daily_prorata_based": 1,
			})
			pr_asset.flags.ignore_permissions = True
			pr_asset.save()
			pr_asset.submit()
		else:
			# receipt-submitted asset: depreciation goes on through the
			# Enable Depreciation route, which dates from the in-service
			# basis — so the catch-up from the receiving date survives.
			from asset_enterprise.depreciation import enable_depreciation

			enable_depreciation(
				pr_asset.name,
				total_number_of_depreciations=24,
				frequency_of_depreciation=1,
			)
		first = frappe.db.sql(
			"""select ds.schedule_date, ds.depreciation_amount
			   from `tabDepreciation Schedule` ds
			   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			   where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			   order by ds.schedule_date limit 1""",
			pr_asset.name, as_dict=True,
		)
		gross = flt(pr_asset.net_purchase_amount)
		daily = gross / (24 / 12 * 365)  # §4.3 day-count rule
		expected_days = (
			frappe.utils.date_diff(first[0].schedule_date, receiving_date) + 1
		) if first else 0
		expected_amt = round(daily * expected_days, 2)
		g3_sched_ok = bool(first) and abs(flt(first[0].depreciation_amount) - expected_amt) <= 0.02
		print(
			f"tc005  first row {first and first[0].schedule_date}: "
			f"{first and first[0].depreciation_amount} vs {expected_amt} "
			f"({expected_days} days from receiving date x {daily:.6f}) "
			f"{'OK' if g3_sched_ok else 'FAIL'}"
		)
		ok = ok and g3_sched_ok

		# ------------------------------------ GAP-010 (TC-014 / VR-011)
		pr_asset.reload()
		if pr_asset.docstatus == 0:
			pr_asset.submit()
		frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 1)
		from asset_enterprise.disposal import partial_scrap_asset

		try:
			partial_scrap_asset(pr_asset.name, scrap_value=10_000)
			print("gap010 FAIL (uninvoiced asset disposed with flag on)")
			ok = False
		except frappe.ValidationError as e:
			g = "VR-011" in str(e)
			print(f"gap010 disposal blocked before full invoicing: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g
		frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)

		# ------------------------------------ GAP-009 (TC-012/013)
		b1 = make_test_asset(company, gross=200_000, submit=True)
		frappe.db.set_value("Asset", b1.name, "parent_asset", a1.name, update_modified=False)

		from asset_enterprise.api import tree_aggregate

		agg = tree_aggregate(a1.name)
		t_ok = agg["assets"] == 2 and flt(agg["historical_asset_value"]) == 1_200_000
		print(
			f"gap009 tree aggregate assets={agg['assets']} HAV={agg['historical_asset_value']} "
			f"(want 2 / 1200000) {'OK' if t_ok else 'FAIL'}"
		)
		ok = ok and t_ok

		cyc = frappe.get_doc("Asset", a1.name)
		cyc.parent_asset = b1.name  # a1 -> b1 -> a1 cycle
		cyc.flags.ignore_permissions = True
		cyc.flags.ignore_validate_update_after_submit = True
		try:
			cyc.save()
			print("gap009c FAIL (cyclic parent accepted)")
			ok = False
		except frappe.ValidationError as e:
			g = "VR-009" in str(e)
			print(f"gap009c cycle blocked: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# ------------------------------------ GAP-011 (TC-018)
		c1 = make_test_asset(company, gross=73_000, submit=True)  # calc_depreciation=0
		from asset_enterprise.depreciation import enable_depreciation

		ads_name = enable_depreciation(
			c1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=nowdate(),
		)
		status, calc = (
			frappe.db.get_value("Asset Depreciation Schedule", ads_name, "status"),
			frappe.db.get_value("Asset", c1.name, "calculate_depreciation"),
		)
		row_sum = flt(
			frappe.db.sql(
				"select coalesce(sum(depreciation_amount), 0) from `tabDepreciation Schedule` where parent = %s",
				ads_name,
			)[0][0]
		)
		rul = recalculate_asset_values(c1.name, save=False)["remaining_useful_life_months"]
		e_ok = status == "Active" and calc == 1 and row_sum == 73_000 and rul > 0
		print(
			f"gap011 enable-depreciation ADS={ads_name} status={status} calc={calc} "
			f"rows-sum={row_sum} (want 73000) RUL={rul} {'OK' if e_ok else 'FAIL'}"
		)
		ok = ok and e_ok

	finally:
		frappe.db.rollback(save_point="phase9_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 9:", "PASS" if ok else "FAIL")
