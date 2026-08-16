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
from frappe.utils import add_days, add_months, cint, date_diff, flt, get_last_day, getdate, nowdate

from asset_enterprise.rounding import fa_module_round, final_row_amount

# --------------------------------------------------------------------------
# Pure math (xlsx-verifiable)
# --------------------------------------------------------------------------


def daily_rate(depreciable_base, remaining_days):
	"""Unrounded daily rate. Row amounts round; the rate itself never does."""
	if remaining_days <= 0:
		frappe.throw(_("Remaining useful life is zero — cannot compute daily rate."))
	return flt(depreciable_base) / remaining_days


def day_count_365(start_date, end_date):
	"""§4.3 day-count rule (v2.16 CH-12, finance formula 23/07/2026):
	inclusive calendar days minus any 29 February in the span — equals
	months ÷ 12 × 365 for whole-month spans, matching the client xlsx
	daily-rate DENOMINATOR. Row allocation keeps actual calendar days;
	the final row absorbs the leap-day difference."""
	start, end = getdate(start_date), getdate(end_date)
	days = date_diff(end, start) + 1
	leap_days = 0
	for year in range(start.year, end.year + 1):
		if calendar.isleap(year):
			feb29 = date(year, 2, 29)
			if start <= feb29 <= end:
				leap_days += 1
	return days - leap_days


def build_daily_rate_rows(
	depreciable_base,
	start_date,
	total_days,
	company,
	first_posting_date=None,
	rate_days=None,
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
	# §4.3 (v2.16): the rate DENOMINATOR may differ from the row span
	# (365-day-year basis excludes leap days); rows still lay out over
	# the real calendar and the final row absorbs the difference.
	rate = daily_rate(depreciable_base, rate_days or total_days)

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


def build_prospective_rows(nbv_base, as_of_date, end_of_life_date, company, first_posting_date=None):
	"""Post-adjustment regeneration (§4.3): current NBV over remaining
	days from the day AFTER as_of_date to end of life. Never touches the
	past. Rate denominator follows the §4.3 365-day rule (v2.16)."""
	start = add_days(getdate(as_of_date), 1)
	end = getdate(end_of_life_date)
	total_days = date_diff(end, start) + 1
	return build_daily_rate_rows(
		nbv_base,
		start,
		total_days,
		company,
		first_posting_date=first_posting_date,
		rate_days=day_count_365(start, end),
	)


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


def supersede_and_regenerate(
	asset_name,
	finance_book=None,
	as_of_date=None,
	reason=None,
	end_of_life_override=None,
	first_posting_date=None,
):
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
	# Reversed rows (F6) still count as "posted" for verbatim copying —
	# their reversal_journal_entry flag must survive every generation.

	# Remaining life: the OLD schedule's horizon, unless explicitly
	# extended (GAP-014 "Add Value and Extend Life") or supplied by the
	# caller (Path 3 restore regenerates a post-scrap EMPTY schedule
	# from the pre-disposal horizon).
	if not old.get("depreciation_schedule") and not end_of_life_override:
		frappe.throw(_("Schedule {0} has no rows.").format(old_name))
	end_of_life = getdate(
		end_of_life_override
		or old.get("depreciation_schedule")[-1].schedule_date
	)

	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset_name, save=False)
	# §4.3 (Phase 11 F1): salvage is subtracted from the depreciable
	# base in EVERY rate computation — the regenerated schedule must
	# land NBV on salvage, not zero.
	fb_filters = {"parent": asset_name}
	if finance_book:
		fb_filters["finance_book"] = finance_book
	salvage = flt(
		frappe.db.get_value("Asset Finance Book", fb_filters, "expected_value_after_useful_life")
		or 0
	)
	nbv_base = fa_module_round(flt(values["net_book_value"]) - salvage, company)

	future_rows = (
		# first_posting_date (v2.16 Path 3): the first regenerated row
		# accumulates every day from as_of to that posting month's EOM in
		# ONE catch-up entry (§4.5) — e.g. a September restore of a July
		# disposal posts 3 months in September.
		build_prospective_rows(
			nbv_base, as_of_date, end_of_life, company, first_posting_date=first_posting_date
		)
		if getdate(end_of_life) > as_of_date and nbv_base > 0
		else []
	)

	# A schedule that never posted anything is not history — it is a
	# working copy (core builds one at Asset submit that our §4.3
	# rebuild replaces milliseconds later). Drop it instead of leaving
	# a Superseded row, so supersession always means a real event.
	drop_old = not posted and not any(
		r.get("reversal_journal_entry") for r in old.get("depreciation_schedule")
	)

	new = frappe.copy_doc(old)
	new.status = "Active"
	new.supersedes = old.get("supersedes") if drop_old else old.name
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
				"reversal_journal_entry": r.get("reversal_journal_entry"),
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

	if drop_old:
		delete_unposted_schedule(old.name)
		if reason:
			new.add_comment(
				"Comment",
				_("Replaced an unposted schedule (nothing had been booked): {0}").format(reason),
			)
		return new

	if reason:
		old.add_comment("Comment", _("Superseded by {0}: {1}").format(new.name, reason))

	# Drop now-orphaned unposted rows context for audit trail.
	if unposted:
		old.add_comment(
			"Comment",
			_("{0} unposted rows regenerated prospectively in {1}.").format(len(unposted), new.name),
		)
	return new


def delete_unposted_schedule(schedule_name):
	"""Remove a depreciation schedule that never booked an entry. The
	doc is submitted, so drop its docstatus first — there is nothing to
	reverse, which is exactly why it qualifies."""
	frappe.db.set_value(
		"Asset Depreciation Schedule", schedule_name, "docstatus", 2, update_modified=False
	)
	frappe.delete_doc(
		"Asset Depreciation Schedule",
		schedule_name,
		force=1,
		ignore_permissions=True,
		delete_permanently=True,
	)


def schedule_horizon_from_life(asset_name, finance_book=None):
	"""§4.3/§4.4 horizon: start basis + total life − 1 day."""
	asset = frappe.get_doc("Asset", asset_name)
	filters = {"parent": asset_name}
	if finance_book:
		filters["finance_book"] = finance_book
	fb = frappe.db.get_value(
		"Asset Finance Book", filters,
		["total_number_of_depreciations", "frequency_of_depreciation", "depreciation_start_date"],
		as_dict=True,
	)
	if not fb or not fb.total_number_of_depreciations:
		return None
	months = cint(fb.total_number_of_depreciations) * (cint(fb.frequency_of_depreciation) or 1)
	start = getdate(asset.available_for_use_date or fb.depreciation_start_date)
	return add_days(add_months(start, months), -1)


def is_rule_built_schedule(asset_name):
	"""Our engine stamps daily_rate/days_in_period on every row; core
	leaves them empty. Used to avoid rebuilding what we already built."""
	row = frappe.db.sql(
		"""select ds.daily_rate from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		   order by ds.schedule_date limit 1""",
		asset_name,
	)
	return bool(row and flt(row[0][0]))


def apply_daycount_rule(asset_name, reason=None, finance_book=None):
	"""Rebuild the Active schedule under the §4.3 day-count rule so the
	daily rate is UNIFORM across the whole life (core spreads the initial
	schedule over the real calendar span, giving a different rate inside
	a leap year). Posted rows are preserved verbatim; only future rows
	are regenerated, so this is safe on assets already depreciating."""
	if is_rule_built_schedule(asset_name):
		return None
	end_of_life = schedule_horizon_from_life(asset_name, finance_book)
	if not end_of_life:
		return None
	last_posted = frappe.db.sql(
		"""select max(ds.schedule_date) from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		     and ifnull(ds.journal_entry, '') != ''""",
		asset_name,
	)[0][0]
	asset = frappe.get_doc("Asset", asset_name)
	# nothing posted -> regenerate from the start basis itself
	as_of = getdate(last_posted) if last_posted else add_days(
		getdate(asset.available_for_use_date), -1
	)
	if getdate(end_of_life) <= as_of:
		return None

	# §4.5 first-posting catch-up: when the first POSTING date is later
	# than the month end of the calculation basis, the first entry
	# accumulates every period in between (TC-019).
	first_posting = None
	if not last_posted:
		fb_filters = {"parent": asset_name}
		if finance_book:
			fb_filters["finance_book"] = finance_book
		fb_start = frappe.db.get_value(
			"Asset Finance Book", fb_filters, "depreciation_start_date"
		)
		basis = getdate(asset.available_for_use_date)
		if fb_start and getdate(fb_start) > get_last_day(basis):
			first_posting = getdate(fb_start)

	try:
		return supersede_and_regenerate(
			asset_name,
			finance_book=finance_book,
			as_of_date=as_of,
			end_of_life_override=end_of_life,
			first_posting_date=first_posting,
			reason=reason or _("§4.3 day-count rule applied"),
		)
	except frappe.ValidationError:
		return None


def regenerate_after_value_change(asset_name, adjustment_date, reason, end_of_life_override=None):
	"""Supersede + regenerate once the value change is recorded.

	`as_of` is never earlier than the last POSTED schedule date, so an
	already-posted period is not regenerated (which would duplicate it).
	"""
	last_posted = frappe.db.sql(
		"""
		select max(ds.schedule_date) from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') != ''
		""",
		asset_name,
	)[0][0]
	as_of = getdate(adjustment_date or nowdate())
	if last_posted and getdate(last_posted) > as_of:
		as_of = getdate(last_posted)
	try:
		return supersede_and_regenerate(
			asset_name,
			as_of_date=as_of,
			reason=reason,
			end_of_life_override=end_of_life_override,
		)
	except frappe.ValidationError:
		return None  # no Active schedule (non-depreciating asset)


def active_schedule_horizon(asset_name):
	"""Last schedule date of the Active generation (the end of life)."""
	return frappe.db.sql(
		"""
		select max(ds.schedule_date)
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		""",
		asset_name,
	)[0][0]


def depreciate_remaining_base_now(asset_name, posting_date, source_doc, transaction_type):
	"""GAP-013 / VR-018 (Phase 11 F2): when an adjustment drives RUL to
	zero, the full remaining depreciable base (NBV − salvage) posts as
	one immediate depreciation entry on the adjustment date."""
	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values

	asset = frappe.get_doc("Asset", asset_name)
	values = recalculate_asset_values(asset_name, save=False)
	salvage = flt(
		frappe.db.get_value(
			"Asset Finance Book", {"parent": asset_name}, "expected_value_after_useful_life"
		)
		or 0
	)
	base = fa_module_round(flt(values["net_book_value"]) - salvage, asset.company)
	if base <= 0:
		return None

	aca = frappe.db.get_value(
		"Asset Category Account",
		{"parent": asset.asset_category, "company_name": asset.company},
		["depreciation_expense_account", "accumulated_depreciation_account"],
		as_dict=True,
	)
	if not aca:
		frappe.throw(
			_("Asset Category Account missing for {0} / {1}").format(asset.asset_category, asset.company)
		)
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Depreciation Entry",
			"company": asset.company,
			"posting_date": posting_date,
			"user_remark": _("Immediate depreciation — remaining useful life exhausted ({0})").format(
				transaction_type
			),
			"accounts": [
				{
					"account": aca.depreciation_expense_account,
					"debit_in_account_currency": base,
					"cost_center": asset.get("cost_center"),
					"reference_type": "Asset",
					"reference_name": asset.name,
				},
				{
					"account": aca.accumulated_depreciation_account,
					"credit_in_account_currency": base,
					"reference_type": "Asset",
					"reference_name": asset.name,
				},
			],
		}
	)
	je.flags.ignore_permissions = True
	je.submit()

	tcc.apply(
		source_doc=source_doc,
		category="Depreciation",
		transaction_type=transaction_type,
		asset=asset_name,
		posting_date=posting_date,
		amount=base,
		accum_delta=base,
		journal_entry=je.name,
	)
	return je.name


# --------------------------------------------------------------------------
# Enable depreciation after creation (GAP-011)
# --------------------------------------------------------------------------


@frappe.whitelist()
def enable_depreciation(
	asset_name,
	total_number_of_depreciations,
	frequency_of_depreciation=1,
	depreciation_start_date=None,
	expected_value_after_useful_life=0,
	finance_book=None,
	depreciation_method="Straight Line",
):
	"""GAP-011: amendment-free enablement on a submitted asset.

	Core clears finance_books when calculate_depreciation=0 and blocks
	re-enabling without amendment. Here: flag on + Asset Finance Book row
	+ a new Active schedule built prospectively from current NBV over the
	chosen life via the daily-rate engine. Nothing posted changes.
	"""
	if not enterprise_enabled():
		frappe.throw(_("Enable Depreciation requires Enterprise Assets to be enabled."))
	frappe.has_permission("Asset", "write", asset_name, throw=True)

	asset = frappe.get_doc("Asset", asset_name)
	if asset.docstatus != 1:
		frappe.throw(_("Asset {0} must be submitted.").format(asset_name))
	if asset.calculate_depreciation:
		frappe.throw(_("Depreciation is already enabled on {0}.").format(asset_name))
	if asset.status in ("Sold", "Scrapped", "Capitalized"):
		frappe.throw(_("Asset {0} is {1} — cannot enable depreciation.").format(asset_name, asset.status))
	if frappe.db.exists(
		"Asset Depreciation Schedule", {"asset": asset_name, "status": "Active", "docstatus": 1}
	):
		frappe.throw(_("Asset {0} already has an Active depreciation schedule.").format(asset_name))

	months = cint(total_number_of_depreciations) * (cint(frequency_of_depreciation) or 1)
	if months <= 0:
		frappe.throw(_("Total useful life must be positive."))

	start = getdate(depreciation_start_date or nowdate())
	from asset_enterprise.asset_values import recalculate_asset_values

	nbv = flt(recalculate_asset_values(asset_name, save=False)["net_book_value"])
	base = fa_module_round(nbv - flt(expected_value_after_useful_life), asset.company)
	if base <= 0:
		frappe.throw(
			_("Depreciable base is {0} — NBV must exceed the salvage value.").format(base)
		)

	end_of_life = add_days(add_months(start, months), -1)
	total_days = date_diff(end_of_life, start) + 1
	rows = build_daily_rate_rows(
		base, start, total_days, asset.company,
		rate_days=day_count_365(start, end_of_life),  # §4.3 v2.16 rule
	)

	fb_row = frappe.get_doc(
		{
			"doctype": "Asset Finance Book",
			"parent": asset.name,
			"parenttype": "Asset",
			"parentfield": "finance_books",
			"idx": 1,
			"docstatus": 1,
			"finance_book": finance_book,
			"depreciation_method": depreciation_method,
			"total_number_of_depreciations": cint(total_number_of_depreciations),
			"frequency_of_depreciation": cint(frequency_of_depreciation) or 1,
			"depreciation_start_date": rows[0]["schedule_date"],
			"expected_value_after_useful_life": flt(expected_value_after_useful_life),
			"value_after_depreciation": base,
			"daily_prorata_based": 1,
		}
	)
	fb_row.flags.ignore_permissions = True
	fb_row.db_insert()

	asset.db_set("calculate_depreciation", 1, update_modified=False)
	if not asset.available_for_use_date:
		asset.db_set("available_for_use_date", start, update_modified=False)

	ads = frappe.get_doc(
		{
			"doctype": "Asset Depreciation Schedule",
			"asset": asset.name,
			"company": asset.company,
			"finance_book": finance_book,
			"depreciation_method": depreciation_method,
			"total_number_of_depreciations": cint(total_number_of_depreciations),
			"frequency_of_depreciation": cint(frequency_of_depreciation) or 1,
			"expected_value_after_useful_life": flt(expected_value_after_useful_life),
			"daily_prorata_based": 1,
			# Set so core ADS validate does NOT regenerate our rows.
			"finance_book_id": fb_row.idx,
			"notes": _("Created by Enable Depreciation (GAP-011) starting {0}.").format(start),
		}
	)
	accumulated = 0.0
	for row in rows:
		accumulated = flt(accumulated + row["amount"])
		ads.append(
			"depreciation_schedule",
			{
				"schedule_date": row["schedule_date"],
				"depreciation_amount": row["amount"],
				"accumulated_depreciation_amount": fa_module_round(accumulated, asset.company),
				"days_in_period": row["days_in_period"],
				"daily_rate": row["daily_rate"],
			},
		)
	ads.flags.ignore_permissions = True
	ads.insert()
	ads.submit()  # core on_submit sets status Active

	asset.add_comment(
		"Comment",
		_("Depreciation enabled: {0} months from {1}, schedule {2} (GAP-011).").format(
			months, start, ads.name
		),
	)
	recalculate_asset_values(asset_name, save=True)
	return ads.name


# --------------------------------------------------------------------------
# Posting (wrapper target for patches.py — Phase 3 wiring)
# --------------------------------------------------------------------------


def enterprise_enabled():
	try:
		return bool(
			frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		)
	except Exception:
		return False


def post_schedule_entries(schedule_name, date=None, sch_start_idx=None, sch_end_idx=None):
	"""Replacement for core make_depreciation_entry — the button on the
	Asset Depreciation Schedule form. Core's version posts a plain
	depreciation JE, skipping the §4.7 prior-year split, the cost-centre
	routing and the Financial Treatment record, so the same asset posted
	differently depending on which button was used."""
	posting_date = getdate(date or nowdate())
	rows = frappe.db.sql(
		"""
		select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
		       ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
		       ds.daily_rate, ds.days_in_period, ds.idx
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ds.parent = %s and ifnull(ds.journal_entry, '') = ''
		  and ds.schedule_date <= %s
		order by ds.schedule_date
		""",
		(schedule_name, posting_date),
		as_dict=True,
	)
	posted = []
	for row in rows:
		if sch_start_idx and row.idx < cint(sch_start_idx):
			continue
		if sch_end_idx and row.idx > cint(sch_end_idx):
			continue
		if final_row_requires_manual_post(row):
			continue
		_post_one(row, posting_date)
		posted.append(row.row_name)
	return posted


def post_depreciation_entries(date=None):
	"""Replacement for erpnext post_depreciation_entries when the master
	switch is ON: posts due rows from Active schedules with PYA routing
	and CC-aware JEs, recording each through the TCC."""
	posting_date = getdate(date or nowdate())
	due = frappe.db.sql(
		"""
		select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
		       ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
		       ds.daily_rate, ds.days_in_period
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
		if final_row_requires_manual_post(row):
			# §4.10 point 4 (v2.16 CH-01): beyond-tolerance final row is
			# never auto-posted — manual posting with approval required.
			continue
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


def final_row_requires_manual_post(row):
	"""§4.10 point 4 (v2.16 CH-01): True when `row` is the FINAL row of
	its schedule and its absorbed drift exceeds the company tolerance —
	such a row must be posted manually (post_final_row), optionally
	overriding the tolerance with Tolerance Approver approval."""
	last = frappe.db.sql(
		"select max(schedule_date) from `tabDepreciation Schedule` where parent = %s",
		row.schedule,
	)[0][0]
	if not last or getdate(row.schedule_date) != getdate(last):
		return False
	if not flt(row.get("daily_rate")) or not flt(row.get("days_in_period")):
		return False  # core-generated rows carry no drift metadata

	from asset_enterprise.accounts import get_last_period_tolerance

	company = frappe.db.get_value("Asset", row.asset, "company")
	nominal = fa_module_round(flt(row.daily_rate) * flt(row.days_in_period), company)
	drift = abs(flt(row.depreciation_amount) - nominal)
	return drift > flt(get_last_period_tolerance(company))


@frappe.whitelist()
def post_final_row(asset_name, override_tolerance=0):
	"""Manual posting of the final schedule row (§4.10 point 4, v2.16).

	Within tolerance: posts like any row. Beyond tolerance: requires
	override_tolerance=1 AND the session user holding the company's
	Tolerance Approver role from Asset Settings."""
	from frappe.utils import cint

	if not enterprise_enabled():
		frappe.throw(_("Enterprise Assets is not enabled."))
	frappe.has_permission("Asset", "write", asset_name, throw=True)

	row = frappe.db.sql(
		"""
		select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
		       ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
		       ds.daily_rate, ds.days_in_period
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') = ''
		order by ds.schedule_date desc
		limit 1
		""",
		asset_name,
		as_dict=True,
	)
	if not row:
		frappe.throw(_("No unposted schedule row found for {0}.").format(asset_name))
	row = row[0]

	if final_row_requires_manual_post(row):
		if not cint(override_tolerance):
			frappe.throw(
				_(
					"Final-row drift on {0} exceeds the company tolerance. Posting "
					"requires the tolerance override (with Tolerance Approver "
					"approval) — §4.10 point 4."
				).format(asset_name),
				title=_("Tolerance Exceeded"),
			)
		company = frappe.db.get_value("Asset", asset_name, "company")
		approver = frappe.db.get_value(
			"Asset Settings Tolerance",
			{"parent": "Asset Settings", "company": company},
			"tolerance_approver",
		)
		if approver and approver not in frappe.get_roles():
			frappe.throw(
				_(
					"Overriding the tolerance requires the '{0}' role (Tolerance "
					"Approver, Asset Settings)."
				).format(approver),
				title=_("Approval Required"),
			)

	_post_one(row, getdate(nowdate()))
	return frappe.db.get_value("Depreciation Schedule", row.row_name, "journal_entry")


def _split_row_by_fiscal_year(row, posting_date, company):
	"""(prior_year_amount, current_year_amount) for one schedule row.

	A normal row sits wholly in one fiscal year. A §4.5 catch-up row can
	span several, so its days are apportioned: days falling in a fiscal
	year earlier than the posting year go to Prior Year Adjustment."""
	amount = flt(row.depreciation_amount)
	days = cint(row.get("days_in_period") or 0)
	end = getdate(row.schedule_date)
	if days <= 1:
		return (amount, 0.0) if is_prior_fiscal_year(end, posting_date) else (0.0, amount)

	start = add_days(end, -(days - 1))
	prior_days = 0
	cursor = start
	while cursor <= end:
		if is_prior_fiscal_year(cursor, posting_date):
			prior_days += 1
		cursor = add_days(cursor, 1)
	if not prior_days:
		return 0.0, amount
	if prior_days >= days:
		return amount, 0.0
	prior_amount = fa_module_round(amount * prior_days / days, company)
	return prior_amount, fa_module_round(amount - prior_amount, company)


def cost_center_segments(asset, row):
	"""GAP-021 / TC-035: a cost-centre change part-way through a period
	splits that period's debit between the two centres by days. The
	primitive existed but nothing called it, so the whole period landed
	on whichever centre the asset happened to carry at posting time."""
	end = getdate(row.schedule_date)
	days = cint(row.days_in_period) or (date_diff(end, end) + 1)
	start = add_days(end, -(days - 1))
	moves = frappe.db.sql(
		"""select am.transaction_date, ami.target_cost_center, ami.source_cost_center
		   from `tabAsset Movement Item` ami
		   join `tabAsset Movement` am on am.name = ami.parent
		   where ami.asset = %s and am.docstatus = 1
		     and ifnull(ami.target_cost_center, '') != ''
		     and am.transaction_date > %s and am.transaction_date <= %s
		   order by am.transaction_date""",
		(row.asset, start, end),
		as_dict=True,
	)
	fallback = row.cost_center or asset.cost_center
	if not moves:
		return [(fallback, flt(row.depreciation_amount))]

	company = asset.company
	boundaries = []
	current_cc = moves[0].source_cost_center or fallback
	cursor = start
	for move in moves:
		change = getdate(move.transaction_date)
		segment_days = date_diff(change, cursor)
		if segment_days > 0:
			boundaries.append((current_cc, segment_days))
		current_cc = move.target_cost_center
		cursor = change
	remaining = date_diff(end, cursor) + 1
	if remaining > 0:
		boundaries.append((current_cc, remaining))

	total_days = sum(d for _cc, d in boundaries) or days
	amount = flt(row.depreciation_amount)
	out, allocated = [], 0.0
	for idx, (cc, seg_days) in enumerate(boundaries):
		if idx == len(boundaries) - 1:
			part = fa_module_round(amount - allocated, company)
		else:
			part = fa_module_round(amount * seg_days / total_days, company)
		allocated = flt(allocated + part)
		out.append((cc, part))
	return [(cc, part) for cc, part in out if flt(part)]


def _depreciation_legs(asset, aca, row, pya_account, pya_amount, current_amount, segments, cost_center):
	"""One debit per cost centre the asset sat in during the period
	(GAP-021 / TC-035), plus the prior-year debit when the period spans
	fiscal years (§4.7), against a single accumulated-depreciation credit."""
	legs = []
	if flt(pya_amount):
		legs.append(
			{
				"account": pya_account,
				"debit_in_account_currency": flt(pya_amount),
				"cost_center": segments[0][0] if segments else cost_center,
				"reference_type": "Asset",
				"reference_name": asset.name,
			}
		)
	if flt(current_amount):
		# Spread the current-year portion across the period's cost centres
		# in the same proportion the segmentation produced.
		total = flt(row.depreciation_amount) or flt(current_amount)
		allocated = 0.0
		usable = segments or [(cost_center, flt(current_amount))]
		for idx, (cc, part) in enumerate(usable):
			if idx == len(usable) - 1:
				amount = fa_module_round(flt(current_amount) - allocated, asset.company)
			else:
				amount = fa_module_round(flt(current_amount) * flt(part) / total, asset.company)
			allocated = flt(allocated + amount)
			if flt(amount):
				legs.append(
					{
						"account": aca.depreciation_expense_account,
						"debit_in_account_currency": amount,
						"cost_center": cc,
						"reference_type": "Asset",
						"reference_name": asset.name,
					}
				)
	legs.append(
		{
			"account": aca.accumulated_depreciation_account,
			"credit_in_account_currency": row.depreciation_amount,
			"reference_type": "Asset",
			"reference_name": asset.name,
		}
	)
	return legs


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

	cost_center = row.cost_center or asset.cost_center
	segments = cost_center_segments(asset, row)
	pya_account = None
	# §4.7: split a row that SPANS fiscal years into a prior-year and a
	# current-year debit inside ONE entry (catch-up rows can span years).
	pya_amount, current_amount = _split_row_by_fiscal_year(row, posting_date, company)
	if flt(pya_amount):
		pya_account = get_enterprise_account(
			"pya_expense_account", company, asset.asset_category
		)
	pya = bool(flt(pya_amount)) and not flt(current_amount)
	debit_account = pya_account if pya else aca.depreciation_expense_account

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Depreciation Entry",
			"company": company,
			"posting_date": posting_date,
			"accounts": _depreciation_legs(
				asset, aca, row, pya_account, pya_amount, current_amount, segments, cost_center
			),
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
