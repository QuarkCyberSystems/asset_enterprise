"""Per-asset attribution of the acquisition leg — GAP-006 / §5.1 (audit C1).

§5.1 defines asset values as the GL balances on the asset's Fixed Asset
and Accumulated Depreciation accounts, and VR-008 requires the displayed
figures to match those balances exactly. Every entry our own engine
posts carries the `asset` accounting dimension, so for an asset whose
whole history we posted, that holds today.

It fails on the one leg we do not post. Core books an asset purchase
with NEITHER the dimension nor `against_voucher`, and writes ONE row per
receipt LINE — so a line of qty 4 books a single amount covering four
assets (UAT MAT-PRE-2026-00466: `Dr 1730 8,000` for ACC-ASS-2026-00210
through 00213). The acquisition cost is therefore unattributable in the
ledger, which is why the values fold has to start from
`Asset.net_purchase_amount` rather than from GL at all.

This module gives that leg its dimension:

- a line that created ONE asset is stamped;
- a line that created SEVERAL is split, one row per asset, weighted by
  each asset's own value, with the last row absorbing the rounding
  remainder (the §4.10 pattern).

Account totals never change and `voucher_detail_no` is preserved, so the
trial balance and receipt-level reports tie exactly as before. The split
runs AFTER core's `process_gl_map`: `merge_similar_entries` keys on the
accounting dimensions, so rows for different assets never merge back
into one.
"""

import frappe
from frappe.utils import flt

from asset_enterprise.rounding import fa_module_round

# Every monetary field a gl dict may carry; whichever are present are
# split together so the row stays internally consistent.
_AMOUNT_FIELDS = (
	"debit",
	"credit",
	"debit_in_account_currency",
	"credit_in_account_currency",
	"debit_in_transaction_currency",
	"credit_in_transaction_currency",
)

_ITEM_DOCTYPE = {
	"Purchase Receipt": ("Purchase Receipt Item", "purchase_receipt_item"),
	"Purchase Invoice": ("Purchase Invoice Item", "purchase_invoice_item"),
}


def _asset_line(doctype, detail_name):
	"""The receipt/invoice line behind a gl row, when it is a fixed-asset
	line. Returns (item_row, asset_link_field) or (None, None)."""
	item_doctype, asset_field = _ITEM_DOCTYPE.get(doctype, (None, None))
	if not item_doctype or not detail_name:
		return None, None
	row = frappe.db.get_value(
		item_doctype, detail_name, ["is_fixed_asset", "expense_account"], as_dict=True
	)
	if not row or not row.is_fixed_asset:
		return None, None
	return row, asset_field


def _assets_from_line(asset_field, detail_name):
	"""Assets created by that line, oldest first. Cancelled assets are
	excluded — their cost no longer belongs to them."""
	return frappe.get_all(
		"Asset",
		filters={asset_field: detail_name, "docstatus": ("<", 2)},
		fields=["name", "net_purchase_amount"],
		order_by="creation, name",
	)


def _shares(total, assets, company):
	"""Split `total` across the assets by their own values, last row
	absorbing the remainder so the parts always sum to the whole."""
	weights = [flt(a.net_purchase_amount) for a in assets]
	if not any(weights):  # nothing to weigh by — equal parts
		weights = [1.0] * len(assets)
	weight_total = sum(weights)
	parts, running = [], 0.0
	for weight in weights[:-1]:
		part = fa_module_round(flt(total) * weight / weight_total, company)
		parts.append(part)
		running += part
	parts.append(fa_module_round(flt(total) - running, company))
	return parts


def attribute_asset_legs(doc, gl_entries):
	"""Stamp — and where a line made several assets, split — the leg that
	lands on the asset's own account. Anything else is returned untouched.
	"""
	from asset_enterprise.depreciation import enterprise_enabled

	if not gl_entries or not enterprise_enabled():
		return gl_entries

	out = []
	for entry in gl_entries:
		detail = entry.get("voucher_detail_no")
		item_row, asset_field = _asset_line(doc.doctype, detail)
		# Only the leg that lands on the asset account — the credit side
		# (Asset Received But Not Billed) is a payable, not asset value.
		if not item_row or entry.get("account") != item_row.expense_account:
			out.append(entry)
			continue
		assets = _assets_from_line(asset_field, detail)
		if not assets:
			# Nothing to attribute to: an asset line that creates no Asset
			# record (auto_create_assets off — the user makes it by hand),
			# or a return referencing the original line.
			out.append(entry)
			continue
		if len(assets) == 1:
			entry["asset"] = assets[0].name
			out.append(entry)
			continue

		splits = {
			field: _shares(entry.get(field), assets, doc.company)
			for field in _AMOUNT_FIELDS
			if flt(entry.get(field))
		}
		for idx, asset in enumerate(assets):
			row = entry.copy()
			row["asset"] = asset.name
			for field, parts in splits.items():
				row[field] = parts[idx]
			out.append(row)
	return out
