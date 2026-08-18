"""Ledger-derived asset values — GA-0005-01 v2.14 GAP-006 / §5.1.

The Asset form's Enterprise tab shows values DERIVED, not stored-and-
mutated:

    HAV   = net_purchase_amount  (v16 field; v15 gross_purchase_amount)
            + sum(Posted Financial Treatment.hav_delta)
    Accum = opening_accumulated_depreciation
            + sum(posted Depreciation Schedule row amounts, all books)
            + sum(Posted Financial Treatment.accum_delta)
    NBV   = HAV − Accum
    RUL   = total useful life − elapsed since depreciation start
            + sum(Posted Financial Treatment.life_delta_months)      (C33)

Financial Treatments carry SIGNED deltas set by the TCC handler that
created them, so this module never re-derives category semantics — it
just folds. Reversal pairs contribute nothing: the original flips to
status Reversed (excluded by status) and the mirror FT carries
reversal_reference back to it (excluded by that reference), so the
pair nets out of the fold while both remain visible for audit.

Recalculation triggers: after every tcc.apply / tcc.reverse, and the
manual Recalculate button (Phase 8 JS).
"""

import frappe
from frappe.utils import date_diff, flt, month_diff, nowdate

from asset_enterprise.rounding import fa_module_round


def _posted_ft_sums(asset_name):
	# Row-backed engine depreciation (source = Asset Depreciation
	# Schedule) is ALREADY counted through the posted schedule rows —
	# folding its accum_delta again double-counts (Phase 11 F0 fix).
	# Standalone Depreciation FTs (e.g. Expense-Immediately on merge)
	# have no row and must keep folding.
	row = frappe.db.sql(
		"""
		select
			coalesce(sum(hav_delta), 0)         as hav_delta,
			coalesce(sum(case when source_doctype = 'Asset Depreciation Schedule'
			                  then 0 else accum_delta end), 0) as accum_delta,
			coalesce(sum(life_delta_months), 0) as life_delta_months
		from `tabFinancial Treatment`
		where asset = %s
		  and status = 'Posted'
		  and ifnull(reversal_reference, '') = ''
		""",
		asset_name,
		as_dict=True,
	)[0]
	return row


def _posted_depreciation_total(asset_name):
	"""Sum of posted schedule-row amounts from the ACTIVE schedule only.

	Posted rows are copied verbatim into every superseding generation
	(GAP-031/032), so the Active schedule alone is the complete record —
	summing across Superseded generations would double-count.
	"""
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(ds.depreciation_amount), 0)
			from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s
			  and ads.docstatus = 1
			  and ads.status = 'Active'
			  and ifnull(ds.journal_entry, '') != ''
			  and ifnull(ds.reversal_journal_entry, '') = ''
			""",
			asset_name,
		)[0][0]
	)


def _category_accounts(asset):
	row = frappe.db.get_value(
		"Asset Category Account",
		{"parent": asset.asset_category, "company_name": asset.company},
		["fixed_asset_account", "accumulated_depreciation_account"],
		as_dict=True,
	)
	return row or frappe._dict({})


def _counted_vouchers(asset_name):
	"""Journal Entries already represented in the fold — schedule rows
	and Financial Treatments. Anything else on the asset's accounts is
	a manual posting."""
	names = set()
	for je, rev in frappe.db.sql(
		"""select ds.journal_entry, ds.reversal_journal_entry
		   from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s""",
		asset_name,
	):
		names.update(x for x in (je, rev) if x)
	names.update(
		frappe.get_all(
			"Financial Treatment",
			filters={"asset": asset_name, "journal_entry": ("is", "set")},
			pluck="journal_entry",
		)
	)
	return names


def _manual_gl_adjustments(asset):
	"""§5.1 / TC-010: the values are LEDGER-derived. A journal entry
	posted straight to the asset's fixed-asset or accumulated-
	depreciation account — outside the schedule and outside any
	Financial Treatment — still moves the asset's value, and the
	Recalculate button must pick it up."""
	accounts = _category_accounts(asset)
	if not (accounts.get("fixed_asset_account") or accounts.get("accumulated_depreciation_account")):
		return 0.0, 0.0

	rows = frappe.db.sql(
		"""select gle.account, gle.voucher_no, gle.debit, gle.credit
		   from `tabGL Entry` gle
		   where gle.is_cancelled = 0
		     and gle.against_voucher_type = 'Asset'
		     and gle.against_voucher = %s
		     and gle.account in %s""",
		(
			asset.name,
			tuple(
				a
				for a in (
					accounts.get("fixed_asset_account"),
					accounts.get("accumulated_depreciation_account"),
				)
				if a
			),
		),
		as_dict=True,
	)
	counted = _counted_vouchers(asset.name)
	hav_delta = accum_delta = 0.0
	for row in rows:
		if row.voucher_no in counted:
			continue
		if row.account == accounts.get("fixed_asset_account"):
			hav_delta += flt(row.debit) - flt(row.credit)
		else:
			accum_delta += flt(row.credit) - flt(row.debit)
	return hav_delta, accum_delta


def _remaining_life_months(asset, life_delta_months):
	"""C33: original UL − elapsed months since posting-basis date + net
	UL adjustments from AVA transactions (signed)."""
	fb = frappe.db.get_value(
		"Asset Finance Book",
		{"parent": asset.name},
		["total_number_of_depreciations", "frequency_of_depreciation", "depreciation_start_date"],
		as_dict=True,
	)
	if not fb or not fb.total_number_of_depreciations:
		return 0.0

	# The finance book's period count is ALREADY re-written by a Useful
	# Life Adjustment (TC-025 expects total UL = 48 there), so folding
	# the treatment's life delta on top counted every adjustment twice.
	total_months = flt(fb.total_number_of_depreciations) * flt(fb.frequency_of_depreciation or 1)
	start = fb.depreciation_start_date or asset.available_for_use_date
	elapsed = month_diff(nowdate(), start) - 1 if start else 0
	elapsed = max(0, elapsed)
	return max(0.0, flt(total_months - elapsed, 2))


def recalculate_asset_values(asset_name, save=True):
	"""Re-derive HAV / Accum / NBV / RUL for one asset. Returns the dict."""
	asset = frappe.get_doc("Asset", asset_name)
	company = asset.company
	ft = _posted_ft_sums(asset_name)

	manual_hav, manual_accum = _manual_gl_adjustments(asset)

	hav = fa_module_round(
		flt(asset.net_purchase_amount) + flt(ft.hav_delta) + flt(manual_hav), company
	)
	accum = fa_module_round(
		flt(asset.opening_accumulated_depreciation)
		+ _posted_depreciation_total(asset_name)
		+ flt(ft.accum_delta)
		+ flt(manual_accum),
		company,
	)
	nbv = fa_module_round(hav - accum, company)
	rul_months = _remaining_life_months(asset, ft.life_delta_months)

	values = {
		"historical_asset_value": hav,
		"accumulated_depreciation_value": accum,
		"net_book_value": nbv,
		"remaining_useful_life_months": rul_months,
		"remaining_useful_life_years": flt(rul_months / 12, 2),
	}
	if save:
		frappe.db.set_value("Asset", asset_name, values, update_modified=False)
		_sync_core_bookkeeping(asset, nbv, accum)
	return values


def _sync_core_bookkeeping(asset, nbv, accum):
	"""Keep core's own counters honest against the derived values.

	Core decrements finance_books.value_after_depreciation on every
	depreciation JE and its status logic reads THAT counter — but no
	core code credits it on our value events (TCC additions, invoice
	adjustments, revaluations). On ACC-ASS-2026-00125 a +100,000
	invoice adjustment left the counter 100,000 short, it went negative
	one row before the end, and core declared "Fully Depreciated" while
	6,162.30 was still unposted. The derived NBV is the authority —
	overwrite the counter with it after every recalculation, and
	restate the depreciation-lifecycle status from it.
	"""
	if asset.docstatus != 1 or not asset.calculate_depreciation:
		return
	frappe.db.set_value(
		"Asset Finance Book",
		{"parent": asset.name, "parenttype": "Asset"},
		"value_after_depreciation",
		flt(nbv),
		update_modified=False,
	)
	# Only the depreciation-lifecycle statuses may be restated — never
	# Disposed / Scrapped / Sold / Capitalized, which our flows own.
	if asset.status not in ("Submitted", "Partially Depreciated", "Fully Depreciated"):
		return
	salvage = flt(
		frappe.db.get_value(
			"Asset Finance Book", {"parent": asset.name}, "expected_value_after_useful_life"
		)
		or 0
	)
	if flt(nbv) <= salvage + 0.005:
		status = "Fully Depreciated"
	elif flt(accum) > 0:
		status = "Partially Depreciated"
	else:
		status = "Submitted"
	if status != asset.status:
		frappe.db.set_value("Asset", asset.name, "status", status, update_modified=False)


def assert_nbv_covers_reversal(asset_name, amount, context=None):
	"""VR-042 (2026-07-23 review): a reversal that reduces asset value
	is blocked when the current NBV cannot cover the amount being
	reversed — it would drive NBV negative / below salvage."""
	from frappe import _

	amount = flt(amount)
	if amount <= 0:
		return
	nbv = flt(recalculate_asset_values(asset_name, save=False)["net_book_value"])
	if amount > nbv + 0.005:
		frappe.throw(
			_(
				"Reversal amount {0} cannot be covered by the current Net Book Value "
				"{1} of Asset {2}{3}. The reversal is not allowed (VR-042) — handle "
				"the correction via Asset Value Adjustment."
			).format(amount, nbv, asset_name, f" ({context})" if context else ""),
			title=_("Reversal Not Covered by NBV"),
		)
