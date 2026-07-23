"""Rounding policy — GA-0005-01 v2.14 §4.10.

Single source of truth for every depreciation-amount rounding in the
app. No ad-hoc round(x, 2) anywhere else.

Rules:
1. Precision = company currency's smallest fraction (SAR/USD 0.01,
   BHD/KWD 0.001). Never hard-coded.
2. Module rounding == GL rounding: amounts are rounded HERE, before
   they reach get_gl_dict, so schedule rows and GL rows are equal.
3. Final-period drift absorption: the last schedule row is computed as
   depreciable-base minus everything already posted — never rate x days
   — so NBV lands on Salvage exactly.
4. Drift beyond the per-company tolerance is still posted (recon is
   always exact) but flagged for the Daily Reconciliation Report.
"""

from decimal import ROUND_HALF_UP, Decimal

import frappe

_PRECISION_CACHE = {}


def get_currency_precision(company):
	"""Decimal places implied by the company currency's smallest fraction."""
	if company in _PRECISION_CACHE:
		return _PRECISION_CACHE[company]

	currency = frappe.db.get_value("Company", company, "default_currency")
	fraction = (
		frappe.db.get_value("Currency", currency, "smallest_currency_fraction_value") or 0.01
	)
	# 0.01 -> 2 places, 0.001 -> 3, 1 -> 0
	places = max(0, -Decimal(str(fraction)).as_tuple().exponent)
	_PRECISION_CACHE[company] = places
	return places


def fa_module_round(amount, company):
	"""Round a depreciation/asset amount to company currency precision.

	Half-up (commercial rounding), matching what finance expects on
	printed schedules.
	"""
	places = get_currency_precision(company)
	quantum = Decimal(1).scaleb(-places)
	return float(Decimal(str(amount or 0)).quantize(quantum, rounding=ROUND_HALF_UP))


def final_row_amount(depreciable_base, prior_posted_total, company):
	"""§4.10 point 3 — the last row absorbs accumulated drift.

	Final Row Amount = (HAV − Salvage) − sum(prior rows), rounded.
	Guarantees NBV == Salvage after the last posting.
	"""
	return fa_module_round(depreciable_base - prior_posted_total, company)


def final_row_drift(final_amount, nominal_amount, company):
	"""Difference between the absorbing final row and the nominal
	rate x days amount. Positive or negative."""
	return fa_module_round(final_amount - nominal_amount, company)


def is_drift_beyond_tolerance(drift, company):
	"""§4.10 point 4 — informational flag only; posting is never blocked."""
	from asset_enterprise.accounts import get_last_period_tolerance

	return abs(drift) > get_last_period_tolerance(company)
