"""Small whitelisted API surface for the JS layer (C126: JS is UX-only;
these endpoints re-run the authoritative logic)."""

import frappe


@frappe.whitelist()
def recalculate(asset_name):
	from asset_enterprise.asset_values import recalculate_asset_values

	frappe.has_permission("Asset", "write", asset_name, throw=True)
	return recalculate_asset_values(asset_name, save=True)


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
