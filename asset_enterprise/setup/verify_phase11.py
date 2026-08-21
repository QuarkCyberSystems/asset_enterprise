"""Phase 11a verification (audit gap closure) — run:
bench --site <site> execute asset_enterprise.setup.verify_phase11.run

F0 accum single-count · F1 salvage in reschedule base · F2 UL horizon
+ RUL exhaustion · F3 manual-depreciation cancel gate · F4 opening-JE
reversal on asset cancel · F5 reclassification model · F6 straddling
depreciation reversal at merge · F7 SI disposal gate.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, cint, flt, get_first_day, getdate, nowdate


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
	from asset_enterprise.depreciation import (
		_post_one,
		active_schedule_horizon,
		enable_depreciation,
	)
	from asset_enterprise.setup.test_fixtures import make_test_asset

	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	frappe.db.savepoint("phase11_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		if not frappe.db.get_value("Company", company, "disposal_account"):
			expense = pick_plain_account(company, "Expense")
			frappe.db.set_value("Company", company, "disposal_account", expense, update_modified=False)

		def first_unposted_row(asset_name):
			r = frappe.db.sql(
				"""select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
				   ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
				   ds.daily_rate, ds.days_in_period
				from `tabDepreciation Schedule` ds
				join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
				where ads.asset = %s and ads.status='Active' and ads.docstatus=1
				  and ifnull(ds.journal_entry,'')='' order by ds.schedule_date limit 1""",
				asset_name, as_dict=True,
			)
			return r[0] if r else None

		# ------------------------------------------- F0: accum single-count
		a0 = make_test_asset(company, gross=24_000, submit=True)
		enable_depreciation(
			a0.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(add_months(nowdate(), -1)),
		)
		row = first_unposted_row(a0.name)
		_post_one(row, getdate(nowdate()))
		amt = flt(row.depreciation_amount)
		accum = flt(recalculate_asset_values(a0.name, save=False)["accumulated_depreciation_value"])
		f0_ok = abs(accum - amt) < 0.02
		print(f"f0     posted {amt}; derived accum {accum} (want single-count) {'OK' if f0_ok else 'FAIL'}")
		ok = ok and f0_ok

		# ------------------------------- F1: salvage in the reschedule base
		s1 = make_test_asset(company, gross=50_000, submit=True)
		enable_depreciation(
			s1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=nowdate(), expected_value_after_useful_life=5_000,
		)
		from asset_enterprise.depreciation import supersede_and_regenerate

		supersede_and_regenerate(s1.name, as_of_date=nowdate(), reason="f1 probe")
		regen_sum = flt(frappe.db.sql(
			"""select coalesce(sum(ds.depreciation_amount),0)
			from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent=ads.name
			where ads.asset=%s and ads.status='Active' and ads.docstatus=1""",
			s1.name)[0][0])
		f1_ok = regen_sum == 45_000  # NBV 50k − salvage 5k
		print(f"f1     regenerated base = {regen_sum} (want 45000, salvage kept) {'OK' if f1_ok else 'FAIL'}")
		ok = ok and f1_ok

		# ---------------------- F2: UL adjustment moves the horizon; RUL<=0
		u1 = make_test_asset(company, gross=36_000, submit=True)
		enable_depreciation(
			u1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=nowdate(),
		)
		h_before = getdate(active_schedule_horizon(u1.name))
		ava = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": u1.name, "company": company,
			"date": nowdate(), "transaction_type": "Useful Life Adjustment",
			"current_asset_value": 36_000, "new_asset_value": 36_000,
			"adjusted_life_months": 12,
			"difference_account": frappe.db.get_value(
				"Account", {"company": company, "root_type": "Liability", "is_group": 0}, "name"),
		})
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		h_after = getdate(active_schedule_horizon(u1.name))
		fb_total = frappe.db.get_value(
			"Asset Finance Book", {"parent": u1.name}, "total_number_of_depreciations")
		f2_ok = h_after == getdate(add_months(h_before, 12)) and int(fb_total) == 36
		print(
			f"f2     horizon {h_before} -> {h_after} (want +12mo) fb periods={fb_total} (want 36) "
			f"{'OK' if f2_ok else 'FAIL'}"
		)
		ok = ok and f2_ok

		# RUL exhaustion: shorten far below today.
		u2 = make_test_asset(company, gross=12_000, submit=True)
		enable_depreciation(
			u2.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=nowdate(),
		)
		ava2 = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": u2.name, "company": company,
			"date": nowdate(), "transaction_type": "Useful Life Adjustment",
			"current_asset_value": 12_000, "new_asset_value": 12_000,
			"adjusted_life_months": -36,
			"difference_account": frappe.db.get_value(
				"Account", {"company": company, "root_type": "Liability", "is_group": 0}, "name"),
		})
		ava2.flags.ignore_permissions = True
		ava2.insert()
		ava2.submit()
		v2 = recalculate_asset_values(u2.name, save=False)
		imm_ft = frappe.db.exists(
			"Financial Treatment",
			{"asset": u2.name, "transaction_category": "Depreciation",
			 "transaction_type": ("like", "Immediate Depreciation%"), "status": "Posted"},
		)
		ava2.reload()
		f2b_ok = (
			flt(v2["net_book_value"]) == 0
			and bool(imm_ft)
			# the AVA links the depreciation JE it caused (client, 19/08)
			and bool(ava2.get("exhaustion_journal_entry"))
		)
		print(f"f2b    RUL exhausted -> NBV {v2['net_book_value']} (want 0) immediate-FT={bool(imm_ft)} "
		      f"JE-link={ava2.get('exhaustion_journal_entry')} {'OK' if f2b_ok else 'FAIL'}")
		ok = ok and f2b_ok

		# F2c (client, 18/08): life adjusts by DAYS as well as months —
		# +2 months +15 days moves the horizon by exactly that.
		u3 = make_test_asset(company, gross=24_000, submit=True)
		enable_depreciation(
			u3.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=nowdate(),
		)
		h3_before = getdate(active_schedule_horizon(u3.name))
		ava3 = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": u3.name, "company": company,
			"date": nowdate(), "transaction_type": "Useful Life Adjustment",
			"current_asset_value": 24_000, "new_asset_value": 24_000,
			"adjusted_life_months": 2, "adjusted_life_days": 15,
		})
		ava3.flags.ignore_permissions = True
		ava3.insert()
		ava3.submit()
		h3_after = getdate(active_schedule_horizon(u3.name))
		h3_want = getdate(add_days(add_months(h3_before, 2), 15))
		f2c_ok = h3_after == h3_want
		print(
			f"f2c    +2 months +15 days: horizon {h3_before} -> {h3_after} "
			f"(want {h3_want}) {'OK' if f2c_ok else 'FAIL'}"
		)
		ok = ok and f2c_ok

		# --------------------- F3: manual depreciation blocks asset cancel
		m1 = make_test_asset(company, gross=10_000, submit=True)  # calc off
		aca = frappe.db.get_value(
			"Asset Category Account",
			{"parent": "AE Smoke Category", "company_name": company},
			["depreciation_expense_account", "accumulated_depreciation_account"], as_dict=True)
		je = frappe.get_doc({
			"doctype": "Journal Entry", "voucher_type": "Depreciation Entry",
			"company": company, "posting_date": nowdate(),
			"accounts": [
				{"account": aca.depreciation_expense_account, "debit_in_account_currency": 500,
				 "reference_type": "Asset", "reference_name": m1.name,
				 "cost_center": frappe.db.get_value("Company", company, "cost_center")},
				{"account": aca.accumulated_depreciation_account, "credit_in_account_currency": 500,
				 "reference_type": "Asset", "reference_name": m1.name},
			],
		})
		je.flags.ignore_permissions = True
		je.submit()
		try:
			m1.reload()
			m1.cancel()
			print("f3     FAIL (manually-depreciated asset cancelled)")
			ok = False
		except frappe.ValidationError as e:
			g = "posted depreciation" in str(e)
			print(f"f3     manual depreciation blocks cancel: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# ------------------- F4: opening JE reversed on clean asset cancel
		c1 = make_test_asset(company, gross=8_000, submit=True)  # opening JE fires
		ft = frappe.db.get_value(
			"Financial Treatment",
			{"asset": c1.name, "transaction_type": "Existing-Asset Opening", "status": "Posted"},
			"name")
		c1.reload()
		c1.cancel()
		ft_status = frappe.db.get_value("Financial Treatment", ft, "status")
		mirror = frappe.db.get_value(
			"Financial Treatment",
			{"asset": c1.name, "reversal_reference": ft}, "journal_entry")
		f4_ok = ft_status == "Reversed" and bool(mirror) and frappe.db.get_value(
			"Journal Entry", mirror, "docstatus") == 1
		print(f"f4     opening FT {ft_status} (want Reversed), mirror JE {mirror} {'OK' if f4_ok else 'FAIL'}")
		ok = ok and f4_ok

		# --------------------------------- F5: reclassification model
		cat2 = "AE Smoke Category 2"
		if not frappe.db.exists("Asset Category", cat2):
			src_aca = frappe.db.get_value(
				"Asset Category Account", {"parent": "AE Smoke Category", "company_name": company},
				["fixed_asset_account", "accumulated_depreciation_account", "depreciation_expense_account"],
				as_dict=True)
			frappe.get_doc({
				"doctype": "Asset Category", "asset_category_name": cat2,
				"accounts": [{
					"company_name": company,
					"fixed_asset_account": src_aca.fixed_asset_account,
					"accumulated_depreciation_account": src_aca.accumulated_depreciation_account,
					"depreciation_expense_account": src_aca.depreciation_expense_account,
				}],
			}).insert(ignore_permissions=True)

		item2 = "AE-SMOKE-ITEM-2"
		if not frappe.db.exists("Item", item2):
			frappe.get_doc({
				"doctype": "Item", "item_code": item2,
				"item_name": "AE Smoke Fixed Asset Item 2",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"is_fixed_asset": 1, "is_stock_item": 0, "asset_category": cat2,
			}).insert(ignore_permissions=True)

		r_src = make_test_asset(company, gross=20_000, submit=True)
		r_tgt = frappe.get_doc({
			"doctype": "Asset", "company": company, "item_code": item2,
			"asset_name": "AE Smoke Reclass Target", "asset_category": cat2,
			"location": r_src.location,
			"purchase_amount": 1, "net_purchase_amount": 1,
			"purchase_date": nowdate(), "available_for_use_date": nowdate(),
			"calculate_depreciation": 0,
		})
		r_tgt.flags.ignore_permissions = True
		r_tgt.insert()  # DRAFT target in the new category

		cap = frappe.get_doc({
			"doctype": "Asset Capitalization",
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Reclassification / Asset Category Transfer",
			"target_asset": r_tgt.name, "company": company,
			"posting_date": nowdate(), "posting_time": frappe.utils.nowtime(),
			"entry_type": "Capitalization",
			"asset_items": [{"asset": r_src.name}],
		})
		cap.flags.ignore_permissions = True
		cap.flags.ignore_mandatory = True
		cap.insert()
		cap.submit()

		src_after = frappe.db.get_value("Asset", r_src.name, ["docstatus"], as_dict=True)
		tgt_after = frappe.db.get_value(
			"Asset", r_tgt.name,
			["docstatus", "net_purchase_amount", "reclassified_from"], as_dict=True)
		tgt_vals = recalculate_asset_values(r_tgt.name, save=False)
		reclass_je = frappe.db.get_value(
			"Journal Entry", {"user_remark": ("like", f"%Reclassification of {r_src.name}%"),
			 "docstatus": 1}, "name")
		# no clearing account leg; no merge log rows; no suspense JE on target
		clearing_hits = frappe.db.sql(
			"""select count(*) from `tabJournal Entry Account`
			   where parent = %s and account like '%%Clearing%%'""", reclass_je)[0][0] if reclass_je else 99
		mlog = frappe.db.count("Composite Merge Log Entry", {"parent": r_tgt.name})
		suspense_ft = frappe.db.exists(
			"Financial Treatment",
			{"asset": r_tgt.name, "transaction_type": "Existing-Asset Opening"})
		f5_ok = (
			src_after.docstatus == 2
			and tgt_after.docstatus == 1
			and flt(tgt_after.net_purchase_amount) == 20_000
			and tgt_after.reclassified_from == r_src.name
			and flt(tgt_vals["historical_asset_value"]) == 20_000
			and reclass_je and clearing_hits == 0 and mlog == 0 and not suspense_ft
		)
		print(
			f"f5     reclass: src cancelled={src_after.docstatus == 2} tgt submitted gross={tgt_after.net_purchase_amount} "
			f"HAV={tgt_vals['historical_asset_value']} JE={reclass_je} clearing-legs={clearing_hits} "
			f"merge-log={mlog} suspense-suppressed={not suspense_ft} {'OK' if f5_ok else 'FAIL'}"
		)
		ok = ok and bool(f5_ok)

		# ------------------ F6: straddling depreciation reversed at merge
		g1 = make_test_asset(company, gross=36_500, submit=True)
		enable_depreciation(
			g1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(nowdate()),
		)
		row6 = first_unposted_row(g1.name)  # EOM of current month
		_post_one(row6, getdate(nowdate()))  # posted "full month" row
		accum_before = flt(recalculate_asset_values(g1.name, save=False)["accumulated_depreciation_value"])

		if not frappe.db.get_value("Company", company, "default_capitalization_clearing_account"):
			clearing = frappe.db.get_value(
				"Account", {"company": company, "root_type": "Liability", "is_group": 0}, "name"
			)
			frappe.db.set_value(
				"Company", company, "default_capitalization_clearing_account", clearing,
				update_modified=False,
			)
		gt = make_test_asset(company, gross=60_000, submit=True)
		merge_date = add_days(get_first_day(nowdate()), 6)  # mid-period, before the posted EOM row
		cap6 = frappe.get_doc({
			"doctype": "Asset Capitalization",
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Standard Maintenance",
			"target_asset": gt.name, "company": company,
			"posting_date": merge_date, "posting_time": frappe.utils.nowtime(),
			"entry_type": "Capitalization",
			"asset_items": [{"asset": g1.name}],
		})
		cap6.flags.ignore_permissions = True
		cap6.flags.ignore_mandatory = True
		cap6.insert()
		cap6.submit()
		reversed_row = frappe.db.sql(
			"""select count(*) from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent=ads.name
			where ads.asset=%s and ifnull(ds.reversal_journal_entry,'') != ''""",
			g1.name)[0][0]
		f6_ok = reversed_row >= 1 and accum_before > 0
		print(
			f"f6     straddling row reversed at merge: flagged-rows={reversed_row} "
			f"(accum before merge {accum_before}) {'OK' if f6_ok else 'FAIL'}"
		)
		ok = ok and f6_ok

		# ----------------------------------------- F7: SI disposal gate
		frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 1)
		from asset_enterprise.invoice_diff import si_validate

		fake_asset = make_test_asset(company, gross=5_000, submit=True)
		frappe.db.set_value("Asset", fake_asset.name, "purchase_receipt", "PR-FAKE-001",
			update_modified=False)
		class _FakeSI:
			items = [frappe._dict(asset=fake_asset.name)]

		fake_si = _FakeSI()
		try:
			si_validate(fake_si)
			print("f7     FAIL (uninvoiced asset sale passed)")
			ok = False
		except frappe.ValidationError as e:
			g = "VR-011" in str(e)
			print(f"f7     SI disposal gated: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g
		frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)

		# ================= Phase 11b guard/enrichment checks =================

		# T3/T4 (VR-010 / VR-025): movement guards.
		p1 = make_test_asset(company, gross=9_000, submit=True)
		child = make_test_asset(company, gross=4_000, submit=True)
		frappe.db.set_value("Asset", child.name, "parent_asset", p1.name, update_modified=False)
		loc2 = frappe.db.get_value("Location", {"name": ["!=", p1.location]}, "name") or p1.location
		mv = frappe.get_doc({
			"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
			"transaction_date": frappe.utils.now(),
			"assets": [{"asset": p1.name, "target_location": loc2}],
		})
		mv.flags.ignore_permissions = True
		try:
			mv.insert()
			print("t3     FAIL (parent with children moved)")
			ok = False
		except frappe.ValidationError as e:
			g = "VR-010" in str(e)
			print(f"t3     parent-with-children transfer blocked: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		group_cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
		if group_cc:
			mv2 = frappe.get_doc({
				"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
				"transaction_date": frappe.utils.now(),
				"assets": [{"asset": child.name, "target_cost_center": group_cc}],
			})
			mv2.flags.ignore_permissions = True
			try:
				mv2.insert()
				print("t4     FAIL (group cost center accepted)")
				ok = False
			except frappe.ValidationError as e:
				g = "VR-025" in str(e)
				print(f"t4     group cost center blocked: {'OK' if g else 'FAIL: ' + str(e)}")
				ok = ok and g

		# T5 (VR-039): direct merge-log edit rejected.
		comp2 = make_test_asset(company, gross=15_000, submit=True)
		frappe.get_doc({
			"doctype": "Composite Merge Log Entry", "parenttype": "Asset",
			"parent": comp2.name, "parentfield": "merge_log", "idx": 1,
			"merged_source_asset": p1.name, "merged_date": nowdate(),
			"historical_value_at_merge": 1, "net_book_value_at_merge": 1, "status": "Active",
		}).db_insert()
		doc2 = frappe.get_doc("Asset", comp2.name)
		doc2.merge_log[0].status = "Reversed"
		doc2.flags.ignore_permissions = True
		try:
			doc2.save()
			print("t5     FAIL (direct merge-log edit saved)")
			ok = False
		except frappe.exceptions.ValidationError as e:
			# frappe's non-allow-on-submit protection OR our VR-039 guard —
			# either satisfies the rule.
			g = "VR-039" in str(e) or "Not allowed to change" in str(e)
			print(f"t5     direct merge-log edit blocked: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# T6 (VR-036): dropping a posted row raises.
		ads_name = frappe.db.get_value(
			"Asset Depreciation Schedule", {"asset": a0.name, "status": "Active"}, "name")
		sched = frappe.get_doc("Asset Depreciation Schedule", ads_name)
		sched.depreciation_schedule = [r for r in sched.depreciation_schedule if not r.journal_entry]
		sched.flags.ignore_permissions = True
		try:
			sched.save()
			print("t6     FAIL (posted row dropped)")
			ok = False
		except frappe.exceptions.ValidationError as e:
			# frappe's row-count protection OR our VR-036 guard — either
			# satisfies the rule.
			g = "VR-036" in str(e) or "Not allowed to change" in str(e)
			print(f"t6     posted-row drop blocked: {'OK' if g else 'FAIL: ' + str(e)}")
			ok = ok and g

		# T7: useful_life_after populated + movement summary row.
		ula = frappe.db.get_value(
			"Asset Activity",
			{"asset": a0.name, "transaction_category": "Depreciation"},
			"useful_life_after",
		)
		t7_ok = flt(ula) == 24
		print(f"t7     useful_life_after on TCC row = {ula} (want 24) {'OK' if t7_ok else 'FAIL'}")
		ok = ok and t7_ok

		# T8: AVA difference account defaults from the chain.
		liab = pick_plain_account(company, "Liability")
		frappe.db.set_value(
			"Company", company, "default_impairment_loss_account", liab, update_modified=False)
		ava_d = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": s1.name, "company": company,
			"date": nowdate(), "transaction_type": "Initial Impairment",
			"current_asset_value": 50_000, "new_asset_value": 49_000,
		})
		ava_d.flags.ignore_permissions = True
		ava_d.insert()
		t8_ok = ava_d.difference_account == liab
		print(f"t8     AVA impairment account defaulted = {ava_d.difference_account} {'OK' if t8_ok else 'FAIL'}")
		ok = ok and t8_ok

		# T9: the client's own Capitalized Maintenance worked example
		# ("FA Test After WP - with calculatioin.xlsx", sheet Dep Calcu):
		# 32,500 in service 25/03/2026 over 36 months = 29.680365/day,
		# posted through 30/06, merged 17/08 with Extended Life 0.
		# Depreciation must run to the MERGE date at the schedule's own
		# daily rate, both catch-up rows must POST, and only what is left
		# may transfer. Core's reschedule used to rebuild these rows on
		# its monthly model (32,500/36 = 902.78) and left the last one
		# unposted, overstating the transferred NBV.
		from asset_enterprise.setup.verify_tc import (
			_category, _cm_merge, _company, _depreciating_asset, _plain, _plain_asset,
			_post_through, _rows,
		)

		cm_company = _company()
		cm_cat = _category(cm_company, "TC IT Equipment", suspense=_plain(cm_company, "Liability"))
		cm_clearing = _plain(cm_company, "Liability")
		frappe.db.set_value(
			"Asset Category Account", {"parent": cm_cat, "company_name": cm_company},
			"capitalization_clearing_account", cm_clearing, update_modified=False)
		cm_target = _plain_asset(cm_company, cm_cat, "T9 COMPOSITE", 112_000)
		cm_target.submit()
		cm_src = _depreciating_asset(
			cm_company, cm_cat, "T9 SOURCE 32500/36m", 32_500, "2026-03-31", 36, "2026-03-25")
		_post_through(cm_src.name, "2026-06-30")
		_cm_merge(cm_company, cm_target.name, cm_src.name, posting_date="2026-08-17")

		_s, t9_rows = _rows(cm_src.name)
		by_date = {str(r.schedule_date): r for r in t9_rows}
		jul, aug = by_date.get("2026-07-31"), by_date.get("2026-08-17")
		accum = flt(sum(flt(r.depreciation_amount) for r in t9_rows), 2)
		t9_ok = (
			jul and aug
			and flt(jul.depreciation_amount, 2) == 920.09
			and flt(aug.depreciation_amount, 2) == 504.57
			and flt(aug.days_in_period) == 17
			and jul.journal_entry and aug.journal_entry
			and accum == 4_333.33
			and not [r for r in t9_rows if getdate(r.schedule_date) > getdate("2026-08-17")]
		)
		print(
			f"t9     client CM example: Jul={jul and flt(jul.depreciation_amount, 2)} (want 920.09) "
			f"Aug17={aug and flt(aug.depreciation_amount, 2)} over "
			f"{aug and aug.days_in_period}d (want 504.57/17), both posted="
			f"{bool(jul and jul.journal_entry and aug and aug.journal_entry)}, accum={accum:,.2f} "
			f"(want 4,333.33), transferred NBV={flt(32_500 - accum, 2):,.2f} (want 28,166.67) "
			f"{'OK' if t9_ok else 'FAIL'}"
		)
		ok = ok and bool(t9_ok)

		# T10 (Ruba, 18/08, ACC-ASS-2026-00118): a value change re-prices
		# the schedule only AFTER its date. Asset 125,000 / 60m in
		# service 31/03/2026, posted through 30/06 at 68.493151/day, then
		# +35,945.21 added on 18/08. July's unposted row must stay
		# 2,123.29 to the cent; August splits at the event (18 days old
		# rate + 13 days new) into one 31-day row; September onward runs
		# at the new rate; the future rows still sum to the full NBV.
		from asset_enterprise.depreciation import enable_depreciation as t10_enable

		t10 = make_test_asset(company, gross=125_000, submit=True)
		t10_enable(
			t10.name, total_number_of_depreciations=60,
			available_for_use_date="2026-03-31", depreciation_start_date="2026-03-31",
		)
		from asset_enterprise.depreciation import post_schedule_entries

		t10_sched = frappe.db.get_value(
			"Asset Depreciation Schedule",
			{"asset": t10.name, "status": "Active", "docstatus": 1}, "name",
		)
		post_schedule_entries(t10_sched, "2026-06-30")

		t10_diff = pick_plain_account(company, "Liability")
		t10_ava = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": t10.name, "company": company,
			"date": "2026-08-18", "transaction_type": "Upward Revaluation",
			"current_asset_value": 118_698.64, "new_asset_value": 154_643.85,
			"difference_account": t10_diff,
			"cost_center": frappe.db.get_value("Asset", t10.name, "cost_center"),
		})
		t10_ava.flags.ignore_permissions = True
		t10_ava.insert()
		t10_ava.submit()

		t10_rows = frappe.db.sql(
			"""
			select ds.schedule_date, ds.depreciation_amount, ds.days_in_period,
			       ds.daily_rate, ifnull(ds.journal_entry, '') je
			from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			order by ds.schedule_date
			""",
			t10.name, as_dict=True,
		)
		by = {str(r.schedule_date): r for r in t10_rows}
		jul, aug, sep = by.get("2026-07-31"), by.get("2026-08-31"), by.get("2026-09-30")
		future_sum = flt(sum(flt(r.depreciation_amount) for r in t10_rows if not r.je), 2)
		# Core's counter must track the derived NBV after value events —
		# it is what core's status logic reads (client, ACC-ASS-2026-00125:
		# counter went negative and status flipped to Fully Depreciated
		# with a row still unposted).
		t10_counter = flt(frappe.db.get_value(
			"Asset Finance Book", {"parent": t10.name}, "value_after_depreciation"))
		t10_status = frappe.db.get_value("Asset", t10.name, "status")
		t10_ok = (
			jul and aug and sep
			and flt(jul.depreciation_amount, 2) == 2_123.29
			and flt(jul.daily_rate, 6) == 68.493151
			and cint(aug.days_in_period) == 31
			and flt(aug.depreciation_amount, 2) == 2_400.78
			and flt(sep.depreciation_amount, 2) == 2_695.15
			and future_sum == 154_643.85  # the post-event NBV, already net of posted
			and flt(t10_counter, 2) == 154_643.85
			and t10_status == "Partially Depreciated"
		)
		print(
			f"t10    rate change only after its date: Jul={jul and flt(jul.depreciation_amount, 2)} "
			f"@{jul and flt(jul.daily_rate, 6)} (want 2,123.29 @68.493151 UNCHANGED), "
			f"Aug={aug and flt(aug.depreciation_amount, 2)}/{aug and aug.days_in_period}d "
			f"(want 2,400.78/31 split at 18/08), Sep={sep and flt(sep.depreciation_amount, 2)} "
			f"(want 2,695.15 new rate), future-sum={future_sum:,.2f} (want 154,643.85), "
			f"core counter={t10_counter:,.2f} (want =NBV) status={t10_status} "
			f"(want Partially Depreciated) {'OK' if t10_ok else 'FAIL'}"
		)
		ok = ok and bool(t10_ok)

		# T11 (client, ACC-ASS-2026-00127): Extended Life / the fully-
		# depreciated treatment is refused on a target still carrying
		# NBV — it re-anchored the horizon to the posting date and
		# collapsed years of remaining life. Mid-life extension goes
		# through a Useful Life Adjustment instead.
		t11_probe = frappe.get_doc({
			"doctype": "Asset Capitalization",
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Standard Maintenance",
			"target_asset": t10.name,  # mid-life, NBV 154,643.85
			"company": company,
			"extended_life_months": 12,
			"fully_depreciated_treatment": "Add Value and Extend Life",
			"asset_items": [{"asset": s1.name}],
		})
		try:
			t11_probe._validate_cm()
			print("t11    FAIL (fully-depreciated TREATMENT accepted on a mid-life target)")
			ok = False
		except frappe.ValidationError as e:
			t11_ok = "Extended Life" in str(e)
			print(f"t11    treatment on mid-life target refused: {'OK' if t11_ok else 'FAIL: ' + str(e)[:120]}")
			ok = ok and t11_ok

		# T11b (client, 19/08): Extended Life months ALONE on a LIVING
		# target extend the CURRENT end of life — an overhaul that
		# prolongs service life, folded into the merge.
		from asset_enterprise.depreciation import active_schedule_horizon as t11_horizon
		from asset_enterprise.setup.verify_tc import (
			_category as t11_cat_fn, _plain as t11_plain, _cm_merge as t11_cm,
			_depreciating_asset as t11_dep_asset,
		)

		t11_company_cat = t11_cat_fn(company, "TC IT Equipment", suspense=t11_plain(company, "Liability"))
		frappe.db.set_value(
			"Asset Category Account", {"parent": t11_company_cat, "company_name": company},
			"capitalization_clearing_account", t11_plain(company, "Liability"), update_modified=False)
		t11_tgt = t11_dep_asset(company, t11_company_cat, "T11B TARGET", 60_000, "2026-02-28", 36, "2026-02-01")
		t11_src = t11_dep_asset(company, t11_company_cat, "T11B SOURCE", 12_000, "2026-02-28", 36, "2026-02-01")
		h11_before = getdate(t11_horizon(t11_tgt.name))
		fb11_before = cint(frappe.db.get_value(
			"Asset Finance Book", {"parent": t11_tgt.name}, "total_number_of_depreciations"))
		cap11 = frappe.get_doc({
			"doctype": "Asset Capitalization", "company": company,
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Standard Maintenance",
			"target_asset": t11_tgt.name, "posting_date": nowdate(), "set_posting_time": 1,
			"extended_life_months": 12,
			"asset_items": [{"asset": t11_src.name}]})
		cap11.flags.ignore_permissions = True
		cap11.insert()
		cap11.submit()
		h11_after = getdate(t11_horizon(t11_tgt.name))
		fb11_after = cint(frappe.db.get_value(
			"Asset Finance Book", {"parent": t11_tgt.name}, "total_number_of_depreciations"))
		t11b_ok = (
			h11_after == getdate(add_months(h11_before, 12))
			and fb11_after == fb11_before + 12
		)
		print(f"t11b   living-target Extended Life: horizon {h11_before} -> {h11_after} "
		      f"(want +12mo), fb periods {fb11_before} -> {fb11_after} (want +12) "
		      f"{'OK' if t11b_ok else 'FAIL'}")
		ok = ok and t11b_ok

		# T12–T14 (19/08 caller audit): every path that regenerates a
		# schedule must resume from the last POSTED row, and non-SL
		# curves must survive our §4.3 rebuild.
		from asset_enterprise import disposal as t_disposal
		from asset_enterprise.depreciation import post_schedule_entries as t_post

		def _mk_posted(gross):
			x = make_test_asset(company, gross=gross, submit=True)
			start = get_first_day(add_months(nowdate(), -4))
			enable_depreciation(
				x.name, total_number_of_depreciations=36,
				available_for_use_date=str(start), depreciation_start_date=str(start),
			)
			sched = frappe.db.get_value("Asset Depreciation Schedule",
				{"asset": x.name, "status": "Active", "docstatus": 1}, "name")
			t_post(sched, str(add_days(get_first_day(nowdate()), -1)))
			return x

		def _future(asset_name):
			return frappe.db.sql("""
				select ds.schedule_date, ds.depreciation_amount, ds.days_in_period
				from `tabDepreciation Schedule` ds
				join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
				where ads.asset = %s and ads.status='Active' and ads.docstatus=1
				  and ifnull(ds.journal_entry,'') = ''
				order by ds.schedule_date""", asset_name, as_dict=True)

		mid_month = str(add_days(get_first_day(nowdate()), 14))

		# T12: mid-month partial scrap — the event month stays one full row.
		p1 = _mk_posted(36_000)
		t_disposal.partial_scrap_asset(
			p1.name, scrap_value=6_000, scrap_date=mid_month, scrapping_type="Damage")
		f1_rows = _future(p1.name)
		t12_ok = f1_rows and cint(f1_rows[0].days_in_period) >= 28
		print(f"t12    partial scrap mid-month: first future row "
		      f"{f1_rows and (str(f1_rows[0].schedule_date), f1_rows[0].days_in_period)} "
		      f"(want full month) {'OK' if t12_ok else 'FAIL'}")
		ok = ok and bool(t12_ok)

		# T12b (client, 19/08 — ACC-ASS-2026-00139): a full scrap POSTS its
		# proration up to the scrap date. Core's disposal path handed the
		# posting a list instead of a name, the blanket except swallowed
		# it, and the freeze dropped the unposted rows — usage days
		# silently became disposal loss.
		p2 = _mk_posted(24_000)
		t_disposal.scrap_asset(p2.name, scrap_date=mid_month, scrapping_type="Damage")
		p2_rows = frappe.db.sql("""
			select ds.schedule_date, ds.days_in_period, ifnull(ds.journal_entry,'') je
			from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			order by ds.schedule_date""", p2.name, as_dict=True)
		unposted_due = [str(r.schedule_date) for r in p2_rows if not r.je]
		stub = p2_rows[-1] if p2_rows else None
		t12b_ok = (
			not unposted_due
			and stub and str(stub.schedule_date) == mid_month
			and cint(stub.days_in_period) == 15
		)
		print(f"t12b   full scrap posts ALL rows incl. the stub: unposted={unposted_due or 'none'}, "
		      f"last row {stub and (str(stub.schedule_date), stub.days_in_period)} "
		      f"(want {mid_month}/15d incl. the scrap day) {'OK' if t12b_ok else 'FAIL'}")
		ok = ok and t12b_ok

		# T13: Path 1 restore — the schedule must come back ALIVE.
		from asset_enterprise.restore import restore_asset as t_restore

		t_restore(p2.name)
		f2_rows = _future(p2.name)
		t13_ok = bool(f2_rows) and getdate(f2_rows[-1].schedule_date) > getdate(nowdate())
		print(f"t13    path-1 restore: {len(f2_rows)} future rows, horizon "
		      f"{f2_rows and f2_rows[-1].schedule_date} (want a live schedule) "
		      f"{'OK' if t13_ok else 'FAIL'}")
		ok = ok and t13_ok

		# T14: a WDV asset keeps its declining curve through our on_submit.
		w1 = make_test_asset(company, gross=50_000, submit=False, with_depreciation=True)
		w1.finance_books[0].depreciation_method = "Written Down Value"
		w1.finance_books[0].rate_of_depreciation = 40
		w1.finance_books[0].daily_prorata_based = 0
		w1.flags.ignore_permissions = True
		w1.save()
		w1.submit()
		w_rows = frappe.db.sql("""
			select ds.depreciation_amount from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status='Active' and ads.docstatus=1
			order by ds.schedule_date limit 15""", w1.name)
		amts = [flt(r[0]) for r in w_rows]
		# WDV declines YEAR over year (equal monthly rows within a year);
		# our flattened rebuild would instead show day-count variation
		# month to month and no year step.
		t14_ok = len(amts) >= 14 and amts[1] > amts[13] and amts[1] == amts[2]
		print(f"t14    WDV curve preserved: yr1 {amts[1:3]} vs yr2 {amts[13:14]} "
		      f"(want yearly decline, flat within year) {'OK' if t14_ok else 'FAIL'}")
		ok = ok and t14_ok

		# T15–T17 (client, 19/08 — ACC-JV-2026-00661): the transaction
		# type governs the fields. A UL adjustment posts NO JE of its own
		# (exhaustion depreciation aside); mixed intent is refused both
		# ways; the §3.4 combined type carries both legs.
		def _ava_doc(asset_name, ttype, **extra):
			base = {
				"doctype": "Asset Value Adjustment", "asset": asset_name,
				"company": company, "date": nowdate(), "transaction_type": ttype,
			}
			base.update(extra)
			doc = frappe.get_doc(base)
			doc.flags.ignore_permissions = True
			return doc

		# T15: the client's exact malformed entry — UL with a value pair.
		g1 = _mk_posted(18_000)
		try:
			_ava_doc(g1.name, "Useful Life Adjustment",
				current_asset_value=17_000, new_asset_value=0,
				adjusted_life_months=-12).insert()
			# new=0 is treated as untouched and neutralised — must yield
			# difference 0 and NO core JE on submit.
			pass
		except frappe.ValidationError:
			pass
		bad = _ava_doc(g1.name, "Useful Life Adjustment",
			current_asset_value=17_000, new_asset_value=9_000,
			adjusted_life_months=-6)
		try:
			bad.insert()
			print("t15    FAIL (UL with conflicting value pair accepted)")
			ok = False
		except frappe.ValidationError as e:
			t15_ok = "Value + Life Adjustment" in str(e)
			print(f"t15    UL with conflicting values refused: {'OK' if t15_ok else 'FAIL: ' + str(e)[:100]}")
			ok = ok and t15_ok

		# T16: a UL adjustment posts exactly ZERO JEs of its own.
		je_before = frappe.db.count("Journal Entry", {"docstatus": 1})
		ul_ok_doc = _ava_doc(g1.name, "Useful Life Adjustment", adjusted_life_months=6)
		ul_ok_doc.insert()
		ul_ok_doc.submit()
		je_after = frappe.db.count("Journal Entry", {"docstatus": 1})
		# provenance: the regenerated schedule names the AVA that caused it,
		# and the document carries a real series name (client, 19/08).
		t16_trigger = frappe.db.get_value("Asset Depreciation Schedule",
			{"asset": g1.name, "status": "Active", "docstatus": 1}, "triggered_by")
		t16_ok = (
			je_after == je_before
			and not ul_ok_doc.get("journal_entry")
			and flt(ul_ok_doc.difference_amount) == 0
			and ul_ok_doc.name.startswith("ACC-AVA-")
			and t16_trigger == ul_ok_doc.name
		)
		print(f"t16    UL adjustment posts no JE (difference normalised to "
		      f"{flt(ul_ok_doc.difference_amount)}); name={ul_ok_doc.name} (want ACC-AVA-*); "
		      f"schedule triggered_by={t16_trigger} {'OK' if t16_ok else 'FAIL'}")
		ok = ok and t16_ok

		# T17: value-only type refuses life fields.
		try:
			_ava_doc(g1.name, "Upward Revaluation",
				current_asset_value=17_000, new_asset_value=20_000,
				adjusted_life_months=6,
				difference_account=pick_plain_account(company, "Liability")).insert()
			print("t17    FAIL (Upward Revaluation with life months accepted)")
			ok = False
		except frappe.ValidationError as e:
			t17_ok = "Value + Life Adjustment" in str(e)
			print(f"t17    value type with life fields refused: {'OK' if t17_ok else 'FAIL: ' + str(e)[:100]}")
			ok = ok and t17_ok

		# T18: the §3.4 combined type carries BOTH legs — one value JE,
		# HAV moves, horizon moves.
		g2 = _mk_posted(20_000)
		h_g2 = getdate(active_schedule_horizon(g2.name))
		combo = _ava_doc(g2.name, "Value + Life Adjustment",
			current_asset_value=flt(recalculate_asset_values(g2.name, save=False)["net_book_value"]),
			new_asset_value=flt(recalculate_asset_values(g2.name, save=False)["net_book_value"]) + 4_000,
			adjusted_life_months=6,
			difference_account=pick_plain_account(company, "Liability"))
		combo.insert()
		combo.submit()
		v_g2 = recalculate_asset_values(g2.name, save=False)
		h_g2_after = getdate(active_schedule_horizon(g2.name))
		t18_ok = (
			bool(combo.get("journal_entry"))
			and flt(v_g2["historical_asset_value"]) == 24_000
			and h_g2_after == getdate(add_months(h_g2, 6))
		)
		print(f"t18    Value + Life: JE={combo.get('journal_entry')} HAV "
		      f"{flt(v_g2['historical_asset_value']):,.2f} (want 24,000) horizon "
		      f"{h_g2} -> {h_g2_after} (want +6mo) {'OK' if t18_ok else 'FAIL'}")
		ok = ok and t18_ok

		# T19 (client, 19/08): reclassification targets the new MATERIAL —
		# the asset is created by the transaction, like purchasing, with
		# available-for-use = the posting date.
		from asset_enterprise.setup.verify_tc import _category as t19_category, _plain as t19_plain

		r_src = _mk_posted(12_000)
		t19_cat = t19_category(company, "T19 New Category", suspense=t19_plain(company, "Liability"))
		if not frappe.db.exists("Item", "T19 Reclass Item"):
			t19_item = frappe.get_doc({
				"doctype": "Item", "item_code": "T19 Reclass Item", "item_name": "T19 Reclass Item",
				"item_group": "Sub Assemblies", "stock_uom": "Nos", "is_fixed_asset": 1,
				"is_stock_item": 0, "asset_category": t19_cat})
			t19_item.flags.ignore_permissions = True
			t19_item.insert()
		t19_cap = frappe.get_doc({
			"doctype": "Asset Capitalization", "company": company,
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Reclassification / Asset Category Transfer",
			"target_item": "T19 Reclass Item", "posting_date": nowdate(),
			"asset_items": [{"asset": r_src.name}]})
		t19_cap.flags.ignore_permissions = True
		t19_cap.insert()
		t19_cap.submit()
		t19_cap.reload()
		new_asset = frappe.db.get_value("Asset", t19_cap.target_asset,
			["asset_category", "available_for_use_date", "net_purchase_amount",
			 "opening_accumulated_depreciation", "docstatus", "reclassified_from"], as_dict=True)
		src_after = frappe.db.get_value("Asset", r_src.name, ["docstatus", "status"], as_dict=True)
		t19_ok = (
			bool(t19_cap.target_asset)
			and new_asset.asset_category == t19_cat
			and getdate(new_asset.available_for_use_date) == getdate(nowdate())
			and new_asset.docstatus == 1
			and flt(new_asset.net_purchase_amount) == 12_000
			and new_asset.reclassified_from == r_src.name
			and src_after.docstatus == 2
		)
		print(f"t19    reclass by material: created {t19_cap.target_asset} in {new_asset.asset_category} "
		      f"AFU={new_asset.available_for_use_date} (want today) gross "
		      f"{flt(new_asset.net_purchase_amount):,.2f} src cancelled={src_after.docstatus == 2} "
		      f"{'OK' if t19_ok else 'FAIL'}")
		ok = ok and t19_ok

		# T20 (client, 19/08 — ACC-AVA-2026-00002): the desk's Cancel All
		# dialog must never offer the depreciation schedule, and a direct
		# schedule cancel is refused; the asset reversal (the one flow
		# that legitimately cancels schedules) keeps working — covered by
		# F4 above.
		exempted = frappe.get_hooks("auto_cancel_exempted_doctypes")
		t20a_ok = "Asset Depreciation Schedule" in exempted
		g3 = _mk_posted(9_000)
		g3_sched = frappe.get_doc("Asset Depreciation Schedule", frappe.db.get_value(
			"Asset Depreciation Schedule", {"asset": g3.name, "status": "Active", "docstatus": 1}, "name"))
		try:
			g3_sched.cancel()
			print("t20    FAIL (direct schedule cancel allowed)")
			ok = False
		except frappe.ValidationError as e:
			t20b_ok = "superseded" in str(e)
			print(f"t20    Cancel-All exemption={t20a_ok}; direct schedule cancel refused: "
			      f"{'OK' if (t20a_ok and t20b_ok) else 'FAIL'}")
			ok = ok and t20a_ok and t20b_ok

		# T21 (client, 21/08 — "as AVA, could we add days"): a capitalized
		# REPAIR grants life in months AND days, and both move the end of
		# life. The months half is a regression in its own right: core
		# grants them on Asset Finance Book.increase_in_asset_life, which
		# only core's own schedule builder reads — under supersession that
		# builder never runs, so the extension was recorded and ignored.
		r1 = make_test_asset(company, gross=36_000, submit=True)
		enable_depreciation(
			r1.name, total_number_of_depreciations=36, frequency_of_depreciation=1,
			depreciation_start_date=nowdate(),
		)
		r1_before = getdate(active_schedule_horizon(r1.name))
		rep = frappe.get_doc({
			"doctype": "Asset Repair", "asset": r1.name, "company": company,
			"failure_date": nowdate(), "completion_date": nowdate(),
			"repair_status": "Completed", "repair_cost": 3_000,
			"capitalize_repair_cost": 1,
			"cost_center": frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name"),
			"increase_in_asset_life": 6, "increase_in_asset_life_days": 10,
		})
		rep.flags.ignore_permissions = True
		rep.insert()
		rep.submit()
		r1_after = getdate(active_schedule_horizon(r1.name))
		r1_want = getdate(add_days(add_months(r1_before, 6), 10))
		# and the reversal hands the life back with the value
		rep.reload()
		rep.cancel()
		r1_rev = getdate(active_schedule_horizon(r1.name))
		fb_after = frappe.db.get_value(
			"Asset Finance Book", {"parent": r1.name},
			["increase_in_asset_life", "life_extension_days"], as_dict=True,
		)
		t21_ok = (
			r1_after == r1_want
			and r1_rev == r1_before
			and cint(fb_after.increase_in_asset_life) == 0
			and cint(fb_after.life_extension_days) == 0
		)
		print(
			f"t21    repair +6 months +10 days: horizon {r1_before} -> {r1_after} "
			f"(want {r1_want}); after reversal {r1_rev} (want {r1_before}); "
			f"counters m={fb_after.increase_in_asset_life} d={fb_after.life_extension_days} "
			f"{'OK' if t21_ok else 'FAIL'}"
		)
		ok = ok and t21_ok

		# T22 (client, 21/08): Extended Life on a Capitalized Maintenance
		# takes days too, and a reversal retracts BOTH — the reversal
		# document carries no grant of its own, so it reads the source's.
		c1 = make_test_asset(company, gross=48_000, submit=True)
		enable_depreciation(
			c1.name, total_number_of_depreciations=48, frequency_of_depreciation=1,
			depreciation_start_date=nowdate(),
		)
		c1_before = getdate(active_schedule_horizon(c1.name))
		from asset_enterprise.setup.verify_tc import (
			_plain as t22_plain,
			_service_item as t22_service,
		)

		cap = frappe.get_doc({
			"doctype": "Asset Capitalization", "company": company,
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Standard Maintenance",
			"target_asset": c1.name, "posting_date": nowdate(), "set_posting_time": 1,
			"extended_life_months": 18, "extended_life_days": 10,
			"service_items": [{
				"item_code": t22_service(),
				"qty": 1, "rate": 5_000, "amount": 5_000,
				"expense_account": t22_plain(company, "Expense"),
				"cost_center": frappe.db.get_value(
					"Cost Center", {"company": company, "is_group": 0}, "name"),
			}],
		})
		cap.flags.ignore_permissions = True
		cap.insert()
		cap.submit()
		c1_after = getdate(active_schedule_horizon(c1.name))
		c1_want = getdate(add_days(add_months(c1_before, 18), 10))
		cap.reload()
		cap.cancel()
		c1_rev = getdate(active_schedule_horizon(c1.name))
		t22_ok = c1_after == c1_want and c1_rev == c1_before
		print(
			f"t22    CM +18 months +10 days: horizon {c1_before} -> {c1_after} "
			f"(want {c1_want}); after reversal {c1_rev} (want {c1_before}) "
			f"{'OK' if t22_ok else 'FAIL'}"
		)
		ok = ok and t22_ok

		# T23: a rebuild FROM LIFE (restore, day-count rebuild) must keep
		# both grants — they live on the finance book, not only in the
		# horizon of the generation that granted them.
		from asset_enterprise.depreciation import schedule_horizon_from_life

		t23_ok = getdate(schedule_horizon_from_life(c1.name)) == c1_before
		u3_life = getdate(schedule_horizon_from_life(u3.name))
		t23_ok = t23_ok and u3_life == getdate(add_days(add_months(h3_before, 2), 15))
		print(
			f"t23    horizon-from-life keeps grants: CM target {schedule_horizon_from_life(c1.name)} "
			f"(want {c1_before} after reversal); AVA asset {u3_life} (want {h3_want}) "
			f"{'OK' if t23_ok else 'FAIL'}"
		)
		ok = ok and t23_ok

	finally:
		frappe.db.rollback(save_point="phase11_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 11:", "PASS" if ok else "FAIL")
