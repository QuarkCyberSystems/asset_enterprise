"""PR/PI asset flows — GA-0005-01 v2.14 GAP-004 / GAP-012 (Phase 7).

PR side (N1, per 2026-07-14 meeting):
- Asset creation at delivery is core behavior (buying_controller
  auto-creates draft Assets from the PR at the PO/PR rate). We add:
  the `asset_linked` flag on PR rows, the OVER-ALLOCATION block
  (linked assets must not exceed row qty / amount — PR only; PI is
  deliberately uncapped), and a reversal guard (a PR with submitted
  assets cannot be cancelled until those assets are reversed).

PI side (N2/N3):
- PI Asset Allocation (1:1): the user picks WHICH received Assets a
  (partial) invoice covers. Validations: assets must belong to a PR
  referenced by this PI; an asset already covered by a submitted PI
  allocation cannot be re-selected.
- Invoice Adjustment ± : per allocated asset the PI-vs-PR rate delta
  decomposes into price delta (capitalized via auto-AVA with
  transaction_type="Invoice Adjustment"; the AVA difference account is
  the Asset Invoice Difference / Clearing chain account) and FX delta
  (Exchange Gain/Loss — never capitalized). Both directions flow per
  the client ASSET_MVT matrix rows 10-14.
- `warn_invoice_below_receipt` (Asset Settings, DEFAULT ON — GAP-012
  Option B per finance 23/07/2026): a PI priced below the PR raises a
  warning the user acknowledges; both directions then flow.

PI cancel: the auto-AVAs are cancelled, which flows through the
Phase 4 Reversal-AVA path — deltas unwind via counter-documents.
"""

import frappe
from frappe import _
from frappe.utils import flt

from asset_enterprise.rounding import fa_module_round


def _enterprise():
	from asset_enterprise.depreciation import enterprise_enabled

	return enterprise_enabled()


# ---------------------------------------------------------------- PR side


def pr_on_submit(doc, method=None):
	if not _enterprise():
		return
	for row in doc.items:
		linked = frappe.get_all(
			"Asset", filters={"purchase_receipt_item": row.name, "docstatus": ["<", 2]}, pluck="name"
		)
		if linked:
			frappe.db.set_value(
				"Purchase Receipt Item", row.name, "asset_linked", 1, update_modified=False
			)
			_validate_pr_over_allocation(row, linked)


def _validate_pr_over_allocation(row, linked_assets):
	"""GAP-004 / N1: linked assets must not exceed the PR row (PR only)."""
	if len(linked_assets) > (row.qty or 0):
		frappe.throw(
			_(
				"Purchase Receipt row {0}: {1} assets linked but qty is {2} — "
				"over-allocation on PR is blocked (GAP-004)."
			).format(row.idx, len(linked_assets), row.qty)
		)
	linked_value = flt(
		frappe.db.sql(
			"select coalesce(sum(net_purchase_amount), 0) from `tabAsset` where purchase_receipt_item = %s and docstatus < 2",
			row.name,
		)[0][0]
	)
	if linked_value > flt(row.base_net_amount) + 0.01:
		frappe.throw(
			_(
				"Purchase Receipt row {0}: linked asset value {1} exceeds row amount {2} — "
				"over-allocation on PR is blocked (GAP-004)."
			).format(row.idx, linked_value, row.base_net_amount)
		)


def pr_before_cancel(doc, method=None):
	"""A PR whose assets are already submitted cannot be cancelled —
	reverse the assets first (immutable-ledger ordering)."""
	if not _enterprise():
		return
	submitted = frappe.get_all(
		"Asset",
		filters={"purchase_receipt": doc.name, "docstatus": 1},
		pluck="name",
	)
	if submitted:
		frappe.throw(
			_(
				"Purchase Receipt {0} has submitted Assets ({1}). Reverse those assets "
				"first (per GA-0001-01 ordering), then cancel the receipt."
			).format(doc.name, ", ".join(submitted))
		)


# ---------------------------------------------------------------- SI side


def si_validate(doc, method=None):
	"""GAP-010 / VR-011 (Phase 11 F7): the SALE disposal path honors
	the prevent-disposal-before-full-invoicing control too."""
	if not _enterprise():
		return
	from asset_enterprise.disposal import assert_fully_invoiced

	for row in doc.items:
		if row.get("asset"):
			asset = frappe.db.get_value(
				"Asset",
				row.asset,
				["name", "purchase_receipt", "purchase_invoice"],
				as_dict=True,
			)
			if asset:
				assert_fully_invoiced(asset)


# ---------------------------------------------------------------- PI side


def pi_validate(doc, method=None):
	if not _enterprise() or not doc.get("pi_asset_allocation"):
		return

	referenced_prs = {row.purchase_receipt for row in doc.items if row.get("purchase_receipt")}
	seen = set()
	for row in doc.pi_asset_allocation:
		if row.asset in seen:
			frappe.throw(_("Asset {0} is allocated twice on this invoice.").format(row.asset))
		seen.add(row.asset)

		asset = frappe.db.get_value(
			"Asset",
			row.asset,
			["purchase_receipt", "purchase_receipt_item", "docstatus"],
			as_dict=True,
		)
		if not asset or asset.docstatus == 2:
			frappe.throw(_("Asset {0} is not available for allocation.").format(row.asset))
		if referenced_prs and asset.purchase_receipt not in referenced_prs:
			frappe.throw(
				_(
					"Asset {0} belongs to Purchase Receipt {1}, which this invoice does "
					"not reference."
				).format(row.asset, asset.purchase_receipt or _("(none)"))
			)

		# Fully-invoiced assets cannot be re-selected. Per the 2026-07-23
		# review this is a QUANTITY rule — value differences never block.
		# With 1:1 asset allocation, qty-covered == one submitted PI
		# allocation per asset.
		prior = frappe.db.sql(
			"""
			select paa.parent from `tabPI Asset Allocation` paa
			join `tabPurchase Invoice` pi on pi.name = paa.parent
			where paa.asset = %s and pi.docstatus = 1 and pi.name != %s
			limit 1
			""",
			(row.asset, doc.name),
		)
		if prior:
			frappe.throw(
				_(
					"Asset {0} is already covered by submitted invoice {1} — "
					"fully-invoiced assets cannot be re-selected (GAP-012)."
				).format(row.asset, prior[0][0])
			)

	_maybe_warn_below_receipt(doc)


def _maybe_warn_below_receipt(doc):
	"""GAP-012 Option B (finance decision 23/07/2026): a PI priced below
	the PR raises a WARNING the user acknowledges — never a block. Both
	GL directions then flow per the ASSET_MVT matrix."""
	if not frappe.db.get_single_value("Asset Settings", "warn_invoice_below_receipt", cache=False):
		return
	for row in doc.pi_asset_allocation:
		price_delta, _fx = _compute_deltas(doc, row.asset)
		if price_delta < 0:
			frappe.msgprint(
				_(
					"Invoice prices Asset {0} below its receipt value (delta {1}). "
					"Proceeding will post an Invoice Adjustment Decrease per the "
					"ASSET_MVT matrix (Option B, finance 23/07/2026)."
				).format(row.asset, price_delta),
				title=_("Invoice Below Receipt Amount"),
				indicator="orange",
			)


def pi_on_submit(doc, method=None):
	if not _enterprise() or not doc.get("pi_asset_allocation"):
		return

	from asset_enterprise.accounts import get_enterprise_account

	for row in doc.pi_asset_allocation:
		price_delta, fx_delta = _compute_deltas(doc, row.asset)
		frappe.db.set_value(
			"PI Asset Allocation",
			row.name,
			{"pi_delta_amount": price_delta, "fx_delta_amount": fx_delta},
			update_modified=False,
		)

		asset = frappe.db.get_value(
			"Asset", row.asset, ["docstatus", "asset_category", "company"], as_dict=True
		)
		if not price_delta or asset.docstatus != 1:
			continue  # draft assets absorb the delta on their own submit values

		# Auto-AVA sweeps the price delta onto the asset (Case A.01);
		# the clearing account is the AVA difference account, matching
		# §12.4/§12.5 Invoice Adjustment Increase / Decrease.
		clearing = get_enterprise_account(
			"asset_invoice_difference_account", asset.company, asset.asset_category
		)
		from asset_enterprise.asset_values import recalculate_asset_values

		current = recalculate_asset_values(row.asset, save=False)["net_book_value"]
		ava = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment",
				"asset": row.asset,
				"company": doc.company,
				"date": doc.posting_date,
				"transaction_type": "Invoice Adjustment",
				"current_asset_value": current,
				"new_asset_value": fa_module_round(current + price_delta, doc.company),
				"difference_account": clearing,
				"cost_center": frappe.db.get_value("Asset", row.asset, "cost_center"),
			}
		)
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		row.db_set("purchase_receipt", frappe.db.get_value("Asset", row.asset, "purchase_receipt"))
		doc.add_comment(
			"Comment",
			_("Invoice Adjustment AVA {0} posted for Asset {1} (delta {2}).").format(
				ava.name, row.asset, price_delta
			),
		)


def pi_on_cancel(doc, method=None):
	"""Cancel the auto-AVAs — each flows through the Phase 4
	Reversal-AVA path, unwinding the delta via counter-documents."""
	if not _enterprise():
		return
	for ava_name in frappe.get_all(
		"Asset Value Adjustment",
		filters={
			"transaction_type": "Invoice Adjustment",
			"docstatus": 1,
			"date": doc.posting_date,
			"asset": ["in", [r.asset for r in doc.get("pi_asset_allocation") or []]],
		},
		pluck="name",
	):
		frappe.get_doc("Asset Value Adjustment", ava_name).cancel()


def _compute_deltas(doc, asset_name):
	"""(price_delta, fx_delta) in company currency for one asset (N3).

	price delta = (PI rate − PR rate, supplier currency) × PR exchange
	fx delta    = PI rate × (PI exchange − PR exchange)
	Together they sum to the base-currency difference.
	"""
	asset = frappe.db.get_value(
		"Asset", asset_name, ["purchase_receipt", "purchase_receipt_item"], as_dict=True
	)
	if not asset or not asset.purchase_receipt_item:
		return 0.0, 0.0

	pr_item = frappe.db.get_value(
		"Purchase Receipt Item",
		asset.purchase_receipt_item,
		["net_rate", "base_net_rate", "parent"],
		as_dict=True,
	)
	pi_item = None
	for row in doc.items:
		if row.get("pr_detail") == asset.purchase_receipt_item:
			pi_item = row
			break
	if not pr_item or not pi_item:
		return 0.0, 0.0

	pr_conversion = flt(
		frappe.db.get_value("Purchase Receipt", pr_item.parent, "conversion_rate") or 1
	)
	pi_conversion = flt(doc.conversion_rate or 1)

	price_delta = fa_module_round(
		(flt(pi_item.net_rate) - flt(pr_item.net_rate)) * pr_conversion, doc.company
	)
	fx_delta = fa_module_round(
		flt(pi_item.net_rate) * (pi_conversion - pr_conversion), doc.company
	)
	return price_delta, fx_delta
