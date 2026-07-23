import frappe


def run():
	print("Asset Category:", frappe.db.count("Asset Category"))
	print("Fixed-asset Items:", frappe.db.count("Item", {"is_fixed_asset": 1}))
	print("Assets:", frappe.db.count("Asset"))
	print("Companies:", frappe.get_all("Company", pluck="name"))
	cat = frappe.db.get_value("Asset Category", {}, "name")
	if cat:
		accounts = frappe.get_all(
			"Asset Category Account",
			filters={"parent": cat},
			fields=["company_name", "fixed_asset_account", "accumulated_depreciation_account"],
		)
		print("First category:", cat, accounts)
