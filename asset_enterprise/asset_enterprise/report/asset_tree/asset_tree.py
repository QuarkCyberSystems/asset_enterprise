"""Asset Tree — GA-0005-01 GAP-009 (TC-012).

Collapsible parent/child hierarchy with per-asset values AND subtree
aggregates on every parent node. Rendered as a tree grid by the
report JS (tree: true, parent_field: parent_asset).
"""

import frappe
from frappe.utils import flt


def execute(filters=None):
	filters = filters or {}
	conditions = {"docstatus": ("<", 2)}
	if filters.get("company"):
		conditions["company"] = filters["company"]

	assets = frappe.get_all(
		"Asset",
		filters=conditions,
		fields=[
			"name",
			"asset_name",
			"parent_asset",
			"status",
			"asset_category",
			"historical_asset_value",
			"accumulated_depreciation_value",
			"net_book_value",
			"remaining_useful_life_months",
		],
	)
	# The stored HAV/Accum/NBV are snapshots written when a treatment
	# posts; an asset that has had none carries zeros, so the tree showed
	# blank values for most of the register (client sheet, row 44).
	# Derive them from the same single source every other screen uses —
	# the tree is a bounded set, so this stays cheap.
	from asset_enterprise.asset_values import recalculate_asset_values

	for a in assets:
		try:
			v = recalculate_asset_values(a.name, save=False)
			a.historical_asset_value = v["historical_asset_value"]
			a.accumulated_depreciation_value = v["accumulated_depreciation_value"]
			a.net_book_value = v["net_book_value"]
			a.remaining_useful_life_months = v["remaining_useful_life_months"]
		except Exception:
			pass  # a broken asset must not blank the whole tree

	by_name = {a.name: a for a in assets}
	children = {}
	for a in assets:
		if a.parent_asset and a.parent_asset in by_name:
			children.setdefault(a.parent_asset, []).append(a.name)

	in_tree = set(children.keys())
	for kids in children.values():
		in_tree.update(kids)
	# ancestors of tree members count too (deep trees)
	for name in list(in_tree):
		p = by_name.get(name)
		while p and p.parent_asset and p.parent_asset in by_name:
			in_tree.add(p.parent_asset)
			p = by_name[p.parent_asset]

	if not filters.get("include_standalone"):
		assets = [a for a in assets if a.name in in_tree]

	def subtree(name):
		"""(hav, accum, nbv) aggregated over the node and descendants."""
		a = by_name[name]
		hav = flt(a.historical_asset_value)
		accum = flt(a.accumulated_depreciation_value)
		nbv = flt(a.net_book_value)
		for kid in children.get(name, []):
			kh, ka, kn = subtree(kid)
			hav += kh
			accum += ka
			nbv += kn
		return hav, accum, nbv

	rows = []
	for a in assets:
		hav, accum, nbv = subtree(a.name)
		rows.append(
			{
				"asset": a.name,
				"parent_asset": a.parent_asset if (a.parent_asset in by_name) else None,
				"asset_name": a.asset_name,
				"status": a.status,
				"asset_category": a.asset_category,
				"historical_asset_value": flt(a.historical_asset_value),
				"accumulated_depreciation_value": flt(a.accumulated_depreciation_value),
				"net_book_value": flt(a.net_book_value),
				"remaining_useful_life_months": flt(a.remaining_useful_life_months),
				"subtree_hav": hav,
				"subtree_nbv": nbv,
				"children_count": len(children.get(a.name, [])),
			}
		)
	# parents before children so the tree grid can nest correctly
	rows.sort(key=lambda r: (r["parent_asset"] or "", r["asset"]))

	columns = [
		{"fieldname": "asset", "label": "Asset", "fieldtype": "Link", "options": "Asset", "width": 220},
		{"fieldname": "asset_name", "label": "Asset Name", "fieldtype": "Data", "width": 240},
		{"fieldname": "status", "label": "Status", "fieldtype": "Data", "width": 110},
		{"fieldname": "asset_category", "label": "Category", "fieldtype": "Link", "options": "Asset Category", "width": 140},
		{"fieldname": "historical_asset_value", "label": "HAV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "accumulated_depreciation_value", "label": "Accum Depr", "fieldtype": "Currency", "width": 120},
		{"fieldname": "net_book_value", "label": "NBV", "fieldtype": "Currency", "width": 120},
		{"fieldname": "subtree_hav", "label": "Subtree HAV", "fieldtype": "Currency", "width": 130},
		{"fieldname": "subtree_nbv", "label": "Subtree NBV", "fieldtype": "Currency", "width": 130},
		{"fieldname": "remaining_useful_life_months", "label": "RUL (Months)", "fieldtype": "Float", "width": 100},
		{"fieldname": "children_count", "label": "Children", "fieldtype": "Int", "width": 80},
	]
	return columns, rows
