"""Composite Merge Log Report — GA-0005-01 GAP-035.

Fleet-wide register of component merges with the value snapshot, for
scrap-sizing and audit ("what physical items went into this composite,
and what were they worth on entry").
"""

import frappe


def execute(filters=None):
	filters = filters or {}
	conditions = {"parenttype": "Asset"}
	if filters.get("composite_asset"):
		conditions["parent"] = filters["composite_asset"]
	if filters.get("source_asset"):
		conditions["merged_source_asset"] = filters["source_asset"]
	if filters.get("status"):
		conditions["status"] = filters["status"]
	# GAP-035 req 6 (Phase 11b): date-range filterability.
	if filters.get("from_date") and filters.get("to_date"):
		conditions["merged_date"] = ("between", [filters["from_date"], filters["to_date"]])
	elif filters.get("from_date"):
		conditions["merged_date"] = (">=", filters["from_date"])
	elif filters.get("to_date"):
		conditions["merged_date"] = ("<=", filters["to_date"])

	rows = frappe.get_all(
		"Composite Merge Log Entry",
		filters=conditions,
		fields=[
			"parent as composite_asset",
			"merged_source_asset",
			"merged_source_asset_name",
			"merged_date",
			"merged_via_capitalization",
			"historical_value_at_merge",
			"accumulated_depreciation_at_merge",
			"net_book_value_at_merge",
			"remaining_useful_life_in_months",
			"remaining_useful_life_in_years",
			"status",
		],
		order_by="merged_date desc",
	)
	columns = [
		{"fieldname": "composite_asset", "label": "Composite", "fieldtype": "Link", "options": "Asset", "width": 160},
		{"fieldname": "merged_source_asset", "label": "Source Asset", "fieldtype": "Link", "options": "Asset", "width": 160},
		{"fieldname": "merged_source_asset_name", "label": "Source Name", "fieldtype": "Data", "width": 160},
		{"fieldname": "merged_date", "label": "Merged (Disposal) Date", "fieldtype": "Date", "width": 120},
		{"fieldname": "merged_via_capitalization", "label": "Capitalization", "fieldtype": "Link", "options": "Asset Capitalization", "width": 150},
		{"fieldname": "historical_value_at_merge", "label": "HAV at Merge", "fieldtype": "Currency", "width": 130},
		{"fieldname": "accumulated_depreciation_at_merge", "label": "Accum at Merge", "fieldtype": "Currency", "width": 130},
		{"fieldname": "net_book_value_at_merge", "label": "NBV at Merge", "fieldtype": "Currency", "width": 130},
		{"fieldname": "remaining_useful_life_in_months", "label": "RUL at Merge (Months)", "fieldtype": "Int", "width": 110},
		{"fieldname": "remaining_useful_life_in_years", "label": "RUL at Merge (Years)", "fieldtype": "Float", "width": 110},
		{"fieldname": "status", "label": "Status", "fieldtype": "Data", "width": 90},
	]
	return columns, rows
