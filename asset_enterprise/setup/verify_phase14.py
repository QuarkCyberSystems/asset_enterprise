"""Phase 14 verification — client tickets of 27/09/2026. Run:
bench --site <site> execute asset_enterprise.setup.verify_phase14.run

  FA-005  a reversed depreciation period is marked, its value restored,
          and it posts again on the next run
  FA-006  depreciation is reversed latest period first
  FA-003  Mass Depreciation Reversal (period x scope, run mode, skips,
          not cancellable); a posted Mass Asset Depreciation is not
          cancellable
  FA-001  Edit Depreciation until the first posting, refused after
  FA-007  first posting at the end of the in-service month (draft seed,
          typed date kept, dialog default, in-service before purchase)
  FA-008  a merged source keeps "Disposed"; an invoice after the merge
          is expensed (Case A.02), no AVA on the source
  FA-002  one unit of a fixed-asset item is one asset (Item, receipt)
  GAP-027 reverse every period latest first, then cancel the asset
  repair  reversals and invoice adjustments written before the fixes

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_months, flt, get_first_day, get_last_day, getdate, nowdate

from asset_enterprise.setup.test_fixtures import (
	control_invoice,
	control_purchase,
	ensure_enterprise_test_defaults,
	make_test_asset,
	pick_company,
)

MONTHS = [
	"January", "February", "March", "April", "May", "June",
	"July", "August", "September", "October", "November", "December",
]


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _refused(fn, *args, **kwargs):
	"""The refusal text, or None when `fn` went through."""
	try:
		fn(*args, **kwargs)
		return None
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return str(e) or type(e).__name__


def _active(asset):
	return frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset, "status": "Active", "docstatus": 1}, "name"
	)


def _rows(schedule):
	return frappe.db.sql(
		"""select name, schedule_date, depreciation_amount, journal_entry, reversal_journal_entry
		   from `tabDepreciation Schedule` where parent = %s order by idx""",
		schedule,
		as_dict=True,
	)


def _nbv(asset):
	return flt(frappe.db.get_value("Asset", asset, "net_book_value"))


def _depreciating_asset(company, gross, months_back=4):
	from asset_enterprise.depreciation import enable_depreciation, post_schedule_entries

	asset = make_test_asset(company, gross=gross, submit=True)
	schedule = enable_depreciation(
		asset.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
		depreciation_start_date=get_last_day(add_months(nowdate(), -months_back)),
		available_for_use_date=get_first_day(add_months(nowdate(), -months_back)),
	)
	post_schedule_entries(schedule, date=nowdate())
	return asset.name


def _reverse(je, posting_date=None):
	from qcs_platform.core.journal_entry import make_reverse_journal_entry

	rev = make_reverse_journal_entry(je)
	rev.posting_date = posting_date or nowdate()
	rev.flags.ignore_permissions = True
	rev.insert()
	rev.submit()
	return rev.name


def _run():
	ok = True
	company = pick_company()
	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	frappe.db.savepoint("phase14_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		ensure_enterprise_test_defaults()
		frappe.get_doc(
			{"doctype": "Asset Settings Authority Role", "parenttype": "Asset Settings",
			 "parent": "Asset Settings", "parentfield": "mass_depreciation_authority_roles",
			 "role": "System Manager"}
		).db_insert()
		from asset_enterprise.depreciation import post_schedule_entries

		# ============ FA-006 / FA-005: single reversal ====================
		a = _depreciating_asset(company, 120_000)
		booked = [r for r in _rows(_active(a)) if r.journal_entry]
		middle = _refused(_reverse, booked[1].journal_entry)
		c = bool(middle) and "latest period first" in middle and booked[-1].journal_entry in middle
		print(f"fa006  middle period refused, names the entry to reverse first: {'OK' if c else 'FAIL'} ({middle and middle[:90]})")
		ok = ok and c

		nbv_before = _nbv(a)
		last = booked[-1]
		rev = _reverse(last.journal_entry)
		rows = _rows(_active(a))
		marked = [r for r in rows if r.journal_entry == last.journal_entry]
		fresh = [r for r in rows if r.schedule_date == last.schedule_date and not r.journal_entry]
		ft = frappe.db.get_value(
			"Financial Treatment",
			{"asset": a, "journal_entry": last.journal_entry, "transaction_category": "Depreciation"},
			"status",
		)
		c = (
			marked and marked[0].reversal_journal_entry == rev
			and len(fresh) == 1
			and flt(fresh[0].depreciation_amount) == flt(last.depreciation_amount)
			and ft == "Reversed"
			and flt(_nbv(a) - nbv_before, 2) == flt(last.depreciation_amount, 2)
		)
		print(
			f"fa005  reversal marks the row, pairs the FT ({ft}), restores {flt(_nbv(a) - nbv_before, 2)} "
			f"(want {last.depreciation_amount}) and regenerates the period: {'OK' if c else 'FAIL'}"
		)
		ok = ok and bool(c)

		reposted = post_schedule_entries(_active(a), date=nowdate())
		c = len(reposted) == 1 and flt(_nbv(a), 2) == flt(nbv_before, 2)
		print(f"fa005  the reversed period posts again on the next run: {'OK' if c else 'FAIL'} ({len(reposted)} row)")
		ok = ok and c

		# ============ FA-003: Mass Depreciation Reversal ==================
		b = _depreciating_asset(company, 60_000)
		c_asset = _depreciating_asset(company, 36_000)
		period = getdate(get_last_day(add_months(nowdate(), -1)))

		def mdr(month_date, **fields):
			doc = frappe.get_doc(
				{"doctype": "Mass Depreciation Reversal", "company": company,
				 "period_month": MONTHS[month_date.month - 1], "period_year": month_date.year,
				 "posting_date": nowdate(), "reason": "phase 14", **fields}
			)
			doc.flags.ignore_permissions = True
			doc.insert()
			doc.submit()
			return doc

		earlier = _refused(
			mdr, getdate(add_months(period, -1)), mode="Selected Assets",
			selected_assets=[{"asset": b}, {"asset": c_asset}],
		)
		c = bool(earlier) and "latest period first" in earlier
		print(f"fa003  an earlier period with later periods booked reverses nothing: {'OK' if c else 'FAIL'}")
		ok = ok and c

		nbv_b, nbv_c = _nbv(b), _nbv(c_asset)
		doc = mdr(period, mode="Selected Assets", selected_assets=[{"asset": b}, {"asset": c_asset}])
		outcomes = sorted((r.asset, r.outcome) for r in doc.result_summary)
		c = (
			outcomes == sorted([(b, "Reversed"), (c_asset, "Reversed")])
			and _nbv(b) > nbv_b and _nbv(c_asset) > nbv_c
			and all(r.reversal_journal_entry for r in doc.result_summary)
		)
		print(f"fa003  selected assets, latest period: {outcomes} {'OK' if c else 'FAIL'}")
		ok = ok and bool(c)

		c = bool(_refused(doc.cancel))
		print(f"fa003  a Mass Depreciation Reversal is not cancellable: {'OK' if c else 'FAIL'}")
		ok = ok and c

		mad = frappe.get_doc(
			{"doctype": "Mass Asset Depreciation", "company": company, "posting_date": period,
			 "mode": "Selected Assets", "selected_assets": [{"asset": b}, {"asset": c_asset}]}
		)
		mad.flags.ignore_permissions = True
		mad.insert()
		mad.submit()
		posted = [r.journal_entry for r in mad.result_summary if r.outcome == "Posted"]
		refusal = _refused(mad.cancel)
		c = len(posted) == 2 and bool(refusal) and "Mass Depreciation Reversal" in refusal
		print(f"fa003  the re-run posts both again; cancelling it is refused and points to the reversal: {'OK' if c else 'FAIL'}")
		ok = ok and c

		run_doc = mdr(period, mode="Mass Depreciation Run", mass_asset_depreciation=mad.name)
		c = sorted(r.journal_entry for r in run_doc.result_summary if r.outcome == "Reversed") == sorted(posted)
		print(f"fa003  run mode reverses exactly the run's entries: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# ============ FA-001: Edit Depreciation ===========================
		from asset_enterprise.depreciation import (
			enable_depreciation,
			edit_depreciation,
			edit_depreciation_defaults,
		)

		e = make_test_asset(company, gross=48_000, submit=True)
		enable_depreciation(
			e.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
			depreciation_start_date=get_last_day(add_months(nowdate(), -2)),
			available_for_use_date=get_first_day(add_months(nowdate(), -2)),
		)
		editable = "refusal" not in edit_depreciation_defaults(e.name)
		schedule = edit_depreciation(
			e.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_last_day(add_months(nowdate(), -2)),
			expected_value_after_useful_life=0,
		)
		life = frappe.db.get_value("Asset Finance Book", {"parent": e.name}, "total_number_of_depreciations")
		actives = frappe.db.count("Asset Depreciation Schedule", {"asset": e.name, "status": "Active", "docstatus": 1})
		c = editable and life == 24 and actives == 1 and _active(e.name) == schedule
		print(f"fa001  edit before the first posting rebuilds the schedule (life {life}, {actives} active): {'OK' if c else 'FAIL'}")
		ok = ok and bool(c)

		post_schedule_entries(schedule, date=nowdate())
		after = edit_depreciation_defaults(e.name).get("refusal")
		refusal = _refused(edit_depreciation, e.name, total_number_of_depreciations=12)
		c = bool(after) and bool(refusal)
		print(f"fa001  edit refused once an entry has posted: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# ============ FA-007: first posting at the in-service month end ===
		from asset_enterprise.api import enable_depreciation_defaults

		category = frappe.get_doc("Asset Category", "AE Smoke Category")
		if not category.finance_books:
			category.append("finance_books", {"depreciation_method": "Straight Line",
				"total_number_of_depreciations": 24, "frequency_of_depreciation": 1})
		seeded = get_last_day(add_months(nowdate(), 1))
		category.finance_books[0].depreciation_start_date = seeded
		category.flags.ignore_permissions = True
		category.save()
		afu = get_first_day(add_months(nowdate(), -6))
		purchase = add_months(afu, 1)

		def existing(start):
			doc = frappe.get_doc(
				{"doctype": "Asset", "company": company, "item_code": "AE-SMOKE-ITEM",
				 "asset_name": "AE Smoke Asset", "asset_category": "AE Smoke Category",
				 "location": e.location, "asset_type": "Existing Asset",
				 "purchase_amount": 240_000, "net_purchase_amount": 240_000,
				 "available_for_use_date": afu, "purchase_date": purchase, "calculate_depreciation": 1,
				 "finance_books": [{"depreciation_method": "Straight Line", "total_number_of_depreciations": 24,
					"frequency_of_depreciation": 1, "depreciation_start_date": start, "daily_prorata_based": 1}]}
			)
			doc.flags.ignore_permissions = True
			doc.insert()
			return doc

		f1 = existing(seeded)
		c = getdate(f1.finance_books[0].depreciation_start_date) == getdate(get_last_day(afu))
		print(f"fa007  a seeded category date becomes the in-service month end, before the purchase date: {'OK' if c else 'FAIL'} ({f1.finance_books[0].depreciation_start_date})")
		ok = ok and c
		f1.submit()
		first = _rows(_active(f1.name))[0]
		c = getdate(first.schedule_date) == getdate(get_last_day(afu))
		print(f"fa007  the first schedule row is that month end: {'OK' if c else 'FAIL'} ({first.schedule_date})")
		ok = ok and c

		typed = get_last_day(add_months(afu, 3))
		f2 = existing(typed)
		c = getdate(f2.finance_books[0].depreciation_start_date) == getdate(typed)
		print(f"fa007  a date the user typed is kept: {'OK' if c else 'FAIL'}")
		ok = ok and c

		g = make_test_asset(company, gross=10_000, submit=True)
		frappe.db.set_value("Asset", g.name, "available_for_use_date", afu)
		dialog = enable_depreciation_defaults(g.name).get("depreciation_start_date")
		c = getdate(dialog) == getdate(get_last_day(afu))
		print(f"fa007  Enable Depreciation defaults to the in-service month end: {'OK' if c else 'FAIL'} ({dialog})")
		ok = ok and c

		# ============ FA-008: invoice after a merge =======================
		from asset_enterprise.status import OFF_REGISTER

		cap_clearing = frappe.db.get_value("Company", company, "default_capitalization_clearing_account")
		if not cap_clearing:
			from asset_enterprise.setup.test_fixtures import pick_plain_account

			frappe.db.set_value(
				"Company", company, "default_capitalization_clearing_account",
				pick_plain_account(company, "Liability"), update_modified=False,
			)
		fixture = frappe._dict(company=company, item="AE-SMOKE-ITEM")
		source, pr = control_purchase(fixture, 60_000, None)
		target = make_test_asset(company, gross=50_000, submit=True)
		cap = frappe.get_doc(
			{"doctype": "Asset Capitalization", "transaction_type": "Capitalized Maintenance",
			 "transaction_sub_type": "Standard Maintenance", "target_asset": target.name,
			 "company": company, "posting_date": nowdate(), "posting_time": frappe.utils.nowtime(),
			 "entry_type": "Capitalization", "asset_items": [{"asset": source}]}
		)
		cap.flags.ignore_permissions = True
		cap.flags.ignore_mandatory = True
		cap.insert()
		cap.submit()
		src = frappe.get_doc("Asset", source)
		src.set_status()
		c = frappe.db.get_value("Asset", source, "status") == "Disposed" and "Disposed" in OFF_REGISTER
		print(f"fa008  a merged source keeps Disposed when core recomputes its status: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# an account of its own, so the legs cannot net against the clearing account
		expense = frappe.db.get_value("Account", {"account_name": "AE14 Post-Disposal", "company": company}, "name")
		if not expense:
			parent = frappe.db.get_value("Account", {"company": company, "root_type": "Expense", "is_group": 1}, "name")
			expense = frappe.get_doc({"doctype": "Account", "account_name": "AE14 Post-Disposal", "company": company,
				"parent_account": parent, "root_type": "Expense", "is_group": 0}).insert(ignore_permissions=True).name
		frappe.db.set_value("Company", company, "default_post_disposal_invoice_diff_account", expense,
			update_modified=False)
		before = flt(frappe.db.sql(
			"select coalesce(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0",
			expense)[0][0])
		pi = control_invoice(pr, 55_000)
		avas = frappe.db.count("Asset Value Adjustment", {"asset": source, "docstatus": 1})
		delta = flt(frappe.db.sql(
			"select coalesce(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0",
			expense)[0][0]) - before
		c = (
			avas == 0
			and flt(delta) == -5_000
			and frappe.db.get_value("Asset", source, "status") == "Disposed"
			and flt(frappe.db.get_value("Asset", source, "net_book_value")) >= 0
		)
		print(
			f"fa008  invoice 5,000 below the receipt after the merge ({pi.name}): no AVA ({avas}), "
			f"post-disposal account {delta} (want -5000), source still Disposed: {'OK' if c else 'FAIL'}"
		)
		ok = ok and c

		# ============ FA-002: asset items counted in units ================
		frappe.db.set_single_value("Asset Settings", "asset_item_uom", "Nos")
		hour = "Hour" if frappe.db.exists("UOM", "Hour") else frappe.get_doc(
			{"doctype": "UOM", "uom_name": "Hour"}).insert(ignore_permissions=True).name
		item = frappe.get_doc("Item", "AE-SMOKE-ITEM")
		item.stock_uom = hour
		c = bool(_refused(item.save))
		item.reload()
		item.append("uoms", {"uom": hour, "conversion_factor": 2})
		c = c and bool(_refused(item.save))
		print(f"fa002  an asset item held in, or also listing, another unit is refused: {'OK' if c else 'FAIL'}")
		ok = ok and c

		from asset_enterprise.asset_items import validate_purchase_rows

		receipt = frappe.get_doc(
			{"doctype": "Purchase Receipt", "company": company,
			 "items": [{"idx": 1, "item_code": "AE-SMOKE-ITEM", "is_fixed_asset": 1, "uom": hour, "stock_uom": "Nos"}]}
		)
		refusal = _refused(validate_purchase_rows, receipt)
		receipt.items[0].uom = "Nos"
		c = bool(refusal) and "Row 1" in refusal and _refused(validate_purchase_rows, receipt) is None
		print(f"fa002  a receipt row for an asset item must be in Nos: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# a receipt accepted in another unit before the rule can still be
		# billed, returned and cancelled (chief review 27/09, MUST)
		legacy_return = frappe.get_doc(
			{"doctype": "Purchase Receipt", "company": company, "is_return": 1,
			 "items": [{"idx": 1, "item_code": "AE-SMOKE-ITEM", "is_fixed_asset": 1, "uom": hour, "stock_uom": hour}]}
		)
		legacy_cancel = frappe.get_doc(
			{"doctype": "Purchase Receipt", "company": company, "is_cancellation": 1,
			 "items": [{"idx": 1, "item_code": "AE-SMOKE-ITEM", "is_fixed_asset": 1, "uom": hour, "stock_uom": hour}]}
		)
		legacy_bill = frappe.get_doc(
			{"doctype": "Purchase Invoice", "company": company,
			 "items": [{"idx": 1, "item_code": "AE-SMOKE-ITEM", "is_fixed_asset": 1, "uom": hour,
				"stock_uom": hour, "purchase_receipt": "LEGACY-PR", "pr_detail": "legacy-row"}]}
		)
		c = all(_refused(validate_purchase_rows, d) is None for d in (legacy_return, legacy_cancel, legacy_bill))
		print(f"fa002  a pre-rule receipt in another unit can still be returned, cancelled and billed: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# ============ GAP-027 sequence after CH-41/42 ======================
		h = _depreciating_asset(company, 24_000, months_back=3)
		for row in reversed([r for r in _rows(_active(h)) if r.journal_entry]):
			_reverse(row.journal_entry)
		doc = frappe.get_doc("Asset", h)
		doc.flags.ignore_permissions = True
		refusal = _refused(doc.cancel)
		c = refusal is None and frappe.db.get_value("Asset", h, "docstatus") == 2
		print(f"gap027 every period reversed latest first, then the asset cancels: {'OK' if c else 'FAIL'} ({refusal and refusal[:90]})")
		ok = ok and c

		# ============ legacy repair: reversals made before CH-41 ==========
		from asset_enterprise import repair
		from asset_enterprise.depreciation_reversal import SYSTEM_REVERSAL_FLAG
		from qcs_platform.core.journal_entry import make_reverse_journal_entry

		k = _depreciating_asset(company, 36_000)
		booked = [r for r in _rows(_active(k)) if r.journal_entry]
		# the old behaviour: a reversal the schedule never heard of, from the middle
		legacy = make_reverse_journal_entry(booked[1].journal_entry)
		legacy.posting_date = nowdate()
		legacy.flags.ignore_permissions = True
		legacy.flags[SYSTEM_REVERSAL_FLAG] = True
		legacy.insert()
		legacy.submit()
		found = repair.find_unmarked_depreciation_reversals(asset=k)
		repair.repair_unmarked_depreciation_reversals(asset=k, dry_run=0, commit=False)
		rows = _rows(_active(k))
		again = [r for r in rows if r.schedule_date == booked[1].schedule_date and not r.journal_entry]
		later_intact = all(
			any(r.journal_entry == b.journal_entry and not r.reversal_journal_entry for r in rows)
			for b in booked[2:]
		)
		reposted = post_schedule_entries(_active(k), date=nowdate())
		c = (
			len(found) == 1 and len(again) == 1
			and flt(again[0].depreciation_amount) == flt(booked[1].depreciation_amount)
			and later_intact and len(reposted) == 1
			and not repair.find_unmarked_depreciation_reversals(asset=k)
		)
		print(f"repair a middle period reversed before the fix is reinstated at its amount, later rows untouched, posts again: {'OK' if c else 'FAIL'}")
		ok = ok and bool(c)

		# ============ legacy repair: invoice adjustment on a merged source =
		from asset_enterprise import invoice_diff

		source2, pr2 = control_purchase(fixture, 40_000, None)
		target2 = make_test_asset(company, gross=10_000, submit=True)
		cap2 = frappe.get_doc(
			{"doctype": "Asset Capitalization", "transaction_type": "Capitalized Maintenance",
			 "transaction_sub_type": "Standard Maintenance", "target_asset": target2.name,
			 "company": company, "posting_date": nowdate(), "posting_time": frappe.utils.nowtime(),
			 "entry_type": "Capitalization", "asset_items": [{"asset": source2}]}
		)
		cap2.flags.ignore_permissions = True
		cap2.flags.ignore_mandatory = True
		cap2.insert()
		cap2.submit()
		fixed_list = invoice_diff.DISPOSED_STATUSES
		invoice_diff.DISPOSED_STATUSES = ("Scrapped", "Sold", "Capitalized", "Cancelled")  # the old list
		try:
			control_invoice(pr2, 37_000)
		finally:
			invoice_diff.DISPOSED_STATUSES = fixed_list
		found = repair.find_post_merge_invoice_adjustments(asset=source2)
		before = flt(frappe.db.sql(
			"select coalesce(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0",
			expense)[0][0])
		repair.repair_post_merge_invoice_adjustments(asset=source2, dry_run=0, commit=False)
		delta = flt(frappe.db.sql(
			"select coalesce(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0",
			expense)[0][0]) - before
		c = (
			len(found) == 1 and found[0].price_delta == -3_000
			and frappe.db.exists("Asset Value Adjustment", {"reversal_of_ava": found[0].name, "docstatus": 1})
			and flt(delta) == -3_000
			and flt(frappe.db.get_value("Asset", source2, "net_book_value")) == 0
			and frappe.db.get_value("Asset", source2, "status") == "Disposed"
			and not repair.find_post_merge_invoice_adjustments(asset=source2)
		)
		print(f"repair an invoice adjustment booked on a merged source is reversed and expensed ({delta}): {'OK' if c else 'FAIL'}")
		ok = ok and bool(c)

	finally:
		frappe.db.rollback(save_point="phase14_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 14:", "PASS" if ok else "FAIL")
