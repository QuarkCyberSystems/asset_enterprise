"""Daily-rate prospective depreciation engine — GA-0005-01 v2.14 §4.

Extends (never replaces) the erpnext v16 schedule model:

- Pure row math lives here and is unit-verifiable against the client's
  xlsx FA_Dep_Simulation (TC-015/016/017/019).
- Schedule supersession (GAP-031/032): reschedules mark the Active
  schedule "Superseded" via db_set — never .cancel() — and build a new
  Active schedule that PRESERVES posted rows verbatim and regenerates
  only future rows prospectively from current NBV.
- Posting (§4.6/§4.7): our post_depreciation_entries wrapper posts
  Dr Depreciation Expense (or PYA Expense for prior-FY rows) [CC] /
  Cr Accumulated Depreciation, then records the impact through
  tcc.apply(category="Depreciation").

Everything rounds through rounding.fa_module_round (§4.10); the final
row absorbs accumulated drift so NBV lands on Salvage exactly.
"""

import calendar
from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, flt, get_last_day, getdate, nowdate

from asset_enterprise.rounding import fa_module_round, final_row_amount

# --------------------------------------------------------------------------
# Pure math (xlsx-verifiable)
# --------------------------------------------------------------------------


def daily_rate(depreciable_base, remaining_days):
	"""Unrounded daily rate. Row amounts round; the rate itself never does."""
	if remaining_days <= 0:
		frappe.throw(_("Remaining useful life is zero — cannot compute daily rate."))
	return flt(depreciable_base) / remaining_days


def build_daily_rate_rows(
	depreciable_base,
	start_date,
	total_days,
	company,
	first_posting_date=None,
):
	"""Generate EOM schedule rows for `depreciable_base` over `total_days`
	starting `start_date` (the depreciation-start basis, §4.4).

	- Rows fall on month-ends (§4.6 EOM).
	- First row runs basis -> first EOM (catch-up §4.5: if
	  first_posting_date is later, the first row accumulates every day
	  from basis to that posting month's EOM in ONE entry).
	- Final row = base − sum(prior rows): §4.10 absorption.

	Returns list of dicts: schedule_date, days_in_period, daily_rate,
	amount.
	"""
	start = getdate(start_date)
	end_of_life = add_days(start, total_days - 1)
	rate = daily_rate(depreciable_base, total_days)

	# First schedule date: EOM of the basis month, or of the catch-up
	# posting month when supplied.
	first_eom = get_last_day(getdate(first_posting_date) if first_posting_date else start)
	if first_eom > end_of_life:
		first_eom = end_of_life

	rows = []
	prev = add_days(start, -1)  # so first days_in_period includes the basis day
	cursor = first_eom
	posted_total = 0.0

	while cursor < end_of_life:
		days = date_diff(cursor, prev)
		amount = fa_module_round(rate * days, company)
		rows.append(
			{
				"schedule_date": cursor,
				"days_in_period": days,
				"daily_rate": rate,
				"amount": amount,
			}
		)
		posted_total += amount
		prev = cursor
		cursor = get_last_day(add_days(cursor, 1))
		if cursor > end_of_life:
			cursor = end_of_life

	# Final row absorbs drift (§4.10 point 3).
	days = date_diff(end_of_life, prev)
	if days > 0 or not rows:
		rows.append(
			{
				"schedule_date": end_of_life,
				"days_in_period": days,
				"daily_rate": rate,
				"amount": final_row_amount(depreciable_base, posted_total, company),
			}
		)
	else:
		rows[-1]["amount"] = final_row_amount(
			depreciable_base, posted_total - rows[-1]["amount"], company
		)
	return rows


def build_prospective_rows(nbv_base, as_of_date, end_of_life_date, company):
	"""Post-adjustment regeneration (§4.3): current NBV over remaining
	days from the day AFTER as_of_date to end of life. Never touches the
	past."""
	start = add_days(getdate(as_of_date), 1)
	total_days = date_diff(getdate(end_of_life_date), start) + 1
	return build_daily_rate_rows(nbv_base, start, total_days, company)


def is_prior_fiscal_year(schedule_date, posting_date):
	"""§4.7 PYA: a row belongs to Prior Year Adjustment when its schedule
	date falls in an earlier fiscal year than the posting date."""
	try:
		from erpnext.accounts.utils import get_fiscal_year

		sched_fy = get_fiscal_year(getdate(schedule_date), as_dict=True)
		post_fy = get_fiscal_year(getdate(posting_date), as_dict=True)
		return sched_fy.name != post_fy.name and getdate(sched_fy.year_end_date) < getdate(
			post_fy.year_start_date
		)
	except Exception:
		return False


def split_period_for_cc_change(row_amount, days_in_period, change_day_offset, company):
	"""GAP-021 primitive: split one period's amount across two cost
	centers at `change_day_offset` days into the period. Returns
	(old_cc_amount, new_cc_amount); the pair sums to row_amount exactly."""
	old_part = fa_module_round(row_amount * change_day_offset / days_in_period, company)
	return old_part, fa_module_round(row_amount - old_part, company)


# --------------------------------------------------------------------------
# Schedule supersession (GAP-031 / GAP-032)
# --------------------------------------------------------------------------


def supersede_and_regenerate(asset_name, finance_book=None, as_of_date=None, reason=None):
	"""Replace reschedule-by-cancel with supersession.

	1. Active schedule -> status "Superseded" (db_set; docstatus stays 1;
	   linked JEs untouched).
	2. New schedule: posted rows copied verbatim (GAP-032), future rows
	   regenerated prospectively from current NBV over remaining days.
	3. New schedule becomes the one core lookups find (they filter
	   status='Active').

	Returns the new Asset Depreciation Schedule doc.
	"""
	as_of_date = getdate(as_of_date or nowdate())

	filters = {"asset": asset_name, "status": "Active", "docstatus": 1}
	if finance_book:
		filters["finance_book"] = finance_book
	old_name = frappe.db.get_value("Asset Depreciation Schedule", filters, "name")
	if not old_name:
		frappe.throw(
			_("No Active depreciation schedule found for {0} — nothing to supersede.").format(
				asset_name
			)
		)
	old = frappe.get_doc("Asset Depreciation Schedule", old_name)

	asset = frappe.get_doc("Asset", asset_name)
	company = asset.company

	posted = [r for r in old.get("depreciation_schedule") if r.journal_entry]
	unposted = [r for r in old.get("depreciation_schedule") if not r.journal_entry]

	# Remaining life: end of life comes from the OLD schedule's horizon.
	if not old.get("depreciation_schedule"):
		frappe.throw(_("Schedule {0} has no rows.").format(old_name))
	end_of_life = getdate(old.get("depreciation_schedule")[-1].schedule_date)

	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset_name, save=False)
	nbv_base = values["net_book_value"]

	future_rows = (
		build_prospective_rows(nbv_base, as_of_date, end_of_life, company)
		if getdate(end_of_life) > as_of_date and nbv_base > 0
		else []
	)

	new = frappe.copy_doc(old)
	new.status = "Active"
	new.supersedes = old.name
	new.superseded_on = as_of_date
	new.set("depreciation_schedule", [])
	accumulated = 0.0
	for r in posted:
		accumulated = flt(accumulated + flt(r.depreciation_amount))
		new.append(
			"depreciation_schedule",
			{
				"schedule_date": r.schedule_date,
				"depreciation_amount": r.depreciation_amount,
				"accumulated_depreciation_amount": r.accumulated_depreciation_amount,
				"journal_entry": r.journal_entry,
				"cost_center": r.get("cost_center"),
				"is_pya_entry": r.get("is_pya_entry"),
				"days_in_period": r.get("days_in_period"),
				"daily_rate": r.get("daily_rate"),
			},
		)
	for row in future_rows:
		accumulated = flt(accumulated + row["amount"])
		new.append(
			"depreciation_schedule",
			{
				"schedule_date": row["schedule_date"],
				"depreciation_amount": row["amount"],
				"accumulated_depreciation_amount": fa_module_round(accumulated, company),
				"days_in_period": row["days_in_period"],
				"daily_rate": row["daily_rate"],
			},
		)

	# Supersede FIRST so any one-Active-per-asset validation on the new
	# doc sees no competing Active schedule. db_set only — never cancel.
	old.db_set("status", "Superseded", update_modified=False)

	new.flags.ignore_permissions = True
	new.insert()
	new.submit()

	if reason:
		old.add_comment("Comment", _("Superseded by {0}: {1}").format(new.name, reason))

	# Drop now-orphaned unposted rows context for audit trail.
	if unposted:
		old.add_comment(
			"Comment",
			_("{0} unposted rows regenerated prospectively in {1}.").format(len(unposted), new.name),
		)
	return new


# --------------------------------------------------------------------------
# Posting (wrapper target for patches.py — Phase 3 wiring)
# --------------------------------------------------------------------------


def enterprise_enabled():
	try:
		return bool(
			frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=True)
		)
	except Exception:
		return False


def post_depreciation_entries(date=None):
	"""Replacement for erpnext post_depreciation_entries when the master
	switch is ON: posts due rows from Active schedules with PYA routing
	and CC-aware JEs, recording each through the TCC."""
	posting_date = getdate(date or nowdate())
	due = frappe.db.sql(
		"""
		select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
		       ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') = ''
		  and ds.schedule_date <= %s
		order by ads.asset, ds.schedule_date
		""",
		posting_date,
		as_dict=True,
	)
	for row in due:
		try:
			_post_one(row, posting_date)
		except Exception:
			frappe.log_error(
				title=f"asset_enterprise depreciation posting failed: {row.asset}",
				message=frappe.get_traceback(),
			)
			frappe.db.rollback()
		else:
			frappe.db.commit()


def _post_one(row, posting_date):
	from asset_enterprise import tcc
	from asset_enterprise.accounts import get_enterprise_account

	asset = frappe.get_doc("Asset", row.asset)
	company = asset.company
	aca = frappe.db.get_value(
		"Asset Category Account",
		{"parent": asset.asset_category, "company_name": company},
		["depreciation_expense_account", "accumulated_depreciation_account"],
		as_dict=True,
	)
	if not aca:
		frappe.throw(
			_("Asset Category Account missing for {0} / {1}").format(asset.asset_category, company)
		)

	pya = is_prior_fiscal_year(row.schedule_date, posting_date)
	debit_account = (
		get_enterprise_account("pya_expense_account", company, asset.asset_category)
		if pya
		else aca.depreciation_expense_account
	)
	cost_center = row.cost_center or asset.cost_center

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Depreciation Entry",
			"company": company,
			"posting_date": posting_date,
			"accounts": [
				{
					"account": debit_account,
					"debit_in_account_currency": row.depreciation_amount,
					"cost_center": cost_center,
					"reference_type": "Asset",
					"reference_name": asset.name,
				},
				{
					"account": aca.accumulated_depreciation_account,
					"credit_in_account_currency": row.depreciation_amount,
					"reference_type": "Asset",
					"reference_name": asset.name,
				},
			],
		}
	)
	je.flags.ignore_permissions = True
	je.submit()

	frappe.db.set_value(
		"Depreciation Schedule",
		row.row_name,
		{"journal_entry": je.name, "is_pya_entry": 1 if pya else 0},
		update_modified=False,
	)

	tcc.apply(
		source_doc=("Asset Depreciation Schedule", row.schedule),
		category="Depreciation",
		transaction_type="PYA Depreciation" if pya else "Depreciation",
		asset=asset.name,
		posting_date=posting_date,
		amount=row.depreciation_amount,
		accum_delta=row.depreciation_amount,
		journal_entry=je.name,
	)
