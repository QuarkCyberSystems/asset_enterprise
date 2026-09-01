"""Phase 3 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase3.run

Math checks reproduce the client's xlsx FA_Dep_Simulation figures
(TC-015/016/017 of GA-0005-01 v2.14 §11). DB-mutating checks run in a
savepoint and roll back.
"""

import traceback

import frappe
from frappe.utils import flt, getdate


def _verify_desk_endpoints():
	"""Resolve every method the app's JS calls, plus both sides of each
	override_whitelisted_methods pair, and assert frappe would allow the
	call. Endpoints are read from the shipped JS so a new button is
	covered the day it is added."""
	import os
	import re

	from frappe import is_whitelisted

	paths = set()
	js_dir = frappe.get_app_path("asset_enterprise", "public", "js")
	for name in os.listdir(js_dir):
		if not name.endswith(".js"):
			continue
		with open(os.path.join(js_dir, name)) as handle:
			paths.update(
				re.findall(r'(?:method|query):\s*"([a-zA-Z_0-9.]+)"', handle.read())
			)

	# both sides of the redirect: the desk calls the core path, frappe
	# hands off to ours — either being unregistered breaks the button.
	own_overrides = frappe.get_hooks(
		"override_whitelisted_methods", app_name="asset_enterprise"
	) or {}
	for core_path, ours in own_overrides.items():
		paths.add(core_path)
		paths.update(ours if isinstance(ours, list) else [ours])

	# monkeypatched core functions the desk still calls by their own path
	paths.add("erpnext.assets.doctype.asset.depreciation.make_depreciation_entry")

	failures = []
	for path in sorted(paths):
		try:
			fn = frappe.get_attr(path)
		except Exception as e:
			failures.append(f"{path} — unresolvable ({type(e).__name__})")
			continue
		try:
			is_whitelisted(fn)
		except Exception as e:
			failures.append(
				f"{path} -> {fn.__module__}.{fn.__name__} — {type(e).__name__}: {str(e)[:60]}"
			)
	print(f"desk   {len(paths)} button endpoints resolve and are callable: "
	      f"{'OK' if not failures else 'FAIL'}")
	for line in failures:
		print(f"       {line}")
	return not failures


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()


def _run():
	ok = True
	from asset_enterprise.setup.test_fixtures import pick_company
	company = pick_company()

	from asset_enterprise.depreciation import (
		build_daily_rate_rows,
		build_prospective_rows,
		daily_rate,
		is_prior_fiscal_year,
		split_period_for_cc_change,
	)

	# ------------------------------------------------ TC-015: baseline math
	# 10M, UL 1825 days, start 01/01/2017, EOM.
	rate = daily_rate(10_000_000, 1825)
	print(f"tc015  daily rate = {rate:.6f} (want 5479.452055)")
	ok = ok and abs(rate - 5479.452055) < 0.000001

	rows = build_daily_rate_rows(10_000_000, "2017-01-01", 1825, company)
	jan, feb = rows[0], rows[1]
	checks = [
		("Jan 2017 row", jan["amount"], 169_863.01, jan["days_in_period"], 31),
		("Feb 2017 row", feb["amount"], 153_424.66, feb["days_in_period"], 28),
	]
	for label, got, want, days, want_days in checks:
		match = got == want and days == want_days
		print(f"tc015  {label}: {got} over {days}d (want {want}/{want_days}d) {'OK' if match else 'FAIL'}")
		ok = ok and match

	total = flt(sum(r["amount"] for r in rows), 2)
	last = rows[-1]
	print(
		f"tc015  rows={len(rows)} last={last['schedule_date']} ({last['days_in_period']}d, {last['amount']}); "
		f"sum={total} (want 10,000,000 exactly) {'OK' if total == 10_000_000 else 'FAIL'}"
	)
	ok = ok and total == 10_000_000 and len(rows) == 60

	# --------------------------------- TC-016: prospective recalc (upward)
	# The client's FA_Dep_Simulation quotes 6,164.383562 — 9,000,000 over
	# 1,460 days, a 365-day year with 29/02/2020 excluded. That was CH-12,
	# and this line keeps the figure on the record as arithmetic.
	prate = daily_rate(9_000_000, 1460)
	print(f"tc016  xlsx rate (CH-12 365-basis) = {prate:.6f} (want 6164.383562)")
	ok = ok and abs(prate - 6164.383562) < 0.000001

	# CH-12 AMENDED 2026-09-01 (Vivek's decision): the denominator is the
	# ACTUAL calendar days held, so the leap day is priced into every row
	# instead of landing on the final one. The span 01/01/2018-31/12/2021
	# contains 29/02/2020, so it is 1,461 days, not 1,460:
	#
	#     9,000,000 / 1,461 = 6,160.164271/day
	#     31d = 190,965.09   (the xlsx, on the old basis, said 191,095.89)
	#     28d = 172,484.60   (the xlsx said 172,602.74)
	#
	# The DIFFERENCE FROM THE CLIENT WORKBOOK IS DELIBERATE and recorded
	# in the design's CH-12 note. Both bases still total 9,000,000 exactly
	# — §4.10 absorption sees to that; only the distribution moves.
	prows = build_prospective_rows(9_000_000, "2017-12-31", "2021-12-31", company)
	pjan, pfeb = prows[0], prows[1]
	m1 = pjan["amount"] == 190_965.09 and pjan["days_in_period"] == 31
	m2 = pfeb["amount"] == 172_484.60 and pfeb["days_in_period"] == 28
	print(f"tc016  Jan 2018: {pjan['amount']} ({pjan['days_in_period']}d) want 190965.09/31d "
	      f"[xlsx on the superseded basis: 191095.89] {'OK' if m1 else 'FAIL'}")
	print(f"tc016  Feb 2018: {pfeb['amount']} ({pfeb['days_in_period']}d) want 172484.60/28d "
	      f"[xlsx: 172602.74] {'OK' if m2 else 'FAIL'}")
	ptotal = flt(sum(r["amount"] for r in prows), 2)
	print(f"tc016  prospective sum = {ptotal} (want 9,000,000 exactly) {'OK' if ptotal == 9_000_000 else 'FAIL'}")
	ok = ok and m1 and m2 and ptotal == 9_000_000

	# ------------------------------------------- TC-019: catch-up first row
	# Basis 01/02/2025, first posting 30/06/2025 -> first row spans 150 days.
	crows = build_daily_rate_rows(
		8_500_000, "2025-02-01", 1095, company, first_posting_date="2025-06-30"
	)
	cdays = crows[0]["days_in_period"]
	print(f"tc019  catch-up first row spans {cdays}d (want 150) {'OK' if cdays == 150 else 'FAIL'}")
	ctotal = flt(sum(r["amount"] for r in crows), 2)
	print(f"tc019  catch-up sum = {ctotal} (want 8,500,000) {'OK' if ctotal == 8_500_000 else 'FAIL'}")
	ok = ok and cdays == 150 and ctotal == 8_500_000

	# ------------------------------------------------------ GAP-021 CC split
	old_cc, new_cc = split_period_for_cc_change(10_000, 30, 15, company)
	print(f"gap021 CC split 10000/30d at day 15 -> {old_cc} + {new_cc} {'OK' if (old_cc, new_cc) == (5000.0, 5000.0) else 'FAIL'}")
	ok = ok and (old_cc, new_cc) == (5000.0, 5000.0)

	# ------------------------------------------------------------- §4.7 PYA
	# Site may lack prior FY records — create temp FYs inside a savepoint.
	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	frappe.db.savepoint("phase3_pya")
	try:
		from erpnext.accounts.utils import get_fiscal_year

		for yr in ("2024", "2025"):
			# Guard by DATE COVERAGE, not name — live sites may already
			# have an FY covering the year under another name.
			try:
				get_fiscal_year(f"{yr}-06-30", as_dict=True)
			except Exception:
				frappe.get_doc(
					{
						"doctype": "Fiscal Year",
						"year": yr,
						"year_start_date": f"{yr}-01-01",
						"year_end_date": f"{yr}-12-31",
					}
				).insert(ignore_permissions=True)
		pya = is_prior_fiscal_year("2024-06-30", "2026-07-16")
		same = is_prior_fiscal_year("2026-07-01", "2026-07-16")
		print(f"pya    prior-FY row -> {pya} (want True); same-FY row -> {same} (want False)")
		ok = ok and pya and not same
	finally:
		frappe.db.rollback(save_point="phase3_pya")

	# ---------------------------------------------- patches wired at import
	import erpnext.assets.doctype.asset.depreciation as core_depr
	import erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule as core_ads

	w1 = getattr(core_depr.post_depreciation_entries, "_asset_enterprise_wrapper", False)
	w2 = getattr(core_ads.reschedule_depreciation, "_asset_enterprise_wrapper", False)
	print(f"patch  post_depreciation_entries wrapped: {'OK' if w1 else 'FAIL'}; reschedule wrapped: {'OK' if w2 else 'FAIL'}")
	ok = ok and w1 and w2

	# ---- every endpoint a BUTTON calls must survive the desk's own gate.
	# Wrapping a core function with an undecorated replacement makes the
	# button fail with "Method Not Allowed" while every server-side test
	# keeps passing — exactly how the Make Depreciation Entry button broke
	# (client report 16/08/2026). Assert reachability the way the desk
	# resolves it, not the way our suites call it.
	callable_ok = _verify_desk_endpoints()
	ok = ok and callable_ok

	# -------------------------- supersession round-trip (savepoint rollback)
	from asset_enterprise.depreciation import supersede_and_regenerate
	from asset_enterprise.setup.test_fixtures import make_test_asset

	frappe.db.savepoint("phase3_verify")
	try:
		asset = make_test_asset(company, gross=120_000, submit=False, with_depreciation=True)
		asset.submit()
		ads = frappe.db.get_value(
			"Asset Depreciation Schedule",
			{"asset": asset.name, "status": "Active", "docstatus": 1},
			"name",
		)
		print(f"super  fixture {asset.name} -> active schedule {ads} {'OK' if ads else 'FAIL'}")
		ok = ok and bool(ads)
		if ads:
			# A schedule that never booked anything is a working copy —
			# regenerating over it must leave no Superseded leftover.
			interim = supersede_and_regenerate(
				asset.name, as_of_date=frappe.utils.nowdate(), reason="Phase3 unposted"
			)
			gone = not frappe.db.exists("Asset Depreciation Schedule", ads)
			print(
				f"super  unposted schedule {ads} removed, not superseded: {'OK' if gone else 'FAIL'}"
			)
			ok = ok and gone

			# Once an entry is posted the schedule IS history — supersede it.
			from erpnext.assets.doctype.asset.depreciation import make_depreciation_entry

			first_row = interim.depreciation_schedule[0]
			make_depreciation_entry(interim.name, str(first_row.schedule_date))
			interim.reload()

			new = supersede_and_regenerate(
				asset.name, as_of_date=frappe.utils.nowdate(), reason="Phase3 smoke"
			)
			old_status = frappe.db.get_value("Asset Depreciation Schedule", interim.name, "status")
			old_docstatus = frappe.db.get_value(
				"Asset Depreciation Schedule", interim.name, "docstatus"
			)
			link_ok = new.supersedes == interim.name
			rows_n = len(new.depreciation_schedule)
			future_sum = flt(sum(r.depreciation_amount for r in new.depreciation_schedule), 2)
			from asset_enterprise.asset_values import recalculate_asset_values

			values = recalculate_asset_values(asset.name, save=False)
			total_want = flt(values["net_book_value"]) + flt(values["accumulated_depreciation_value"])
			print(
				f"super  posted schedule: status={old_status} docstatus={old_docstatus} "
				f"(want Superseded/1) "
				f"{'OK' if old_status == 'Superseded' and old_docstatus == 1 else 'FAIL'}"
			)
			print(
				f"super  new {new.name}: supersedes-link {'OK' if link_ok else 'FAIL'}; "
				f"rows={rows_n}; sum={future_sum} vs cost {total_want}"
			)
			ok = ok and old_status == "Superseded" and old_docstatus == 1 and link_ok and rows_n > 0
			ok = ok and flt(future_sum, 2) == flt(total_want, 2)
	finally:
		frappe.db.rollback(save_point="phase3_verify")
		leftover = frappe.db.count("Asset", {"asset_name": "AE Smoke Asset"})
		print(f"super  rollback clean: {'OK' if leftover == 0 else 'FAIL'}")
		ok = ok and leftover == 0

	print("PHASE 3:", "PASS" if ok else "FAIL")
