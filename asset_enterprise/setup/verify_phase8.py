"""Phase 8 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase8.run"""

import traceback

import frappe
from frappe.utils import add_months, cint, flt, getdate, nowdate


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

		# GAP-021: the transfer month splits BY COST CENTRE — days before
		# the transfer stay on the PRIOR cc, days from the transfer on the
		# new one. Counting tagged rows alone let a mistag through: both
		# halves carried the new CC (client, 20/08, ACC-ASM-2026-01092 —
		# the pre-transfer portion posted to the wrong cost centre).
		halves = frappe.db.sql(
			"""
			select ds.schedule_date, ds.cost_center, ds.days_in_period
			from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			  and ds.cost_center is not null
			order by ds.schedule_date, ds.idx
			""",
			a1.name, as_dict=True,
		)
		new_half = [h for h in halves if h.cost_center == cc2]
		old_half = [h for h in halves if h.cost_center == prior_cc]
		day = getdate(nowdate()).day
		mid_month = day > 1
		g21_ok = bool(new_half) and (not mid_month or (
			bool(old_half) and cint(old_half[0].days_in_period) == day - 1
		))
		print(
			f"gap021 transfer-month CC split: old-cc rows "
			f"{[(str(h.schedule_date), h.days_in_period) for h in old_half]} / new-cc rows "
			f"{[(str(h.schedule_date), h.days_in_period) for h in new_half]} "
			f"(want pre-transfer days on {prior_cc}) {'OK' if g21_ok else 'FAIL'}"
		)
		ok = ok and g21_ok

		# GAP-028: cancel restores prior CC.
		mv.reload()
		mv.cancel()
		restored = frappe.db.get_value("Asset", a1.name, "cost_center")
		print(f"gap028 CC restored on cancel: {restored} == {prior_cc} {'OK' if restored == prior_cc else 'FAIL'}")
		ok = ok and restored == prior_cc

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
