# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Seed Asset Settings > Asset Item UOM with "Nos" on sites installed
before the setting existed (client, 27/09: an item flagged as an asset is
held in Nos). A Single keeps no default for a field added later, so the
rule would otherwise start switched off. Idempotent: a site that already
holds a value — or cleared it on purpose after this ran — is left alone,
and a site without a "Nos" UOM is reported, not guessed at.
"""

import frappe


def execute():
	if frappe.db.get_single_value("Asset Settings", "asset_item_uom"):
		return
	if not frappe.db.exists("UOM", "Nos"):
		print("asset_item_uom: no UOM named 'Nos' on this site; set Asset Settings > Asset Item UOM by hand")
		return
	frappe.db.set_single_value("Asset Settings", "asset_item_uom", "Nos")
	offending = frappe.get_all(
		"Item", filters={"is_fixed_asset": 1, "stock_uom": ("!=", "Nos"), "disabled": 0},
		fields=["name", "stock_uom"],
	)
	print(f"asset_item_uom: set to Nos; {len(offending)} enabled fixed-asset item(s) held in another UOM")
	for item in offending:
		print(f"  {item.name}: {item.stock_uom}")
