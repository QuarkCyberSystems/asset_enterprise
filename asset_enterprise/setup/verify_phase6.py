"""Phase 6 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase6.run"""

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
	company = frappe.db.get_value("Company", {}, "name")

	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.disposal import partial_scrap_asset, scrap_asset
	from asset_enterprise.restore import create_replacement_asset, restore_asset
	from asset_enterprise.setup.test_fixtures import make_test_asset

	frappe.db.savepoint("phase6_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)

		# Scrapping Type "Damage" gets a per-company account (chain tier 1).
		damage_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
		)
		st = frappe.get_doc("Scrapping Type", "Damage")
		st.append("accounts", {"company": company, "gl_account": damage_account})
		st.save(ignore_permissions=True)
		print(f"setup  Damage -> {damage_account}")

		# ------------------------------------------- full scrap (GAP-019)
		a1 = make_test_asset(company, gross=80_000, submit=False, with_depreciation=True)
		a1.submit()
		je1 = scrap_asset(a1.name, scrapping_type="Damage")

		status = frappe.db.get_value("Asset", a1.name, "status")
		lines = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": je1, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		loss_line = [l for l in lines if l.account == damage_account]
		fa_credit = sum(l.credit for l in lines) - sum(
			l.credit for l in lines if l.account == damage_account
		)
		values = recalculate_asset_values(a1.name, save=False)
		s_ok = (
			status == "Scrapped"
			and loss_line
			and flt(values["historical_asset_value"]) == 0
			and flt(values["net_book_value"]) == 0
		)
		print(
			f"scrap  status={status} loss->Damage={bool(loss_line)} "
			f"({loss_line and flt(loss_line[0].debit)}) FA credit={fa_credit} "
			f"post-values HAV/NBV={values['historical_asset_value']}/{values['net_book_value']} "
			f"{'OK' if s_ok else 'FAIL'}"
		)
		ok = ok and bool(s_ok)

		# VR-041: second disposal blocked.
		try:
			scrap_asset(a1.name, scrapping_type="Damage")
			print("vr041  FAIL (double disposal allowed)")
			ok = False
		except frappe.ValidationError:
			print("vr041  double-disposal blocked: OK")

		# ------------------------------- same-period restore (Path 1)
		mirror = restore_asset(a1.name)
		status2 = frappe.db.get_value("Asset", a1.name, "status")
		srje = frappe.db.get_value("Asset", a1.name, "scrap_reversal_journal_entry")
		orig_docstatus = frappe.db.get_value("Journal Entry", je1, "docstatus")
		values2 = recalculate_asset_values(a1.name, save=False)
		r_ok = (
			status2 != "Scrapped"
			and srje == mirror
			and orig_docstatus == 1
			and flt(values2["historical_asset_value"]) == 80_000
		)
		print(
			f"restore status={status2} mirror={mirror == srje} origJE=1:{orig_docstatus == 1} "
			f"HAV back={values2['historical_asset_value']} {'OK' if r_ok else 'FAIL'}"
		)
		ok = ok and bool(r_ok)

		# ------------------------------- outside-window restore blocked
		a2 = make_test_asset(company, gross=30_000, submit=False, with_depreciation=True)
		a2.asset_name = "AE Smoke Asset W"
		a2.flags.ignore_permissions = True
		a2.save()
		a2.submit()
		scrap_asset(a2.name, scrapping_type="Damage")
		frappe.db.set_value("Asset", a2.name, "disposal_date", add_months(nowdate(), -2))
		try:
			restore_asset(a2.name)
			print("window FAIL (cross-period restore allowed)")
			ok = False
		except frappe.ValidationError as e:
			w_ok = "Create Replacement Asset" in str(e)
			print(f"window cross-period blocked with GAP-016 pointer: {'OK' if w_ok else 'FAIL'}")
			ok = ok and w_ok

		# ------------------------------- Create Replacement Asset (Path 2)
		rep_name = create_replacement_asset(a2.name)
		rep = frappe.get_doc("Asset", rep_name)
		rep.purchase_amount = 25_000
		rep.net_purchase_amount = 25_000
		rep.flags.ignore_permissions = True
		rep.save()
		rep.submit()
		back = frappe.db.get_value("Asset", a2.name, "replaced_by_asset")
		fwd = frappe.db.get_value("Asset", rep_name, "replacement_of_asset")
		p2_ok = back == rep_name and fwd == a2.name
		print(f"path2  replacement={rep_name} two-way link {'OK' if p2_ok else 'FAIL'}")
		ok = ok and p2_ok

		# ------------------------------------------- partial scrap (GAP-018)
		a3 = make_test_asset(company, gross=200_000, submit=False, with_depreciation=True)
		a3.asset_name = "AE Smoke Asset P"
		a3.flags.ignore_permissions = True
		a3.save()
		a3.submit()
		before = recalculate_asset_values(a3.name, save=False)
		je3 = partial_scrap_asset(a3.name, percentage=5, scrapping_type="Damage")

		after = recalculate_asset_values(a3.name, save=False)
		scrap_value = fa_credit_p = flt(
			frappe.db.sql(
				"""select sum(credit) from `tabGL Entry`
				   where voucher_no=%s and is_cancelled=0 and credit > 0""",
				je3,
			)[0][0]
		)
		p_ok = (
			flt(scrap_value) == 10_000
			and flt(after["historical_asset_value"]) == flt(before["historical_asset_value"]) - 10_000
			and frappe.db.get_value("Asset", a3.name, "status") not in ("Scrapped",)
		)
		print(
			f"partial 5% of 200k: CR FA={scrap_value} (want 10000) "
			f"HAV {before['historical_asset_value']} -> {after['historical_asset_value']} "
			f"asset stays active {'OK' if p_ok else 'FAIL'}"
		)
		ok = ok and bool(p_ok)

	finally:
		frappe.db.rollback(save_point="phase6_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and not switch else 'FAIL'}")
		ok = ok and left == 0 and not switch

	print("PHASE 6:", "PASS" if ok else "FAIL")
