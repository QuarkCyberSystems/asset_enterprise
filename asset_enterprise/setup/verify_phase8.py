"""Phase 8 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase8.run"""

import traceback

import frappe
from frappe.utils import add_days, add_months, cint, flt, get_first_day, get_last_day, getdate, nowdate


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
	frappe.db.savepoint("phase8_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		# deterministic VR-006 negative: no authority rows during this test
		# (delete happens inside the savepoint — rolled back afterwards).
		frappe.db.delete("Asset Settings Authority Role", {"parent": "Asset Settings"})

		# ----------------------------------- Mass Asset Depreciation (GAP-005)
		a1 = make_test_asset(company, gross=73_000, submit=False, with_depreciation=True)
		# Backdate depreciation start so one monthly row is due.
		a1.finance_books[0].depreciation_start_date = add_months(nowdate(), -1)
		a1.flags.ignore_permissions = True
		a1.save()
		a1.submit()

		mad = frappe.get_doc(
			{
				"doctype": "Mass Asset Depreciation",
				"company": company,
				"posting_date": nowdate(),
				"mode": "All Eligible",
			}
		)
		mad.flags.ignore_permissions = True
		mad.insert()
		mad.submit()
		results = frappe.get_all(
			"Mass Asset Depreciation Result",
			filters={"parent": mad.name},
			fields=["asset", "outcome", "reason"],
		)
		posted = [r for r in results if r.outcome == "Posted" and r.asset == a1.name]
		m_ok = bool(posted)
		print(f"mad    results={[(r.asset, r.outcome) for r in results]} {'OK' if m_ok else 'FAIL'}")
		ok = ok and m_ok

		# VR-007 document level: an identical re-run has nothing to post and
		# must be REFUSED, not recorded as an empty submitted run (client,
		# MAD-2026-00475 duplicating MAD-2026-00435). The one legitimate
		# quiet outcome is a site whose only remaining due rows need the
		# manual §4.10 flow — that run carries information and may submit.
		# ignore_no_copy=False mirrors the UI's Duplicate action, which
		# honours no_copy (server-side copy_doc ignores it by default).
		dup_draft = frappe.copy_doc(mad, ignore_no_copy=False)
		copy_ok = not dup_draft.get("result_summary")
		print(f"vr007a Duplicate carries no result rows: {'OK' if copy_ok else 'FAIL'}")
		ok = ok and copy_ok

		mad2 = frappe.get_doc(
			{
				"doctype": "Mass Asset Depreciation",
				"company": company,
				"posting_date": nowdate(),
				"mode": "All Eligible",
			}
		)
		mad2.flags.ignore_permissions = True
		mad2.insert()
		try:
			mad2.submit()
			results2 = frappe.get_all(
				"Mass Asset Depreciation Result",
				filters={"parent": mad2.name},
				fields=["outcome"],
			)
			outcomes2 = {r.outcome for r in results2}
			# submitted without throwing: only acceptable when manual-flow
			# rows justified the document — and never with a Posted row.
			m2_ok = "Posted" not in outcomes2 and "Manual Posting Required" in outcomes2
			print(
				f"vr007b duplicate run submitted with outcomes {sorted(outcomes2)} "
				f"{'OK (manual rows justify it)' if m2_ok else 'FAIL (empty duplicate accepted)'}"
			)
		except frappe.ValidationError as e:
			m2_ok = "VR-007" in str(e)
			print(f"vr007b duplicate run blocked: {'OK' if m2_ok else 'FAIL: ' + str(e)[:120]}")
		ok = ok and m2_ok

		# VR-006: restricted mode without authority role.
		mad3 = frappe.get_doc(
			{
				"doctype": "Mass Asset Depreciation",
				"company": company,
				"posting_date": nowdate(),
				"mode": "Selected Assets",
				"selected_assets": [{"asset": a1.name}],
			}
		)
		mad3.flags.ignore_permissions = True
		mad3.insert()
		try:
			mad3.submit()
			print("vr006  FAIL (restricted mode without authority)")
			ok = False
		except frappe.ValidationError:
			print("vr006  restricted mode blocked without authority role: OK")

		# FT recorded by the engine posting.
		ft = frappe.db.exists(
			"Financial Treatment",
			{"asset": a1.name, "transaction_category": "Depreciation", "status": "Posted"},
		)
		print(f"engine depreciation FT recorded: {'OK' if ft else 'FAIL'}")
		ok = ok and bool(ft)

		# ------------------------------------------- Movement (GAP-020/021/022)
		emp = frappe.db.get_value("Employee", {"company": company}, "name")
		loc2 = frappe.db.get_value("Location", {"name": ["!=", a1.location]}, "name")
		if not loc2:
			loc2 = (
				frappe.get_doc({"doctype": "Location", "location_name": "AE Smoke Loc2"})
				.insert(ignore_permissions=True)
				.name
			)
		# attribution needs a REAL starting cost centre on the asset —
		# without one the old half of the split is untagged and the test
		# cannot see a mistag (which is how the 20/08 defect survived).
		leaf_ccs = frappe.get_all(
			"Cost Center", filters={"company": company, "is_group": 0}, pluck="name", limit=2
		)
		cc1, cc2 = leaf_ccs[0], leaf_ccs[1]
		frappe.db.set_value("Asset", a1.name, "cost_center", cc1, update_modified=False)
		prior_cc = cc1

		mv = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": company,
				"purpose": "Transfer",
				"transaction_date": frappe.utils.now(),
				"assets": [
					{
						"asset": a1.name,
						"target_location": loc2,
						**({"to_employee": emp} if emp else {}),
						"target_cost_center": cc2,
					}
				],
			}
		)
		mv.flags.ignore_permissions = True
		mv.insert()
		mv.submit()

		after = frappe.db.get_value(
			"Asset", a1.name, ["location", "custodian", "cost_center"], as_dict=True
		)
		combo_ok = after.location == loc2 and after.cost_center == cc2 and (
			not emp or after.custodian == emp
		)
		print(
			f"move   loc={after.location == loc2} emp={'n/a' if not emp else after.custodian == emp} "
			f"cc={after.cost_center == cc2} in ONE movement {'OK' if combo_ok else 'FAIL'}"
		)
		ok = ok and combo_ok

		# GAP-021: the transfer period is split at POSTING time into ONE
		# entry with a debit per cost centre — the design's shape:
		#   DR Depreciation Expense (Old CC) / DR (New CC) / CR Accum.
		# The schedule itself is NOT split: a row-level split produced two
		# entries with two accum credits and outlived a cancelled
		# movement (client, TC-035).
		from asset_enterprise.depreciation import cost_centre_on, cost_centre_split

		day = getdate(nowdate()).day
		period_start = get_first_day(nowdate())
		period_end = get_last_day(nowdate())
		split = cost_centre_split(a1.name, period_start, period_end, 3100.0, company)
		g21_ok = (
			# before the transfer date the OLD centre holds it
			cost_centre_on(a1.name, period_start) == prior_cc
			and cost_centre_on(a1.name, period_end) == cc2
			and (len(split) == 2 if day > 1 else len(split) == 1)
			and {cc for cc, _amt in split} <= {prior_cc, cc2}
			and abs(sum(amt for _cc, amt in split) - 3100.0) < 0.01
		)
		print(
			f"gap021 period split at posting: {[(cc, round(amt, 2)) for cc, amt in split]} "
			f"(want {prior_cc} then {cc2}, summing 3100.00) {'OK' if g21_ok else 'FAIL'}"
		)
		ok = ok and g21_ok

		# schedule rows stay untouched — one row per period, no CC tags
		tagged = frappe.db.sql(
			"""select count(*) from `tabDepreciation Schedule` ds
			   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			   where ads.asset = %s and ads.status='Active' and ads.docstatus=1
			     and ifnull(ds.cost_center,'') != ''""",
			a1.name,
		)[0][0]
		print(f"gap021b schedule left unsplit and untagged: {tagged} tagged rows (want 0) "
		      f"{'OK' if tagged == 0 else 'FAIL'}")
		ok = ok and tagged == 0

		# gap021c (client, TC-035, 20/08): a period that ended BEFORE a
		# transfer must be expensed to the OLD centre however late it is
		# posted. This is the one that kept failing: attribution used to
		# come from the asset's cost_center field, read at posting time,
		# so June and July went to the centre the asset moved to in
		# August (ACC-JV-2026-00893/00894 on ACC-ASS-2026-00154).
		from asset_enterprise.depreciation import _post_one, enable_depreciation

		late = make_test_asset(company, gross=36_000, submit=True)
		frappe.db.set_value("Asset", late.name, "cost_center", prior_cc, update_modified=False)
		enable_depreciation(
			late.name, total_number_of_depreciations=36, frequency_of_depreciation=1,
			available_for_use_date=str(get_first_day(add_months(nowdate(), -2))),
			depreciation_start_date=str(get_first_day(add_months(nowdate(), -2))),
		)
		mv2 = frappe.get_doc({
			"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
			"transaction_date": frappe.utils.now(),
			"assets": [{"asset": late.name, "target_cost_center": cc2}]})
		mv2.flags.ignore_permissions = True
		mv2.insert()
		mv2.submit()
		old_row = frappe.db.sql(
			"""select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
			   ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
			   ds.daily_rate, ds.days_in_period
			from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status='Active' and ads.docstatus=1
			  and ifnull(ds.journal_entry,'')='' and ds.schedule_date < %s
			order by ds.schedule_date limit 1""",
			(late.name, get_first_day(nowdate())), as_dict=True,
		)[0]
		_post_one(old_row, getdate(nowdate()))
		je = frappe.db.get_value("Depreciation Schedule", old_row.row_name, "journal_entry")
		legs = frappe.get_all(
			"Journal Entry Account", filters={"parent": je, "debit": (">", 0)},
			fields=["account", "debit", "cost_center"])
		g21c_ok = bool(legs) and all(l.cost_center == prior_cc for l in legs)
		print(
			f"gap021c pre-transfer period posted AFTER the transfer: JE {je} debits "
			f"{[(l.cost_center, l.debit) for l in legs]} (want all on {prior_cc}, "
			f"asset now sits in {cc2}) {'OK' if g21c_ok else 'FAIL'}"
		)
		ok = ok and g21c_ok

		# gap021d (client, 24/08): the transfer posts nothing of its own, so
		# the user is told BEFORE submitting what it will do to
		# depreciation — the period split, and that earlier unposted
		# periods stay with the old centre.
		from asset_enterprise.api import movement_cost_centre_impact

		warn_asset = make_test_asset(company, gross=36_000, submit=True)
		frappe.db.set_value("Asset", warn_asset.name, "cost_center", prior_cc, update_modified=False)
		enable_depreciation(
			warn_asset.name, total_number_of_depreciations=36, frequency_of_depreciation=1,
			available_for_use_date=str(get_first_day(add_months(nowdate(), -2))),
			depreciation_start_date=str(get_first_day(add_months(nowdate(), -2))),
		)
		mv3 = frappe.get_doc({
			"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
			"transaction_date": frappe.utils.now(),
			"assets": [{"asset": warn_asset.name, "target_cost_center": cc2}]})
		mv3.flags.ignore_permissions = True
		mv3.insert()   # still a DRAFT — this is when the form warns
		preview = movement_cost_centre_impact(warn_asset.name, nowdate(), cc2)
		mv3.submit()
		note = frappe.db.sql(
			"""select content from `tabComment` where reference_doctype='Asset Movement'
			   and reference_name=%s and comment_type='Comment'
			   and content like '%%Effect on depreciation%%' limit 1""",
			mv3.name,
		)
		g21d_ok = (
			preview.get("old_cost_center") == prior_cc
			and preview.get("new_cost_center") == cc2
			# two months of unposted history plus the period being split
			and bool(preview.get("split"))
			and len(preview.get("earlier_unposted") or []) >= 1
			and bool(note)
		)
		print(
			f"gap021d pre-submit warning: preview {prior_cc} -> {cc2}, "
			f"split={bool(preview.get('split'))}, "
			f"earlier-unposted={len(preview.get('earlier_unposted') or [])}; "
			f"recorded on the movement={bool(note)} {'OK' if g21d_ok else 'FAIL'}"
		)
		ok = ok and g21d_ok

		# gap021e: any number of moves inside one period, and attribution
		# must survive a REGENERATION (a value change rebuilds future rows
		# from scratch — they used to come back untagged and fall back to
		# the asset's current centre).
		cc3 = frappe.get_all(
			"Cost Center",
			filters={"company": company, "is_group": 0, "name": ["not in", [prior_cc, cc2]]},
			pluck="name", limit=1,
		)
		if cc3:
			cc3 = cc3[0]
			multi = make_test_asset(company, gross=36_000, submit=True)
			frappe.db.set_value("Asset", multi.name, "cost_center", prior_cc, update_modified=False)
			enable_depreciation(
				multi.name, total_number_of_depreciations=36, frequency_of_depreciation=1,
				available_for_use_date=str(get_first_day(nowdate())),
				depreciation_start_date=str(get_first_day(nowdate())),
			)
			start = get_first_day(nowdate())
			for target, day in ((cc2, 10), (cc3, 20)):
				m = frappe.get_doc({
					"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
					"transaction_date": f"{add_days(start, day - 1)} 10:00:00",
					"assets": [{"asset": multi.name, "target_cost_center": target}]})
				m.flags.ignore_permissions = True
				m.insert()
				m.submit()
			amount = 3100.0
			split = cost_centre_split(multi.name, start, get_last_day(nowdate()), amount, company)
			three_ok = (
				len(split) == 3
				and [cc for cc, _a in split] == [prior_cc, cc2, cc3]
				and abs(sum(a for _cc, a in split) - amount) < 0.01
			)
			# a value change regenerates the future rows; attribution must
			# still come from the history afterwards
			from asset_enterprise.depreciation import supersede_and_regenerate

			supersede_and_regenerate(
				multi.name, as_of_date=start, reason="gap021e regeneration probe")
			after = cost_centre_split(multi.name, start, get_last_day(nowdate()), amount, company)
			g21e_ok = three_ok and after == split
			print(
				f"gap021e two moves in one period: {[(cc, round(a, 2)) for cc, a in split]} "
				f"(want 3 segments summing {amount:,.2f}); unchanged after regeneration="
				f"{after == split} {'OK' if g21e_ok else 'FAIL'}"
			)
			ok = ok and g21e_ok

		# GAP-028: cancel restores prior CC.
		mv.reload()
		mv.cancel()
		restored = frappe.db.get_value("Asset", a1.name, "cost_center")
		# and the ATTRIBUTION reverts with it — a cancelled movement is
		# excluded from the history, so future periods route to the prior
		# centre again. The old row-split left them on the new one.
		after_cancel = cost_centre_split(
			a1.name, get_first_day(nowdate()), get_last_day(nowdate()), 3100.0, company
		)
		g28_ok = (
			restored == prior_cc
			and len(after_cancel) == 1
			and after_cancel[0][0] == prior_cc
		)
		print(
			f"gap028 CC restored on cancel: {restored} == {prior_cc}; attribution back to "
			f"{[(cc, round(amt, 2)) for cc, amt in after_cancel]} {'OK' if g28_ok else 'FAIL'}"
		)
		ok = ok and g28_ok

		# --------------------------------------------------------- reports
		from asset_enterprise.asset_enterprise.report.asset_daily_reconciliation.asset_daily_reconciliation import (
			execute as recon,
		)
		from asset_enterprise.asset_enterprise.report.composite_merge_log_report.composite_merge_log_report import (
			execute as mergelog,
		)
		from asset_enterprise.asset_enterprise.report.replacement_chain.replacement_chain import (
			execute as chain,
		)

		c1, r1 = mergelog({})
		c2, r2 = chain({"company": company})
		c3, r3 = recon({"company": company})
		rep_ok = len(c1) == 11 and len(c2) == 7 and len(c3) == 8  # merge-log +2 RUL cols (11b)
		print(
			f"report merge-log cols={len(c1)} chain cols={len(c2)} recon cols={len(c3)} "
			f"(recon rows={len(r3)}) {'OK' if rep_ok else 'FAIL'}"
		)
		ok = ok and rep_ok

	finally:
		frappe.db.rollback(save_point="phase8_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 8:", "PASS" if ok else "FAIL")
