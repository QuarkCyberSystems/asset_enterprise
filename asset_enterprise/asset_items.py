"""Fixed-asset items are counted in units (client, 27/09, FA-002).

Core creates one asset per unit of the receipt row's quantity, read in
the row's own UOM, so an asset item received in any other unit — "Hour",
a box of ten — creates the wrong number of assets. The unit is set in
Asset Settings > Asset Item UOM ("Nos" by default; empty lifts the rule):

- an enabled fixed-asset item holds that UOM as its stock unit and lists
  no other, and
- a Purchase Receipt or Purchase Invoice row for a fixed-asset item is
  in that UOM.
"""

import frappe
from frappe import _


def asset_item_uom():
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		return None
	return frappe.db.get_single_value("Asset Settings", "asset_item_uom")


def validate_item(doc, method=None):
	"""Item validate."""
	if not doc.get("is_fixed_asset") or doc.get("disabled"):
		return
	uom = asset_item_uom()
	if not uom:
		return
	if doc.stock_uom != uom:
		frappe.throw(
			_(
				"{0} is a fixed-asset item: its Default Unit of Measure must be {1}, one unit "
				"per asset (Asset Settings > Asset Item UOM)."
			).format(doc.name, frappe.bold(uom)),
			title=_("Asset Item UOM"),
		)
	others = sorted({d.uom for d in doc.get("uoms") or [] if d.uom and d.uom != uom})
	if others:
		frappe.throw(
			_(
				"{0} is a fixed-asset item and is counted in {1} only; remove {2} from its "
				"Units of Measure."
			).format(doc.name, frappe.bold(uom), ", ".join(others)),
			title=_("Asset Item UOM"),
		)


def validate_purchase_rows(doc, method=None):
	"""Purchase Receipt / Purchase Invoice validate."""
	uom = asset_item_uom()
	if not uom:
		return
	bad = [
		row for row in doc.get("items") or []
		if row.get("is_fixed_asset") and (row.uom != uom or (row.get("stock_uom") and row.stock_uom != uom))
	]
	if not bad:
		return
	lines = "<br>".join(
		_("Row {0}: {1} in {2}").format(row.idx, row.item_code, row.uom) for row in bad
	)
	frappe.throw(
		_("Fixed-asset items are received and invoiced in {0}, one unit per asset:<br>{1}").format(
			frappe.bold(uom), lines
		),
		title=_("Asset Item UOM"),
	)
