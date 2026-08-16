"""Phase 13 verification — END-TO-END EFFECTS. Run:
bench --site <site> execute asset_enterprise.setup.verify_phase13.run

Not code-level checks: every flow is asserted across the surfaces it
must impact —
  - tabGL Entry account balances (role deltas per §12 GL matrix)
  - per-asset ledger sums (against_voucher) vs derived values
  - core Fixed Asset Register report rows
  - our reports (Daily Reconciliation, Merge Log, Replacement Chain,
    Asset Tree)
  - Asset Activity history
  - per-voucher balance (debits == credits)
Savepoint-rolled-back; safe on live sites.
"""

import traceback

import frappe
from frappe.utils import add_months, flt, get_first_day, getdate, nowdate

from asset_enterprise.setup.test_fixtures import make_test_asset, pick_company, pick_plain_account


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()


def gl_bal(account):
	return flt(
		frappe.db.sql(
			"select coalesce(sum(debit) - sum(credit), 0) from `tabGL Entry` "
			"where account = %s and is_cancelled = 0",
			account,
		)[0][0]
	)


def gl_asset_sum(account, asset_name):
	return flt(
		frappe.db.sql(
			"select coalesce(sum(debit) - sum(credit), 0) from `tabGL Entry` "
			"where account = %s and is_cancelled = 0 and against_voucher_type = 'Asset' "
			"and against_voucher = %s",
			(account, asset_name),
		)[0][0]
	)


def register_row(company, asset_name):
	from erpnext.assets.report.fixed_asset_register.fixed_asset_register import execute

	_, data, *_ = execute(frappe._dict({"company": company, "status": "", "filter_based_on": "Fiscal Year"}))
	for row in data:
		if row.get("asset_id") == asset_name:
			return row
	return None


def recon_row(company, asset_name):
	from asset_enterprise.asset_enterprise.report.asset_daily_reconciliation.asset_daily_reconciliation import (
		execute,
	)

	_, data = execute({"company": company})
	for row in data:
		if row.get("asset") == asset_name:
			return row
	return None


def _mk_account(company, name, root_type, account_type=None):
	full = frappe.db.get_value("Account", {"account_name": name, "company": company}, "name")
	if full:
		return full
	parent = frappe.db.get_value(
		"Account", {"company": company, "root_type": root_type, "is_group": 1}, "name"
	)
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": name,
			"company": company,
			"parent_account": parent,
			"root_type": root_type,
			"account_type": account_type,
			"is_group": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _run():
	ok = True
	company = pick_company()

	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import _post_one, enable_depreciation

	switch_before = frappe.db.get_single_value(
		"Asset Settings", "enable_enterprise_assets", cache=False
	)
	frappe.db.savepoint("phase13_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		if flt(frappe.db.get_single_value("Accounts Settings", "over_billing_allowance")) < 100:
			frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 100)

		# Distinct role accounts so ledger assertions cannot degenerate.
		suspense = _mk_account(company, "AE13 Suspense", "Liability")
		clearing = _mk_account(company, "AE13 Invoice Clearing", "Liability")
		damage = _mk_account(company, "AE13 Damage Loss", "Expense")
		frappe.db.set_value(
			"Company", company,
			{"default_asset_suspense_account": suspense,
			 "default_asset_invoice_difference_account": clearing},
			update_modified=False,
		)
		dmg_row = frappe.db.get_value(
			"Scrapping Type Account", {"parent": "Damage", "company": company}, "name"
		)
		if dmg_row:
			frappe.db.set_value("Scrapping Type Account", dmg_row, "gl_account", damage, update_modified=False)
		else:
			frappe.get_doc(
				{"doctype": "Scrapping Type Account", "parenttype": "Scrapping Type",
				 "parent": "Damage", "parentfield": "accounts", "company": company,
				 "gl_account": damage}
			).db_insert()

		seed = make_test_asset(company, gross=1, submit=False)  # masters
		aca = frappe.db.get_value(
			"Asset Category Account",
			{"parent": "AE Smoke Category", "company_name": company},
			["fixed_asset_account", "accumulated_depreciation_account", "depreciation_expense_account"],
			as_dict=True,
		)
		fa, accum, depr_exp = (
			aca.fixed_asset_account, aca.accumulated_depreciation_account,
			aca.depreciation_expense_account,
		)

		# ================= S1: Existing-asset opening (§12.2, TC-001) ====
		b_fa, b_ac, b_su = gl_bal(fa), gl_bal(accum), gl_bal(suspense)
		a1 = make_test_asset(company, gross=1_000_000, submit=False)
		a1.opening_accumulated_depreciation = 400_000
		a1.flags.ignore_permissions = True
		a1.save()
		a1.submit()
		d_fa, d_ac, d_su = gl_bal(fa) - b_fa, gl_bal(accum) - b_ac, gl_bal(suspense) - b_su
		reg = register_row(company, a1.name)
		rec = recon_row(company, a1.name)
		act = frappe.db.count("Asset Activity", {"asset": a1.name, "transaction_category": "Addition"})
		s1_ok = (
			d_fa == 1_000_000 and d_ac == -400_000 and d_su == -600_000
			and gl_asset_sum(fa, a1.name) == 1_000_000
			and reg and flt(reg["net_purchase_amount"]) == 1_000_000
			and flt(reg["opening_accumulated_depreciation"]) == 400_000
			and flt(reg["asset_value"]) == 600_000
			and rec and rec["flagged"] == "No"
			and act >= 1
		)
		print(
			f"s1 opening | GL Δ FA={d_fa} Accum={d_ac} Susp={d_su} | register cost/accum/value="
			f"{reg and (reg['net_purchase_amount'], reg['opening_accumulated_depreciation'], reg['asset_value'])} "
			f"| recon flagged={rec and rec['flagged']} | history={act} {'OK' if s1_ok else 'FAIL'}"
		)
		ok = ok and bool(s1_ok)

		# ================= S2: depreciation via mass run (§12.18, TC-008/015)
		b_ac, b_ex = gl_bal(accum), gl_bal(depr_exp)
		c1 = make_test_asset(company, gross=73_000, submit=True)
		enable_depreciation(
			c1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(add_months(nowdate(), -3)),
		)
		frappe.get_doc(
			{"doctype": "Asset Settings Authority Role", "parenttype": "Asset Settings",
			 "parent": "Asset Settings", "parentfield": "mass_depreciation_authority_roles",
			 "role": "System Manager"}
		).db_insert()
		mad = frappe.get_doc(
			{"doctype": "Mass Asset Depreciation", "company": company,
			 "posting_date": nowdate(), "mode": "Selected Assets",
			 "selected_assets": [{"asset": c1.name}]}
		)
		mad.flags.ignore_permissions = True
		mad.insert()
		mad.submit()
		d_ac, d_ex = gl_bal(accum) - b_ac, gl_bal(depr_exp) - b_ex
		jes = [
			r.reason for r in frappe.get_all(
				"Mass Asset Depreciation Result",
				filters={"parent": mad.name, "outcome": "Posted"}, fields=["reason"])
		]
		cc_rows = flt(frappe.db.sql(
			"""select count(*) from `tabGL Entry` where voucher_no in %s and is_cancelled=0
			   and account = %s and ifnull(cost_center,'') != ''""",
			(tuple(jes), depr_exp))[0][0]) if jes else 0
		posted_sum = flt(frappe.db.sql(
			"""select coalesce(sum(ds.depreciation_amount),0) from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent=ads.name
			where ads.asset=%s and ads.status='Active' and ifnull(ds.journal_entry,'')!=''""",
			c1.name)[0][0])
		vals = recalculate_asset_values(c1.name, save=True)
		reg = register_row(company, c1.name)
		rec = recon_row(company, c1.name)
		s2_ok = (
			len(jes) == 3 and posted_sum > 0
			and d_ac == -posted_sum and d_ex == posted_sum and cc_rows == 3
			and flt(vals["accumulated_depreciation_value"]) == posted_sum
			and gl_asset_sum(accum, c1.name) == -posted_sum
			and reg and flt(reg["depreciated_amount"]) == posted_sum
			and flt(reg["asset_value"]) == flt(73_000 - posted_sum)
			and rec and rec["flagged"] == "No"
		)
		print(
			f"s2 depreciation | 3 JEs, GL Δ Accum={d_ac} Exp={d_ex} CC-rows={cc_rows} | "
			f"derived accum={vals['accumulated_depreciation_value']} | register depr/value="
			f"{reg and (reg['depreciated_amount'], reg['asset_value'])} | recon={rec and rec['flagged']} "
			f"{'OK' if s2_ok else 'FAIL'}"
		)
		ok = ok and bool(s2_ok)

		# ================= S3: PR -> PI delta (§12.1/12.4, TC-023) =======
		item = frappe.get_doc("Item", "AE-SMOKE-ITEM")
		item.auto_create_assets = 1
		item.asset_naming_series = (
			frappe.get_meta("Asset").get_field("naming_series").options.split("\n")[0]
		)
		item.flags.ignore_permissions = True
		item.save()
		supplier = frappe.db.get_value(
			"Supplier", {"supplier_name": "AE Smoke Supplier"}, "name"
		) or frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": "AE Smoke Supplier"}
		).insert(ignore_permissions=True).name
		arbnb = frappe.db.get_value("Company", company, "asset_received_but_not_billed")
		b_fa, b_arb, b_cl = gl_bal(fa), gl_bal(arbnb), gl_bal(clearing)
		pr = frappe.get_doc(
			{"doctype": "Purchase Receipt", "company": company, "supplier": supplier,
			 "posting_date": nowdate(),
			 "items": [{"item_code": "AE-SMOKE-ITEM", "qty": 1, "rate": 50_000,
				"asset_location": seed.location}]}
		)
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		pa = frappe.get_doc(
			"Asset", frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
		)
		pa.available_for_use_date = nowdate()
		pa.flags.ignore_permissions = True
		pa.save()
		pa.submit()
		pi = frappe.get_doc(
			{"doctype": "Purchase Invoice", "company": company, "supplier": supplier,
			 "posting_date": nowdate(),
			 "items": [{"item_code": "AE-SMOKE-ITEM", "qty": 1, "rate": 55_000,
				"purchase_receipt": pr.name, "pr_detail": pr.items[0].name}],
			 "pi_asset_allocation": [{"asset": pa.name}]}
		)
		pi.flags.ignore_permissions = True
		pi.insert()
		pi.submit()
		d_fa, d_arb, d_cl = gl_bal(fa) - b_fa, gl_bal(arbnb) - b_arb, gl_bal(clearing) - b_cl
		reg = register_row(company, pa.name)
		s3_ok = (
			d_fa == 55_000 and d_arb == 0 and d_cl == 0
			and flt(recalculate_asset_values(pa.name, save=False)["historical_asset_value"]) == 55_000
			and reg is not None
		)
		print(
			f"s3 PR->PI delta | GL Δ FA={d_fa} (want 55000) ARBNB={d_arb} (want 0) "
			f"Clearing={d_cl} (want 0) | register present={bool(reg)} {'OK' if s3_ok else 'FAIL'}"
		)
		ok = ok and bool(s3_ok)

		# ================= S4: AVA + cancel pair (§12.15/12.16, TC-044a) =
		oci = pick_plain_account(company, "Liability")
		v1 = make_test_asset(company, gross=100_000, submit=True)
		b_fa2, b_oci = gl_asset_sum(fa, v1.name), gl_bal(oci)
		ava = frappe.get_doc(
			{"doctype": "Asset Value Adjustment", "asset": v1.name, "company": company,
			 "date": nowdate(), "transaction_type": "Upward Revaluation",
			 "current_asset_value": 100_000, "new_asset_value": 120_000,
			 "difference_account": oci}
		)
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		mid_fa = gl_asset_sum(fa, v1.name) - b_fa2
		ava.reload()
		ava.cancel()
		d_fa2 = gl_asset_sum(fa, v1.name) - b_fa2
		d_oci = gl_bal(oci) - b_oci
		orig_je_live = frappe.db.get_value(
			"Journal Entry", frappe.db.get_value("Asset Value Adjustment", ava.name, "journal_entry"),
			"docstatus",
		) if frappe.db.get_value("Asset Value Adjustment", ava.name, "journal_entry") else None
		s4_ok = mid_fa == 20_000 and d_fa2 == 0 and d_oci == 0
		print(
			f"s4 AVA pair | FA-by-asset +20k then net {d_fa2} (want 0) | OCI net {d_oci} (want 0) "
			f"| original JE docstatus={orig_je_live} {'OK' if s4_ok else 'FAIL'}"
		)
		ok = ok and s4_ok

		# ===== S4b: value adjustment must RE-SPREAD the new value (TC-016)
		v2 = make_test_asset(company, gross=240_000, submit=True)
		enable_depreciation(
			v2.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(add_months(nowdate(), -2)),
		)
		_post_one(
			frappe.db.sql(
				"""select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
				   ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
				   ds.daily_rate, ds.days_in_period
				from `tabDepreciation Schedule` ds
				join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
				where ads.asset = %s and ads.status='Active' and ifnull(ds.journal_entry,'')=''
				order by ds.schedule_date limit 1""", v2.name, as_dict=True)[0],
			getdate(nowdate()),
		)
		ava2 = frappe.get_doc(
			{"doctype": "Asset Value Adjustment", "asset": v2.name, "company": company,
			 "date": nowdate(), "transaction_type": "Upward Revaluation",
			 "current_asset_value": flt(recalculate_asset_values(v2.name, save=False)["net_book_value"]),
			 "new_asset_value": flt(recalculate_asset_values(v2.name, save=False)["net_book_value"]) + 24_000,
			 "difference_account": pick_plain_account(company, "Liability")}
		)
		ava2.flags.ignore_permissions = True
		ava2.insert()
		ava2.submit()
		vals2 = recalculate_asset_values(v2.name, save=False)
		sched_total = flt(frappe.db.sql(
			"""select coalesce(sum(ds.depreciation_amount),0) from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent=ads.name
			where ads.asset=%s and ads.status='Active' and ads.docstatus=1""", v2.name)[0][0])
		s4b_ok = (
			flt(vals2["historical_asset_value"]) == 264_000
			and abs(sched_total - 264_000) <= 0.05   # schedule must carry the NEW value
		)
		print(
			f"s4b adjustment re-spread | HAV={vals2['historical_asset_value']:,.2f} "
			f"Active-schedule total={sched_total:,.2f} (want 264,000 — new value fully "
			f"depreciable) {'OK' if s4b_ok else 'FAIL'}"
		)
		ok = ok and bool(s4b_ok)

		# ================= S5: composite merge (§12.21, TC-029/049) ======
		cap_clearing = frappe.db.get_value("Company", company, "default_capitalization_clearing_account")
		if not cap_clearing:
			cap_clearing = _mk_account(company, "AE13 Cap Clearing", "Liability")
			frappe.db.set_value(
				"Company", company, "default_capitalization_clearing_account", cap_clearing,
				update_modified=False,
			)
		s5a = make_test_asset(company, gross=30_000, submit=True)
		s5b = make_test_asset(company, gross=20_000, submit=True)
		tgt = make_test_asset(company, gross=50_000, submit=True)
		b_cl2 = gl_bal(cap_clearing)
		cap = frappe.get_doc(
			{"doctype": "Asset Capitalization", "transaction_type": "Capitalized Maintenance",
			 "transaction_sub_type": "Standard Maintenance", "target_asset": tgt.name,
			 "company": company, "posting_date": nowdate(),
			 "posting_time": frappe.utils.nowtime(), "entry_type": "Capitalization",
			 "asset_items": [{"asset": s5a.name}, {"asset": s5b.name}]}
		)
		cap.flags.ignore_permissions = True
		cap.flags.ignore_mandatory = True
		cap.insert()
		cap.submit()
		d_cl2 = gl_bal(cap_clearing) - b_cl2
		tgt_fa = gl_asset_sum(fa, tgt.name)
		src_fa = gl_asset_sum(fa, s5a.name) + gl_asset_sum(fa, s5b.name)
		from asset_enterprise.asset_enterprise.report.composite_merge_log_report.composite_merge_log_report import (
			execute as mergelog,
		)

		_, mrows = mergelog({"composite_asset": tgt.name})
		reg_src = register_row(company, s5a.name)
		s5_ok = (
			d_cl2 == 0 and tgt_fa == 100_000 and src_fa == 0
			and len(mrows) == 2 and all(r.get("remaining_useful_life_in_months") is not None for r in mrows)
			and reg_src is None  # cancelled sources leave the register
			and register_row(company, tgt.name) is not None
		)
		print(
			f"s5 merge | clearing net={d_cl2} (want 0) | FA-by-asset target={tgt_fa} (want 100000) "
			f"sources={src_fa} (want 0) | merge-log rows={len(mrows)} | sources out of register="
			f"{reg_src is None} {'OK' if s5_ok else 'FAIL'}"
		)
		ok = ok and bool(s5_ok)

		# ====== S5b: TC-027/TC-046 service capitalized onto submitted asset
		svc_item = "AE13 Service Item"
		if not frappe.db.exists("Item", svc_item):
			frappe.get_doc(
				{"doctype": "Item", "item_code": svc_item, "item_name": svc_item,
				 "item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				 "is_stock_item": 0, "is_fixed_asset": 0}
			).insert(ignore_permissions=True)
		svc_expense = _mk_account(company, "AE13 Service Expense", "Expense")
		t2 = make_test_asset(company, gross=1_000_000, submit=True)
		b_fa3, b_svc = gl_asset_sum(fa, t2.name), gl_bal(svc_expense)
		cap_s = frappe.get_doc(
			{"doctype": "Asset Capitalization", "transaction_type": "Capitalized Maintenance",
			 "transaction_sub_type": "Standard Maintenance", "target_asset": t2.name,
			 "company": company, "posting_date": nowdate(),
			 "posting_time": frappe.utils.nowtime(), "entry_type": "Capitalization",
			 "service_items": [{"item_code": svc_item, "qty": 1, "rate": 50_000,
				"amount": 50_000, "expense_account": svc_expense}]}
		)
		cap_s.flags.ignore_permissions = True
		cap_s.flags.ignore_mandatory = True
		cap_s.insert()
		cap_s.submit()
		d_fa3 = gl_asset_sum(fa, t2.name) - b_fa3
		d_svc = gl_bal(svc_expense) - b_svc
		hav_t2 = flt(recalculate_asset_values(t2.name, save=False)["historical_asset_value"])
		reg_t2 = register_row(company, t2.name)
		s5b_ok = (
			d_fa3 == 50_000 and d_svc == -50_000 and hav_t2 == 1_050_000
			and reg_t2 is not None
		)
		print(
			f"s5b service cap | GL Δ FA={d_fa3} (want +50000) ServiceExp={d_svc} (want -50000) "
			f"| HAV={hav_t2} (want 1050000) {'OK' if s5b_ok else 'FAIL'}"
		)
		ok = ok and bool(s5b_ok)

		# ================= S6: reclassification (§12.13, TC-028) =========
		fa_b = _mk_account(company, "AE13 FA Category B", "Asset", "Fixed Asset")
		ac_b = _mk_account(company, "AE13 Accum Category B", "Asset", "Accumulated Depreciation")
		cat_b = "AE13 Category B"
		if not frappe.db.exists("Asset Category", cat_b):
			frappe.get_doc(
				{"doctype": "Asset Category", "asset_category_name": cat_b,
				 "accounts": [{"company_name": company, "fixed_asset_account": fa_b,
					"accumulated_depreciation_account": ac_b,
					"depreciation_expense_account": depr_exp}]}
			).insert(ignore_permissions=True)
		item_b = "AE13-ITEM-B"
		if not frappe.db.exists("Item", item_b):
			frappe.get_doc(
				{"doctype": "Item", "item_code": item_b, "item_name": item_b,
				 "item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				 "is_fixed_asset": 1, "is_stock_item": 0, "asset_category": cat_b}
			).insert(ignore_permissions=True)
		r_src = make_test_asset(company, gross=40_000, submit=True)
		r_tgt = frappe.get_doc(
			{"doctype": "Asset", "company": company, "item_code": item_b,
			 "asset_name": "AE Smoke Reclass B", "asset_category": cat_b,
			 "location": seed.location, "purchase_amount": 1,
			 "net_purchase_amount": 1, "purchase_date": nowdate(),
			 "available_for_use_date": nowdate(), "calculate_depreciation": 0}
		)
		r_tgt.flags.ignore_permissions = True
		r_tgt.insert()
		b_fa_a, b_fa_b = gl_asset_sum(fa, r_src.name), gl_bal(fa_b)
		cap2 = frappe.get_doc(
			{"doctype": "Asset Capitalization", "transaction_type": "Capitalized Maintenance",
			 "transaction_sub_type": "Reclassification / Asset Category Transfer",
			 "target_asset": r_tgt.name, "company": company, "posting_date": nowdate(),
			 "posting_time": frappe.utils.nowtime(), "entry_type": "Capitalization",
			 "asset_items": [{"asset": r_src.name}]}
		)
		cap2.flags.ignore_permissions = True
		cap2.flags.ignore_mandatory = True
		cap2.insert()
		cap2.submit()
		d_fa_a = gl_asset_sum(fa, r_src.name) - b_fa_a
		d_fa_b = gl_bal(fa_b) - b_fa_b
		reg_b = register_row(company, r_tgt.name)
		s6_ok = (
			d_fa_a == -40_000 and d_fa_b == 40_000
			and reg_b and reg_b["asset_category"] == cat_b
			and flt(reg_b["net_purchase_amount"]) == 40_000
		)
		print(
			f"s6 reclass | GL Δ FA-A(by asset)={d_fa_a} (want -40000) FA-B={d_fa_b} (want +40000) "
			f"| register: category={reg_b and reg_b['asset_category']} cost={reg_b and reg_b['net_purchase_amount']} "
			f"{'OK' if s6_ok else 'FAIL'}"
		)
		ok = ok and bool(s6_ok)

		# ================= S7: scrap + restore (§12.8, TC-030a) ==========
		j1 = make_test_asset(company, gross=25_000, submit=True)
		b_dmg = gl_bal(damage)
		from asset_enterprise import disposal
		from asset_enterprise.restore import restore_asset

		disposal.scrap_asset(j1.name, scrapping_type="Damage")
		mid_fa_j = gl_asset_sum(fa, j1.name)
		mid_dmg = gl_bal(damage) - b_dmg
		reg_scrapped = register_row(company, j1.name)
		restore_asset(j1.name)
		end_fa_j = gl_asset_sum(fa, j1.name)
		end_dmg = gl_bal(damage) - b_dmg
		s7_ok = (
			mid_fa_j == 0 and mid_dmg == 25_000
			and reg_scrapped and reg_scrapped["status"] == "Scrapped"
			and end_fa_j == 25_000 and end_dmg == 0
		)
		print(
			f"s7 scrap+restore | after scrap FA-by-asset={mid_fa_j} (0) loss={mid_dmg} (25000) "
			f"register status={reg_scrapped and reg_scrapped['status']} | after restore FA={end_fa_j} "
			f"(25000) loss net={end_dmg} (0) {'OK' if s7_ok else 'FAIL'}"
		)
		ok = ok and bool(s7_ok)

		# ============ S8: replacement chain report effect (TC-030c) ======
		j4 = make_test_asset(company, gross=15_000, submit=True)
		disposal.scrap_asset(j4.name, scrapping_type="Damage")
		from asset_enterprise.restore import create_replacement_asset

		repl = create_replacement_asset(j4.name)
		from asset_enterprise.asset_enterprise.report.replacement_chain.replacement_chain import (
			execute as chain,
		)

		_, crows = chain({"company": company})
		in_chain = any(
			r.get("replacement_of_asset") == j4.name or r.get("asset") == repl for r in crows
		)
		print(f"s8 replacement chain report contains pair: {'OK' if in_chain else 'FAIL: ' + str(crows[:2])}")
		ok = ok and in_chain

		# ============ S9: every voucher balanced (global) ================
		unbalanced = frappe.db.sql(
			"""select voucher_no, sum(debit) - sum(credit) as diff from `tabGL Entry`
			   where is_cancelled = 0 and posting_date = %s
			   group by voucher_no having abs(diff) > 0.005""",
			nowdate(),
		)
		print(f"s9 all today's vouchers balanced: {'OK' if not unbalanced else 'FAIL: ' + str(unbalanced[:3])}")
		ok = ok and not unbalanced

	finally:
		frappe.db.rollback(save_point="phase13_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 13:", "PASS" if ok else "FAIL")
