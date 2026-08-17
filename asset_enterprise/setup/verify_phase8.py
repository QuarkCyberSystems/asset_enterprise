"""Phase 8 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase8.run"""

import traceback

import frappe
from frappe.utils import add_months, flt, nowdate


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
		cc2 = frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0, "name": ["!=", a1.cost_center or ""]}, "name"
		)
		prior_cc = frappe.db.get_value("Asset", a1.name, "cost_center")

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

		# GAP-021: CC recorded on a future schedule row (split or tag).
		cc_rows = frappe.db.sql(
			"""
			select count(*) from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status = 'Active' and ds.cost_center = %s
			""",
			(a1.name, cc2),
		)[0][0]
		print(f"gap021 schedule rows tagged/split to new CC: {cc_rows} {'OK' if cc_rows else 'FAIL'}")
		ok = ok and cc_rows > 0

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
