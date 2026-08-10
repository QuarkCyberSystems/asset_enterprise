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

	total_months = flt(fb.total_number_of_depreciations) * flt(fb.frequency_of_depreciation or 1)
	start = fb.depreciation_start_date or asset.available_for_use_date
	elapsed = month_diff(nowdate(), start) - 1 if start else 0
	elapsed = max(0, elapsed)
	rul = total_months - elapsed + flt(life_delta_months)
	return max(0.0, flt(rul, 2))


def recalculate_asset_values(asset_name, save=True):
	"""Re-derive HAV / Accum / NBV / RUL for one asset. Returns the dict."""
	asset = frappe.get_doc("Asset", asset_name)
	company = asset.company
	ft = _posted_ft_sums(asset_name)

	hav = fa_module_round(flt(asset.net_purchase_amount) + flt(ft.hav_delta), company)
	accum = fa_module_round(
		flt(asset.opening_accumulated_depreciation)
		+ _posted_depreciation_total(asset_name)
		+ flt(ft.accum_delta),
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
	return values


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
