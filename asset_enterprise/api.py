"""Small whitelisted API surface for the JS layer (C126: JS is UX-only;
these endpoints re-run the authoritative logic)."""

import frappe


@frappe.whitelist()
def recalculate(asset_name):
	from asset_enterprise.asset_values import recalculate_asset_values

	frappe.has_permission("Asset", "write", asset_name, throw=True)
	return recalculate_asset_values(asset_name, save=True)
