"""Replacement Chain — GA-0005-01 GAP-016 two-way recovery trace."""

import frappe


def execute(filters=None):
	filters = filters or {}
	conditions = ["(ifnull(a.replacement_of_asset, '') != '' or ifnull(a.replaced_by_asset, '') != '')"]
	values = {}
	if filters.get("company"):
		conditions.append("a.company = %(company)s")
		values["company"] = filters["company"]

	rows = frappe.db.sql(
		f"""
		select a.name as asset, a.asset_name, a.status, a.disposal_date,
		       a.replacement_of_asset, a.replaced_by_asset,
		       a.scrap_reversal_journal_entry
		from `tabAsset` a
		where {" and ".join(conditions)}
		order by a.modified desc
		""",
		values,
		as_dict=True,
	)
	columns = [
		{"fieldname": "asset", "label": "Asset", "fieldtype": "Link", "options": "Asset", "width": 160},
		{"fieldname": "asset_name", "label": "Name", "fieldtype": "Data", "width": 180},
		{"fieldname": "status", "label": "Status", "fieldtype": "Data", "width": 110},
		{"fieldname": "disposal_date", "label": "Disposal Date", "fieldtype": "Date", "width": 110},
		{"fieldname": "replacement_of_asset", "label": "Replacement Of", "fieldtype": "Link", "options": "Asset", "width": 160},
		{"fieldname": "replaced_by_asset", "label": "Replaced By", "fieldtype": "Link", "options": "Asset", "width": 160},
		{"fieldname": "scrap_reversal_journal_entry", "label": "Restore JE", "fieldtype": "Link", "options": "Journal Entry", "width": 150},
	]
	return columns, rows
