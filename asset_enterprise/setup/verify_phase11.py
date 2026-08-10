"""Phase 11a verification (audit gap closure) — run:
bench --site <site> execute asset_enterprise.setup.verify_phase11.run

F0 accum single-count · F1 salvage in reschedule base · F2 UL horizon
+ RUL exhaustion · F3 manual-depreciation cancel gate · F4 opening-JE
reversal on asset cancel · F5 reclassification model · F6 straddling
depreciation reversal at merge · F7 SI disposal gate.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate


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
			expense = frappe.db.get_value(
				"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
			)
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
		f2b_ok = flt(v2["net_book_value"]) == 0 and bool(imm_ft)
		print(f"f2b    RUL exhausted -> NBV {v2['net_book_value']} (want 0) immediate-FT={bool(imm_ft)} {'OK' if f2b_ok else 'FAIL'}")
		ok = ok and f2b_ok

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
			"location": r_src.location, "is_existing_asset": 1,
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
		liab = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Liability", "is_group": 0}, "name")
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

	finally:
		frappe.db.rollback(save_point="phase11_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 11:", "PASS" if ok else "FAIL")
