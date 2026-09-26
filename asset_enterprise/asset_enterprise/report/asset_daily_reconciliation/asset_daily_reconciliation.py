"""VR-008: stored values, the treatment fold, and independent GL balances.

Any difference at company currency precision is flagged. Posting drift
is governed separately by CH-01; tolerance never hides a ledger mismatch.
"""

import frappe
from frappe.utils import flt


def execute(filters=None):
	filters = filters or {}
	from asset_enterprise.accounts import get_last_period_tolerance
	from asset_enterprise.asset_values import fold_asset_values, gl_asset_values, reference_gl_asset_values
	from asset_enterprise.rounding import fa_module_round

	asset_filters = {"docstatus": 1}
	if filters.get("company"):
		asset_filters["company"] = filters["company"]

	rows = []
	for a in frappe.get_all(
		"Asset",
		filters=asset_filters,
		fields=["name", "company", "historical_asset_value", "accumulated_depreciation_value", "net_book_value"],
	):
		derived = fold_asset_values(a.name)
		ledger = gl_asset_values(a.name)
		reference = reference_gl_asset_values(a.name)
		key_mismatch = any(fa_module_round(reference[key] - ledger[key], a.company) for key in ledger)
		tolerance = get_last_period_tolerance(a.company)
		hav_diff = fa_module_round(ledger["historical_asset_value"] - flt(a.historical_asset_value), a.company)
		nbv_diff = fa_module_round(ledger["net_book_value"] - flt(a.net_book_value), a.company)
		accum_diff = fa_module_round(ledger["accumulated_depreciation_value"] - flt(a.accumulated_depreciation_value), a.company)
		flagged = key_mismatch or any((hav_diff, nbv_diff, accum_diff)) or any(
			fa_module_round(derived[key] - ledger[key], a.company)
			for key in ledger
		)
		if filters.get("flagged_only") and not flagged:
			continue
		rows.append(
			{
				"asset": a.name,
				"stored_hav": a.historical_asset_value,
				"derived_hav": derived["historical_asset_value"],
				"stored_nbv": a.net_book_value,
				"derived_nbv": derived["net_book_value"],
				"nbv_diff": nbv_diff,
				"gl_hav": ledger["historical_asset_value"],
				"reference_hav": reference["historical_asset_value"],
				"reference_accum": reference["accumulated_depreciation_value"],
				"reference_nbv": reference["net_book_value"],
				"key_mismatch": "Yes" if key_mismatch else "No",
				"gl_accum": ledger["accumulated_depreciation_value"],
				"gl_nbv": ledger["net_book_value"],
				"hav_diff": hav_diff,
				"accum_diff": accum_diff,
				"tolerance": tolerance,
				"flagged": "Yes" if flagged else "No",
			}
		)

	columns = [
		{"fieldname": "asset", "label": "Asset", "fieldtype": "Link", "options": "Asset", "width": 160},
		{"fieldname": "stored_hav", "label": "Stored HAV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "derived_hav", "label": "Treatment HAV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "stored_nbv", "label": "Stored NBV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "derived_nbv", "label": "Treatment NBV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "reference_hav", "label": "Reference-key HAV", "fieldtype": "Currency", "width": 140},
		{"fieldname": "reference_accum", "label": "Reference-key Accumulated", "fieldtype": "Currency", "width": 160},
		{"fieldname": "reference_nbv", "label": "Reference-key NBV", "fieldtype": "Currency", "width": 140},
		{"fieldname": "key_mismatch", "label": "Key Mismatch", "fieldtype": "Data", "width": 100},
		{"fieldname": "gl_hav", "label": "GL HAV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "gl_accum", "label": "GL Accumulated", "fieldtype": "Currency", "width": 120},
		{"fieldname": "gl_nbv", "label": "GL NBV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "hav_diff", "label": "GL − Stored HAV", "fieldtype": "Currency", "width": 130},
		{"fieldname": "accum_diff", "label": "GL − Stored Accumulated", "fieldtype": "Currency", "width": 150},
		{"fieldname": "nbv_diff", "label": "GL − Stored NBV", "fieldtype": "Currency", "width": 110},
		{"fieldname": "tolerance", "label": "Tolerance", "fieldtype": "Currency", "width": 100},
		{"fieldname": "flagged", "label": "Flagged", "fieldtype": "Data", "width": 80},
	]
	return columns, rows
