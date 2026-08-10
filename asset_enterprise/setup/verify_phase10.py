"""Phase 10 verification (v2.16 deltas) — run:
bench --site <site> execute asset_enterprise.setup.verify_phase10.run

Covers CH-01 tolerance manual-post + approver, CH-04 qty-only check
(via phase 7), CH-05 Option B (via phase 7), CH-06 RUL snapshot (via
phase 5 + component default here), CH-07 VR-042 NBV gate, CH-08 Path 3
cross-period restore, CH-09 Scrap Transaction + component scrap,
CH-10 VR-037, CH-11 JE link on mass results, CH-12 day-count rule.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate


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
	from asset_enterprise.setup.test_fixtures import make_test_asset

	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	scr_before = frappe.db.count("Scrap Transaction")
	frappe.db.savepoint("phase10_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)

		# Disposal loss chain for the scrap tests.
		if not frappe.db.get_value("Company", company, "disposal_account"):
			expense = frappe.db.get_value(
				"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
			)
			frappe.db.set_value("Company", company, "disposal_account", expense, update_modified=False)

		# ------------------------------------ CH-12: §4.3 day-count rule
		from asset_enterprise.depreciation import day_count_365, enable_depreciation

		d1 = day_count_365("2017-01-01", "2021-12-31")  # real 1826 (2020 leap)
		d2 = day_count_365("2025-02-01", "2028-01-31")  # real 1095, no leap day
		dc_ok = d1 == 1825 and d2 == 1095
		print(f"ch12   day_count_365: 5y={d1} (want 1825) 3y={d2} (want 1095) {'OK' if dc_ok else 'FAIL'}")
		ok = ok and dc_ok

		# Leap-span enable: 24 months over 2019-07-01..2021-06-30 (731 real
		# days, 730 rate days) -> rate = 73000/730 = 100.00 exactly.
		c1 = make_test_asset(company, gross=73_000, submit=True)
		ads1 = enable_depreciation(
			c1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date="2019-07-01",
		)
		first = frappe.db.get_value(
			"Depreciation Schedule",
			{"parent": ads1, "schedule_date": "2019-07-31"},
			["daily_rate", "depreciation_amount"],
			as_dict=True,
		)
		row_sum = flt(frappe.db.sql(
			"select sum(depreciation_amount) from `tabDepreciation Schedule` where parent=%s", ads1
		)[0][0])
		leap_ok = first and abs(flt(first.daily_rate) - 100.0) < 1e-9 and row_sum == 73_000
		print(
			f"ch12b  leap-span rate={first and first.daily_rate} (want 100.0) "
			f"rows-sum={row_sum} (want 73000) {'OK' if leap_ok else 'FAIL'}"
		)
		ok = ok and bool(leap_ok)

		# ------------------------- CH-01: tolerance manual-post + approver
		from asset_enterprise.depreciation import final_row_requires_manual_post, post_final_row

		t1 = make_test_asset(company, gross=20_000, submit=True)
		start = get_first_day(add_months(nowdate(), -2))
		enable_depreciation(
			t1.name, total_number_of_depreciations=2, frequency_of_depreciation=1,
			depreciation_start_date=start,
		)
		final_name = frappe.db.get_value(
			"Depreciation Schedule",
			{"parent": ["in", frappe.get_all(
				"Asset Depreciation Schedule",
				filters={"asset": t1.name, "status": "Active"}, pluck="name")]},
			"name", order_by="schedule_date desc",
		)
		frappe.db.set_value(
			"Depreciation Schedule", final_name, "depreciation_amount",
			flt(frappe.db.get_value("Depreciation Schedule", final_name, "depreciation_amount")) + 50,
			update_modified=False,
		)

		# Authority role so restricted mass mode works.
		frappe.get_doc({
			"doctype": "Asset Settings Authority Role", "parenttype": "Asset Settings",
			"parent": "Asset Settings", "parentfield": "mass_depreciation_authority_roles",
			"role": "System Manager",
		}).db_insert()

		mad = frappe.get_doc({
			"doctype": "Mass Asset Depreciation", "company": company,
			"posting_date": nowdate(), "mode": "Selected Assets",
			"selected_assets": [{"asset": t1.name}],
		})
		mad.flags.ignore_permissions = True
		mad.insert()
		mad.submit()
		results = frappe.get_all(
			"Mass Asset Depreciation Result", filters={"parent": mad.name},
			fields=["outcome", "journal_entry"], order_by="idx",
		)
		posted = [r for r in results if r.outcome == "Posted"]
		manual = [r for r in results if r.outcome == "Manual Posting Required"]
		m_ok = len(posted) == 1 and posted[0].journal_entry and len(manual) == 1
		print(
			f"ch01   mass: posted={len(posted)} (JE link={bool(posted and posted[0].journal_entry)}) "
			f"manual-required={len(manual)} {'OK' if m_ok else 'FAIL: ' + str(results)}"
		)
		ok = ok and bool(m_ok)

		# Manual post without override -> blocked.
		try:
			post_final_row(t1.name)
			print("ch01b  FAIL (beyond-tolerance final row posted without override)")
			ok = False
		except frappe.ValidationError as e:
			g = "Tolerance" in str(e)
			print(f"ch01b  manual post blocked without override: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# Approver configured; Administrator holds System Manager -> allowed.
		tol_row = frappe.db.get_value(
			"Asset Settings Tolerance", {"parent": "Asset Settings", "company": company}, "name"
		)
		if tol_row:
			frappe.db.set_value("Asset Settings Tolerance", tol_row, "tolerance_approver",
				"System Manager", update_modified=False)
		else:
			frappe.get_doc({
				"doctype": "Asset Settings Tolerance", "parenttype": "Asset Settings",
				"parent": "Asset Settings", "parentfield": "tolerance_settings",
				"company": company, "tolerance_approver": "System Manager",
			}).db_insert()
		je = post_final_row(t1.name, override_tolerance=1)
		print(f"ch01c  override + approver role posts final row: JE={je} {'OK' if je else 'FAIL'}")
		ok = ok and bool(je)

		# ------------------------- CH-09: Scrap Transaction (direct + auto)
		s1 = make_test_asset(company, gross=50_000, submit=True)
		tx = frappe.get_doc({
			"doctype": "Scrap Transaction", "asset": s1.name, "company": company,
			"transaction_date": nowdate(), "scrap_type": "Partial Scrap",
			"scrapping_type": "Damage", "mode": "By Value", "scrap_value": 5_000,
		})
		tx.flags.ignore_permissions = True
		tx.insert()
		tx.submit()
		tx_je = frappe.db.get_value("Scrap Transaction", tx.name, "journal_entry")
		ft = frappe.db.exists("Financial Treatment", {
			"asset": s1.name, "transaction_category": "Disposal", "status": "Posted"})
		count_s1 = frappe.db.count("Scrap Transaction", {"asset": s1.name})
		tx_ok = bool(tx_je) and bool(ft) and count_s1 == 1
		print(f"ch09   direct Scrap Transaction: JE={tx_je} FT={bool(ft)} records={count_s1} (want 1) {'OK' if tx_ok else 'FAIL'}")
		ok = ok and tx_ok

		try:
			tx.reload()
			tx.cancel()
			print("ch09b  FAIL (Scrap Transaction cancelled)")
			ok = False
		except frappe.ValidationError:
			print("ch09b  Scrap Transaction cancel blocked: OK")

		# Auto-record when scrapping via the (core-button) endpoint.
		from asset_enterprise import disposal

		s2 = make_test_asset(company, gross=20_000, submit=True)
		disposal.scrap_asset(s2.name, scrapping_type="Damage")
		auto = frappe.db.get_value(
			"Scrap Transaction", {"asset": s2.name, "docstatus": 1},
			["scrap_type", "journal_entry"], as_dict=True,
		)
		a_ok = auto and auto.scrap_type == "Full Scrap" and auto.journal_entry
		print(f"ch09c  auto-recorded on endpoint scrap: {auto} {'OK' if a_ok else 'FAIL'}")
		ok = ok and bool(a_ok)

		# Component scrap: fake merge-log row (real merge covered phase 5),
		# component picker defaults the value from the snapshot.
		comp = make_test_asset(company, gross=80_000, submit=True)
		frappe.get_doc({
			"doctype": "Composite Merge Log Entry", "parenttype": "Asset",
			"parent": comp.name, "parentfield": "merge_log", "idx": 1,
			"merged_source_asset": s2.name, "merged_source_asset_name": "AE Smoke Component",
			"merged_date": nowdate(), "historical_value_at_merge": 10_000,
			"accumulated_depreciation_at_merge": 3_000, "net_book_value_at_merge": 7_000,
			"remaining_useful_life_in_months": 14, "remaining_useful_life_in_years": 1.17,
			"status": "Active",
		}).db_insert()
		ctx = frappe.get_doc({
			"doctype": "Scrap Transaction", "asset": comp.name, "company": company,
			"transaction_date": nowdate(), "scrap_type": "Partial Scrap",
			"scrapping_type": "Damage", "composite_component": s2.name,
		})
		ctx.flags.ignore_permissions = True
		ctx.insert()
		ctx.submit()
		hav_after = recalculate_asset_values(comp.name, save=False)["historical_asset_value"]
		row_status = frappe.db.get_value(
			"Composite Merge Log Entry",
			{"parent": comp.name, "merged_source_asset": s2.name}, "status",
		)
		c_ok = flt(hav_after) == 73_000 and row_status == "Scrapped" and flt(ctx.scrap_value) == 7_000
		print(
			f"ch09d  component scrap: defaulted value={ctx.scrap_value} (want 7000) HAV after={hav_after} "
			f"(want 73000) merge-log row={row_status} {'OK' if c_ok else 'FAIL'}"
		)
		ok = ok and c_ok

		# ------------------------- CH-08: Path 3 cross-period restore
		r1 = make_test_asset(company, gross=36_500, submit=True)
		enable_depreciation(
			r1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(add_months(nowdate(), 3)),  # future: no due rows
		)
		disposal_date = add_days(get_first_day(nowdate()), -10)  # previous month
		disposal.scrap_asset(r1.name, scrap_date=disposal_date, scrapping_type="Damage")
		st = frappe.db.get_value("Asset", r1.name, "status")
		from asset_enterprise.restore import cross_period_restore

		mirror = cross_period_restore(r1.name)
		after = frappe.db.get_value(
			"Asset", r1.name, ["status", "scrap_reversal_journal_entry"], as_dict=True)
		values_back = recalculate_asset_values(r1.name, save=False)
		# HAV must fully return (disposal FT paired); a small pre-disposal
		# proration may legitimately remain in Accum.
		hav_back = flt(values_back["historical_asset_value"])
		accum_left = flt(values_back["accumulated_depreciation_value"])
		catchup = frappe.db.sql(
			"""
			select ds.schedule_date, ds.days_in_period from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			  and ifnull(ds.journal_entry,'') = ''
			order by ds.schedule_date limit 1
			""",
			r1.name, as_dict=True,
		)
		p3_ok = (
			st == "Scrapped"
			and after.status != "Scrapped"
			and after.scrap_reversal_journal_entry == mirror
			and hav_back == 36_500
			and accum_left < 1_000
			and catchup
			and getdate(catchup[0].schedule_date) == getdate(get_last_day(nowdate()))
			and flt(catchup[0].days_in_period) > 31
		)
		print(
			f"ch08   path3: status {st}->{after.status} mirror={mirror} HAV={hav_back} (want 36500) "
			f"accum-left={accum_left} catchup-row={catchup and catchup[0].schedule_date} "
			f"days={catchup and catchup[0].days_in_period} {'OK' if p3_ok else 'FAIL'}"
		)
		ok = ok and bool(p3_ok)

		# ------------------------- CH-07: VR-042 NBV-coverage gate (AVA)
		v1 = make_test_asset(company, gross=100_000, submit=True, with_depreciation=True)
		diff_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Liability", "is_group": 0}, "name")
		ava = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": v1.name, "company": company,
			"date": nowdate(), "transaction_type": "Upward Revaluation",
			"current_asset_value": 100_000, "new_asset_value": 150_000,
			"difference_account": diff_account,
			"cost_center": frappe.db.get_value("Asset", v1.name, "cost_center"),
		})
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		disposal.partial_scrap_asset(v1.name, scrap_value=120_000, scrapping_type="Damage")
		nbv_v1 = recalculate_asset_values(v1.name, save=False)["net_book_value"]
		try:
			ava.reload()
			ava.cancel()
			print(f"ch07   FAIL (AVA reversal allowed with NBV {nbv_v1} < 50000)")
			ok = False
		except frappe.ValidationError as e:
			g = "VR-042" in str(e)
			print(f"ch07   VR-042 gate blocked AVA reversal (NBV {nbv_v1}): {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# ------------------------- CH-10: VR-037 Capitalized target allowed
		cap_target = make_test_asset(company, gross=30_000, submit=True)
		frappe.db.set_value("Asset", cap_target.name, "status", "Capitalized", update_modified=False)
		probe = frappe.get_doc({
			"doctype": "Asset Capitalization", "transaction_type": "Capitalized Maintenance",
			"target_asset": cap_target.name, "company": company,
			"asset_items": [{"asset": c1.name}],
		})
		try:
			probe._validate_cm()
			print("ch10   Capitalized target accepted for CM: OK")
		except frappe.ValidationError as e:
			print(f"ch10   FAIL (Capitalized target rejected): {e}")
			ok = False

	finally:
		frappe.db.rollback(save_point="phase10_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		left_tx = frappe.db.count("Scrap Transaction") - scr_before
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(
			f"clean  rollback: leftovers={left} scrap-tx={left_tx} switch={switch} "
			f"{'OK' if left == 0 and left_tx == 0 and switch == switch_before else 'FAIL'}"
		)
		ok = ok and left == 0 and left_tx == 0 and switch == switch_before

	print("PHASE 10:", "PASS" if ok else "FAIL")
