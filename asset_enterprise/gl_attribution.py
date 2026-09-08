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


def acquisition_cost_center(asset, persist=False):
	"""The centre the asset's GROSS COST sits in — fixed for its life.

	GAP-021 attributes depreciation EXPENSE to the centre that held the
	asset during the period: expense answers "who consumed it". The
	balance sheet answers a different question — "what is this asset
	carried at" — and is attributed differently:

	    a contra-asset leg carries the dimension of the asset leg it
	    contras.

	So the Fixed Asset and Accumulated Depreciation legs both take the
	acquisition centre and net to a real book value there, while the
	expense legs follow the movement history. Attributing the contra any
	other way strands it: an asset bought under Plant A and transferred
	to HQ books its cost at Plant A and its accumulated depreciation
	somewhere else, so HQ reports a negative fixed asset and Plant A an
	overstated one — company totals stay right, which is why nothing
	catches it, but no centre's balance sheet reconciles.

	`Asset.cost_center` is NOT this value: Asset Movement mutates it in
	place on transfer (overrides/asset_movement.py). Reading it here
	would reintroduce the defect silently, with no test failure — hence
	the captured field, derived once for assets that predate it.

	`persist` is off by default because the callers are mostly READ
	paths — the whitelisted movement preview, the attribution timeline,
	reports — and a read that writes turns a derivation made under
	incomplete information into a permanent fact. Capture belongs to the
	paths that own the moment the answer is known: `EnterpriseAsset
	.validate`, the movement's own `on_submit` (before it overwrites
	`cost_center`), and `repair.backfill_acquisition_cost_center`.
	"""
	if isinstance(asset, str):
		asset = frappe.get_doc("Asset", asset)
	captured = asset.get("acquisition_cost_center")
	if captured:
		return captured
	derived = _derive_acquisition_cost_center(asset)
	if derived and persist and not asset.get("__islocal"):
		asset.db_set("acquisition_cost_center", derived, update_modified=False)
	return derived


def _has_moved(asset_name):
	"""Whether a submitted transfer has already rewritten this asset's
	`cost_center`. Once one has, that field is the CURRENT centre and can
	no longer stand in for the acquisition one."""
	return bool(
		frappe.db.sql(
			"""select 1
			   from `tabAsset Movement Item` ami
			   join `tabAsset Movement` am on am.name = ami.parent
			   where am.docstatus = 1 and ami.asset = %s
			     and ifnull(ami.target_cost_center, '') <> ''
			   limit 1""",
			asset_name,
		)
	)


def _derive_acquisition_cost_center(asset):
	"""Where the gross cost actually landed, for an asset capitalized
	before the field existed. The ledger is the authority; the movement
	trail and the current field are fallbacks for assets whose cost leg
	we cannot see (no GL, or a core-posted leg with no dimension)."""
	fa_account = frappe.db.get_value(
		"Asset Category Account",
		{"parent": asset.asset_category, "company_name": asset.company},
		"fixed_asset_account",
	)
	if fa_account:
		booked = frappe.db.sql(
			"""select cost_center from `tabGL Entry`
			   where is_cancelled = 0 and asset = %s and account = %s
			   order by posting_date, creation limit 1""",
			(asset.name, fa_account),
		)
		if booked and booked[0][0]:
			return booked[0][0]
	# No attributable cost leg: the earliest transfer records what the
	# asset was moved AWAY from, which is where it was acquired.
	moved_from = frappe.db.sql(
		"""select ami.source_cost_center
		   from `tabAsset Movement Item` ami
		   join `tabAsset Movement` am on am.name = ami.parent
		   where am.docstatus = 1 and ami.asset = %s
		     and ifnull(ami.source_cost_center, '') <> ''
		   order by am.transaction_date, am.creation limit 1""",
		asset.name,
	)
	if moved_from and moved_from[0][0]:
		return moved_from[0][0]
	if _has_moved(asset.name):
		# Nothing recorded where the asset started, and its own field no
		# longer says: a transfer overwrote it with the target. Returning
		# it here would name the receiving centre as the ACQUISITION one
		# — the asset would book its cost, its accumulated depreciation
		# and its pre-transfer expense to a centre it joined later, and
		# the captured field would make that permanent (reproduced on a
		# legacy-shaped asset: acquisition_cost_center came back as the
		# transfer target). The company default is where such an asset's
		# earlier depreciation demonstrably posted, frappe having filled
		# the blank leg with it at `_set_defaults`.
		from erpnext import get_default_cost_center

		return get_default_cost_center(asset.get("company"))
	# Never moved, so the current field is still the acquisition centre.
	return asset.get("cost_center")


def apply_asset_cost_centre_policy(doc, method=None):
	"""One rule, one place, for every entry the module posts.

	Depreciation, disposal, merge, reclassification and the opening
	booking each build their own journal entry, and each was choosing a
	cost centre for the balance-sheet legs on its own — so the same
	question had five answers, and a sixth posting path would have
	invented a seventh. The rule belongs here, at the choke point that
	already fills the `asset` dimension (GAP-023), not in the builders:

	    a balance-sheet leg carries the asset's ACQUISITION centre;
	    the P&L legs keep what the builder computed from movement
	    history (GAP-021 — expense follows use).

	Cost and accumulated depreciation therefore always net to a real
	book value at ONE centre, and no centre ever reports a fixed asset
	it does not hold.

	This assigns rather than fills a blank, because it cannot fill one:
	frappe applies the `:Company` default in `_set_defaults()` BEFORE
	`validate` runs (document.py), so by the time any hook sees the row
	the centre is already the company's — which is precisely the defect
	(UAT ACC-ASS-2026-00185 booked its cost to Plant A and every
	depreciation credit to Main, leaving Main 410,000 short of an asset
	it never held). Only rows that name an Asset AND land on that
	asset's own fixed-asset or accumulated-depreciation account are
	touched; everything else, including a manual entry elsewhere in the
	chart, is left exactly as written.
	"""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		return

	category_accounts, centres = {}, {}
	for row in doc.get("accounts") or []:
		asset_name = row.get("asset") or (
			row.get("reference_name") if row.get("reference_type") == "Asset" else None
		)
		if not asset_name or not row.get("account"):
			continue
		asset = frappe.db.get_value(
			"Asset",
			asset_name,
			["name", "asset_category", "company", "cost_center", "acquisition_cost_center"],
			as_dict=True,
		)
		if not asset:
			continue
		key = (asset.asset_category, asset.company)
		if key not in category_accounts:
			category_accounts[key] = frappe.db.get_value(
				"Asset Category Account",
				{"parent": asset.asset_category, "company_name": asset.company},
				["fixed_asset_account", "accumulated_depreciation_account"],
				as_dict=True,
			)
		aca = category_accounts[key]
		if not aca or row.account not in (
			aca.fixed_asset_account,
			aca.accumulated_depreciation_account,
		):
			continue  # expense, gain, loss, clearing, suspense — not ours
		if asset_name not in centres:
			# Captured at validate for every asset created since the field
			# existed, so the common path costs nothing beyond the row
			# already read; only pre-field assets pay for a derivation.
			centres[asset_name] = asset.acquisition_cost_center or acquisition_cost_center(
				asset_name
			)
		if centres[asset_name]:
			row.cost_center = centres[asset_name]


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
