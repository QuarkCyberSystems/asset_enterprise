"""Fixed-asset items are counted in units (client, 27/09 FA-002; 01/10 FA-013).

Core creates one asset per unit of the receipt row's quantity, read in
the row's own UOM, so an asset item received in any other unit — "Hour",
a box of ten — creates the wrong number of assets. The units a fixed-asset
item may use are the UOMs ticked "Allowed For FA" (UOM master; "Nos" is
ticked at upgrade, from the former Asset Settings > Asset Item UOM). No
UOM ticked lifts the rule.

- an enabled fixed-asset item holds an allowed UOM as its stock unit and
  lists no other unit (no alternates: one unit is one asset), and
- a Purchase Receipt or Purchase Invoice row for a fixed-asset item is in
  an allowed UOM.
"""

import frappe
from frappe import _


def allowed_fa_uoms():
	"""The UOMs ticked Allowed For FA, or an empty set (rule lifted)."""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled() or not frappe.db.has_column("UOM", "allowed_for_fa"):
		return set()
	return set(frappe.get_all("UOM", filters={"allowed_for_fa": 1, "enabled": 1}, pluck="name"))


def _names(uoms):
	return ", ".join(sorted(uoms))


def validate_item(doc, method=None):
	"""Item validate."""
	if not doc.get("is_fixed_asset") or doc.get("disabled"):
		return
	allowed = allowed_fa_uoms()
	if not allowed:
		return
	if doc.stock_uom not in allowed:
		frappe.throw(
			_(
				"{0} is a fixed-asset item: its Default Unit of Measure must be a unit ticked "
				"Allowed For FA ({1}), one unit per asset."
			).format(doc.name, frappe.bold(_names(allowed))),
			title=_("Asset Item UOM"),
		)
	others = sorted({d.uom for d in doc.get("uoms") or [] if d.uom and d.uom != doc.stock_uom})
	if others:
		frappe.throw(
			_(
				"{0} is a fixed-asset item and is counted in {1} only; remove {2} from its "
				"Units of Measure."
			).format(doc.name, frappe.bold(doc.stock_uom), ", ".join(others)),
			title=_("Asset Item UOM"),
		)


def validate_purchase_rows(doc, method=None):
	"""Purchase Receipt / Purchase Invoice validate — rows that RECEIVE an
	asset item. A return or a Cancellation undoes a receipt as it was
	made, and an invoice row billing a receipt row carries that row's
	unit: refusing those would freeze a receipt accepted before the rule
	with no way to bill, return or cancel it (chief review 27/09)."""
	if doc.get("is_return") or doc.get("is_cancellation") or doc.get("cancellation_against"):
		return
	allowed = allowed_fa_uoms()
	if not allowed:
		return
	bad = [
		row for row in doc.get("items") or []
		if row.get("is_fixed_asset")
		and not (row.get("pr_detail") or row.get("purchase_receipt"))
		and (row.uom not in allowed or (row.get("stock_uom") and row.stock_uom not in allowed))
	]
	if not bad:
		return
	lines = "<br>".join(
		_("Row {0}: {1} in {2}").format(row.idx, row.item_code, row.uom) for row in bad
	)
	frappe.throw(
		_("Fixed-asset items are received and invoiced in a unit ticked Allowed For FA ({0}), one unit per asset:<br>{1}").format(
			frappe.bold(_names(allowed)), lines
		),
		title=_("Asset Item UOM"),
	)


@frappe.whitelist()
def fa_uom_filters():
	"""For the Item form's UOM pickers: the allowed units, or none (no rule)."""
	return sorted(allowed_fa_uoms())
