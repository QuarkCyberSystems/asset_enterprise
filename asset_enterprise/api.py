"""Small whitelisted API surface for the JS layer (C126: JS is UX-only;
these endpoints re-run the authoritative logic)."""

import frappe


@frappe.whitelist()
def recalculate(asset_name):
	from asset_enterprise.asset_values import recalculate_asset_values

	frappe.has_permission("Asset", "write", asset_name, throw=True)
	return recalculate_asset_values(asset_name, save=True)


@frappe.whitelist()
def enable_depreciation_defaults(asset_name):
	"""Prefill for the GAP-011 Enable Depreciation dialog from the Asset
	Category's finance-book defaults — the same values core copies onto a
	new Asset when the category is chosen at creation (client, 18/08:
	the dialog opened empty although the category carries them)."""
	from frappe.utils import flt

	from asset_enterprise.rounding import fa_module_round

	frappe.has_permission("Asset", "read", asset_name, throw=True)
	asset = frappe.db.get_value(
		"Asset",
		asset_name,
		["asset_category", "company", "net_purchase_amount", "available_for_use_date", "purchase_receipt"],
		as_dict=True,
	)
	if not asset:
		return {}

	# §4.4 basis prefill: the asset's own in-service date, else the
	# receipt posting date it arrived on (Ruba, 18/08).
	afu = asset.available_for_use_date or (
		asset.purchase_receipt
		and frappe.db.get_value("Purchase Receipt", asset.purchase_receipt, "posting_date")
	)
	if not asset.asset_category:
		return {"available_for_use_date": afu}

	rows = frappe.get_all(
		"Asset Finance Book",
		filters={"parent": asset.asset_category, "parenttype": "Asset Category"},
		fields=[
			"finance_book",
			"total_number_of_depreciations",
			"frequency_of_depreciation",
			"expected_value_after_useful_life",
			"salvage_value_percentage",
			"depreciation_start_date",
		],
		order_by="idx",
	)
	if not rows:
		return {"available_for_use_date": afu}
	default_fb = frappe.db.get_value("Company", asset.company, "default_finance_book")
	row = next((r for r in rows if r.finance_book == default_fb), rows[0])

	# Category-level salvage is normally a percentage of the asset's own
	# value; an absolute amount on the row wins when someone set one.
	salvage = flt(row.expected_value_after_useful_life) or fa_module_round(
		flt(asset.net_purchase_amount) * flt(row.salvage_value_percentage) / 100,
		asset.company,
	)
	# Posting-date default, core's rule (asset.py:617): the category
	# row's own date when it has one and it is not before the in-service
	# date, else the last day of the in-service month — never "today"
	# (Ruba, 18/08).
	from frappe.utils import get_last_day, getdate

	posting = row.depreciation_start_date
	if afu:
		if not posting or getdate(posting) < getdate(afu):
			posting = get_last_day(afu)

	return {
		"total_number_of_depreciations": row.total_number_of_depreciations,
		"frequency_of_depreciation": row.frequency_of_depreciation or 1,
		"expected_value_after_useful_life": salvage,
		"finance_book": row.finance_book,
		"available_for_use_date": afu,
		"depreciation_start_date": posting,
	}


@frappe.whitelist()
def tree_panel(asset_name):
	"""GAP-009: parent + children + subtree totals for the Asset form's
	tree panel."""
	frappe.has_permission("Asset", "read", asset_name, throw=True)
	parent = frappe.db.get_value("Asset", asset_name, "parent_asset")
	parent_name = parent and frappe.db.get_value("Asset", parent, "asset_name")
	children = frappe.get_all(
		"Asset",
		filters={"parent_asset": asset_name, "docstatus": ("<", 2)},
		fields=[
			"name",
			"asset_name",
			"status",
			"historical_asset_value",
			"net_book_value",
		],
		order_by="name",
	)
	return {
		"parent": parent,
		"parent_name": parent_name,
		"children": children,
		"totals": tree_aggregate(asset_name) if children else None,
	}


@frappe.whitelist()
def tree_aggregate(asset_name):
	"""GAP-009: aggregated HAV / Accum / NBV over the asset and every
	descendant (parent_asset chain), for the tree dashboard (TC-012)."""
	from asset_enterprise.asset_values import recalculate_asset_values

	frappe.has_permission("Asset", "read", asset_name, throw=True)

	nodes, queue = [asset_name], [asset_name]
	while queue:
		children = frappe.get_all(
			"Asset", filters={"parent_asset": queue.pop(0), "docstatus": 1}, pluck="name"
		)
		nodes.extend(children)
		queue.extend(children)

	totals = {
		"assets": len(nodes),
		"historical_asset_value": 0.0,
		"accumulated_depreciation_value": 0.0,
		"net_book_value": 0.0,
	}
	for name in nodes:
		values = recalculate_asset_values(name, save=False)
		totals["historical_asset_value"] += values["historical_asset_value"]
		totals["accumulated_depreciation_value"] += values["accumulated_depreciation_value"]
		totals["net_book_value"] += values["net_book_value"]
	return totals
