"""Phase 16 verification — client tickets of 29/09/2026. Run:
bench --site <site> execute asset_enterprise.setup.verify_phase16.run

  FA-011  a value change dated after the in-service date, on an asset
          with nothing posted yet, keeps the days before it: the
          schedule still starts at the in-service date and the periods
          before the change keep their amounts
  FA-012  an asset with posted depreciation carries its cancel refusal
          to the form (onload); an asset with none carries nothing
  FA-003  a Mass Depreciation Reversal dated other than today is refused
          at save with "posts today" when the user may not move the date,
          before the source-date rule (VR-022)

Savepoint-rolled-back; run on the throwaway site.
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate

from asset_enterprise.setup.test_fixtures import (
	ensure_enterprise_test_defaults,
	make_test_asset,
	pick_company,
	pick_plain_account,
)


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _active(asset):
	return frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset, "status": "Active", "docstatus": 1}, "name"
	)


def _rows(schedule):
	return frappe.db.sql(
		"""select schedule_date, days_in_period, depreciation_amount, journal_entry
		   from `tabDepreciation Schedule` where parent = %s order by idx""",
		schedule,
		as_dict=True,
	)


def _start(rows):
	return add_days(getdate(rows[0].schedule_date), -(int(rows[0].days_in_period or 1) - 1))


def _onload_refusal(asset):
	doc = frappe.get_doc("Asset", asset)
	doc.run_method("onload")
	return (doc.get_onload() or {}).get("ae_cancel_refusal")


def _run():
	ok = True
	company = pick_company()
	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	smoke_before = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
	frappe.db.savepoint("phase16_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		ensure_enterprise_test_defaults()
		from asset_enterprise.depreciation import enable_depreciation, post_schedule_entries

		# ============ FA-011: nothing posted, value change later ==========
		# the invoice-adjustment shape of ACC-ASS-2026-00064: in service
		# on the 1st, adjusted on the 15th, before the first posting (an
		# adjustment after a due period is refused by VR-043)
		in_service = get_first_day(add_months(nowdate(), -4))
		event = add_days(in_service, 14)
		asset = make_test_asset(company, gross=120_000, submit=True).name
		# bought and in service on the same day, as a receipt asset is
		frappe.db.set_value("Asset", asset, "purchase_date", in_service, update_modified=False)
		enable_depreciation(
			asset, 60, 1, depreciation_start_date=get_last_day(in_service),
			available_for_use_date=in_service,
		)
		before = _rows(_active(asset))
		ava = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": asset, "company": company,
			"date": event, "transaction_type": "Upward Revaluation",
			"current_asset_value": 120_000, "new_asset_value": 150_000,
			"difference_account": pick_plain_account(company, "Liability"),
		})
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		after = _rows(_active(asset))
		first_days = (getdate(get_last_day(in_service)) - getdate(in_service)).days + 1
		c = (
			bool(after) and _start(after) == getdate(in_service)
			and int(after[0].days_in_period) == first_days
			and flt(after[0].depreciation_amount) > flt(before[0].depreciation_amount)
		)
		print(
			f"fa011  value change {event} on an asset in service {in_service}, nothing posted: schedule "
			f"starts {after and _start(after)}, first row {after and after[0].days_in_period} days "
			f"(want {first_days}), old rate to the change and new after: {'OK' if c else 'FAIL'}"
		)
		ok = ok and c

		later = [r for r in after if getdate(r.schedule_date) > get_last_day(event)]
		old_later = {getdate(r.schedule_date): flt(r.depreciation_amount) for r in before}
		c = bool(later) and all(flt(r.depreciation_amount) > old_later.get(getdate(r.schedule_date), 0) for r in later[:3])
		print(f"fa011  periods after the change are re-priced upward: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# ============ FA-012: cancel refused before Cancel All ============
		posted = make_test_asset(company, gross=24_000, submit=True).name
		schedule = enable_depreciation(
			posted, 12, 1, depreciation_start_date=get_last_day(add_months(nowdate(), -3)),
			available_for_use_date=get_first_day(add_months(nowdate(), -3)),
		)
		post_schedule_entries(schedule, date=nowdate())
		refusal = _onload_refusal(posted)
		c = bool(refusal) and "posted depreciation" in refusal
		print(f"fa012  an asset with posted depreciation carries its cancel refusal to the form: {'OK' if c else 'FAIL'} ({refusal and refusal[:70]})")
		ok = ok and c

		free = make_test_asset(company, gross=5_000, submit=True).name
		c = _onload_refusal(free) is None
		print(f"fa012  an asset with nothing posted carries none: {'OK' if c else 'FAIL'}")
		ok = ok and c

		# ============ FA-003: reversals post today ========================
		frappe.db.delete("Asset Settings Reversal Role", {"parent": "Asset Settings", "company": company})
		march = frappe.get_doc({
			"doctype": "Mass Depreciation Reversal", "company": company, "mode": "All Eligible",
			"period_month": "March", "period_year": getdate(nowdate()).year,
			"posting_date": f"{getdate(nowdate()).year}-03-31", "reason": "phase 16",
		})
		msg = ""
		try:
			march.insert(ignore_permissions=True)
		except frappe.ValidationError as e:
			frappe.clear_last_message()
			msg = str(e)
		c = "posts today" in msg and "VR-022" not in msg
		print(f"fa003  a reversal dated 31/03 is refused at save: reversals post today: {'OK' if c else 'FAIL'} ({msg[:80]})")
		ok = ok and c

		march.posting_date = nowdate()
		march.insert(ignore_permissions=True)
		c = bool(march.name)
		print(f"fa003  dated today it saves: {'OK' if c else 'FAIL'}")
		ok = ok and c

	finally:
		frappe.db.rollback(save_point="phase16_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")}) - smoke_before
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 16:", "PASS" if ok else "FAIL")
