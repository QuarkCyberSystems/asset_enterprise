"""Asset Daily Reconciliation — GA-0005-01 VR-008 / §4.10.

Per asset: stored Enterprise-tab values vs freshly derived values, and
the final-row drift vs the per-company tolerance. Reconciliation is
always exact by construction (§4.10 point 2); rows appear here only
when stored values are stale (recalc pending) or final-row drift
exceeded tolerance (informational — the drift is still posted).
"""

import frappe
from frappe.utils import flt


def execute(filters=None):
	filters = filters or {}
	from asset_enterprise.accounts import get_last_period_tolerance
	from asset_enterprise.asset_values import recalculate_asset_values

	asset_filters = {"docstatus": 1}
	if filters.get("company"):
		asset_filters["company"] = filters["company"]

	rows = []
	for a in frappe.get_all(
		"Asset",
		filters=asset_filters,
		fields=["name", "company", "historical_asset_value", "accumulated_depreciation_value", "net_book_value"],
	):
		derived = recalculate_asset_values(a.name, save=False)
		tolerance = get_last_period_tolerance(a.company)
		hav_diff = flt(derived["historical_asset_value"] - flt(a.historical_asset_value), 2)
		nbv_diff = flt(derived["net_book_value"] - flt(a.net_book_value), 2)
		flagged = abs(hav_diff) > 0 or abs(nbv_diff) > tolerance
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
				"tolerance": tolerance,
				"flagged": "Yes" if flagged else "No",
			}
		)

	columns = [
		{"fieldname": "asset", "label": "Asset", "fieldtype": "Link", "options": "Asset", "width": 160},
		{"fieldname": "stored_hav", "label": "Stored HAV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "derived_hav", "label": "Derived HAV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "stored_nbv", "label": "Stored NBV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "derived_nbv", "label": "Derived NBV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "nbv_diff", "label": "NBV Diff", "fieldtype": "Currency", "width": 110},
		{"fieldname": "tolerance", "label": "Tolerance", "fieldtype": "Currency", "width": 100},
		{"fieldname": "flagged", "label": "Flagged", "fieldtype": "Data", "width": 80},
	]
	return columns, rows
