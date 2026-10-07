"""Fixed-asset items are counted in units (client, 27/09 FA-002; 01/10 FA-013).

Core creates one asset per unit of the receipt row's quantity, read in
the row's own UOM, so an asset item received in any other unit — "Hour",
a box of ten — creates the wrong number of assets. The units a fixed-asset
item may use are the UOMs ticked "Allowed For FA" (UOM master; "Nos" is
ticked at upgrade, from the former Asset Settings > Asset Item UOM). No
UOM ticked lifts the rule.

- an enabled fixed-asset item holds an allowed UOM as its stock unit and
  lists no other unit (no alternates: one unit is one asset); its default
  purchase and sales units are that unit, and
- every buying and selling document row for a fixed-asset item — Material
  Request, Supplier Quotation, Purchase Order, Purchase Receipt, Purchase
  Invoice, Quotation, Sales Order, Delivery Note, Sales Invoice — is in an
  allowed UOM (client, 06/10: "should be apply for all document that
  include UOM of asset").
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
	# the default purchase / sales unit: one unit is one asset, so they are
	# the stock unit (filled when empty, refused when another)
	for field, label in (("purchase_uom", _("Default Purchase Unit of Measure")),
			("sales_uom", _("Default Sales Unit of Measure"))):
		if not doc.get(field):
			doc.set(field, doc.stock_uom)
		elif doc.get(field) != doc.stock_uom:
			frappe.throw(
				_(
					"{0} is a fixed-asset item and is counted in {1} only; its {2} must be "
					"{1}, not {3}."
				).format(doc.name, frappe.bold(doc.stock_uom), label, doc.get(field)),
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


# A row made FROM an earlier document's row carries that row's unit:
# refusing it would freeze a document accepted before the rule with no way
# to receive, bill, deliver, return or cancel it (chief review 27/09).
_SOURCE_ROW_FIELDS = (
	"pr_detail", "purchase_receipt", "po_detail", "purchase_order_item", "material_request_item",
	"supplier_quotation_item", "so_detail", "sales_order_item", "dn_detail", "delivery_note_item",
	"quotation_item", "prevdoc_docname",
)


def validate_purchase_rows(doc, method=None):
	"""Buying and selling documents' validate — the rows of a fixed-asset
	item. A return or a Cancellation undoes a document as it was made, and
	a row made from an earlier document's row carries its unit; neither is
	refused."""
	if doc.get("is_return") or doc.get("is_cancellation") or doc.get("cancellation_against"):
		return
	allowed = allowed_fa_uoms()
	if not allowed:
		return
	rows = [r for r in doc.get("items") or [] if r.get("item_code")]
	# purchase rows carry is_fixed_asset; sales and request rows do not
	asset_items = set(frappe.get_all(
		"Item", filters={"name": ("in", list({r.item_code for r in rows}) or [""]), "is_fixed_asset": 1},
		pluck="name",
	)) if rows else set()
	bad = [
		row for row in rows
		if (row.get("is_fixed_asset") or row.item_code in asset_items)
		and not any(row.get(f) for f in _SOURCE_ROW_FIELDS)
		and (row.get("uom") not in allowed or (row.get("stock_uom") and row.stock_uom not in allowed))
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
