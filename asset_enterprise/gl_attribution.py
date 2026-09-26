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


def dimension_fields(doctype):
	"""Registered accounting dimensions `doctype` can carry.

	`asset` is excluded everywhere this is used: on an Asset Movement row
	that fieldname is the LINK to the asset being moved rather than a
	dimension anyone chose, and on a journal row `stamp_asset_dimension`
	already owns it (GAP-023).

	Cached per request — the answer cannot change inside one, and the
	depreciation timeline asks for it once per schedule row.
	"""
	cache = getattr(frappe.local, "_ae_dimension_fields", None)
	if cache is None:
		cache = frappe.local._ae_dimension_fields = {}
	if doctype not in cache:
		from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
			get_accounting_dimensions,
		)

		meta = frappe.get_meta(doctype)
		cache[doctype] = [
			fieldname
			for fieldname in (get_accounting_dimensions() or [])
			if fieldname != "asset" and meta.has_field(fieldname)
		]
	return cache[doctype]


def acquisition_dimensions(asset_name):
	"""The dimensions the asset was ACQUIRED under.

	The Asset's own dimension fields mean acquisition, in the same sense
	`acquisition_cost_center` does — core's `make_asset` copies them from
	the purchase receipt row, and nothing rewrites them afterwards. A
	transfer deliberately does NOT touch them: where an asset went later
	is the movement trail's answer (`attribution_timeline`), and writing
	it here would destroy the only record of where it started, exactly as
	writing `cost_center` in place did for the centre.
	"""
	fields = dimension_fields("Asset")
	if not fields:
		return {}
	values = frappe.db.get_value("Asset", asset_name, fields, as_dict=True) or {}
	return {field: values.get(field) for field in fields}


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


def control_category_attribution(asset_name):
	"""D-053: (cost centre, dimensions) every posting of a Control Category
	asset carries — its ACQUISITION attribution — or None for any other
	asset.

	A Control Category asset is expensed to whoever bought it (GAP-037):
	its cost and accumulated accounts are Expense-root, so every leg of
	every entry on it is P&L. "Expense follows use" (GAP-021) has nothing
	left to follow once the whole cost sits in the buyer's P&L, and legs
	of one entry split across holders leave one project with the cost and
	another with its reversal (review 2026-09-26 B-2). So the one-day
	charge, its prior-year split, disposal, loss, gain, reversals and
	mirrors all take the acquisition centre and dimensions; a transfer,
	project change or Leave Project records custody only.

	This is the single answer: `attribution_timeline` (and through it
	`attribution_on` / `attribution_split` and every builder that asks
	them), the journal-entry policy below and the core disposal wrapper
	read it. Ordinary assets get None and keep the D-013 rules.
	"""
	from asset_enterprise.depreciation import enterprise_enabled
	from asset_enterprise.overrides.asset_category import is_control_category

	if not asset_name or not enterprise_enabled():
		return None
	category = frappe.db.get_value("Asset", asset_name, "asset_category")
	if not is_control_category(category):
		return None
	return acquisition_cost_center(asset_name), acquisition_dimensions(asset_name)


# Journal entries this app posts as an asset's ACQUISITION — the booking
# of an asset brought in without a purchase document (GAP-001) and the
# booking of a reclassification target (§12.13). Each is recorded as an
# "Addition" Financial Treatment naming that journal entry.
ACQUISITION_JOURNAL_TYPES = ("Existing-Asset Opening", "Reclassification — In")


def acquisition_voucher_sql(gl="gl", asset="ast"):
	"""A boolean SQL expression: GL row `gl` sits on the LIVE voucher that
	acquired asset row `asset` (both are table aliases in the caller's
	query).

	The acquisition is the voucher the asset records, not any entry that
	happens to post the same account and sign:

	    Purchase Receipt / Purchase Invoice   the asset's own purchase link
	    Asset                                 core's booking (CWIP -> FA)
	    Asset Capitalization                  the capitalization targeting it
	    Journal Entry                         the Existing-Asset Opening or
	                                          Reclassification-In booking,
	                                          through its Financial Treatment

	"Live" means the voucher is still submitted (a cancelled receipt's
	rows stay on the ledger under the Immutable Ledger) and, for the
	journal bookings, that the Addition has not been reversed. Anything
	else posting to the asset — reversal mirrors, restore, a sale's
	cancellation rows, a return's regain rows, adjustments, disposals — is
	not on one of these vouchers and so is never the acquisition.

	One indexed probe per branch; the caller bounds the rows it asks
	about. project_accounting reads it for the Control Category source
	rule (D-051 / D-053)."""
	types = ", ".join(frappe.db.escape(t) for t in ACQUISITION_JOURNAL_TYPES)
	return f"""(
		({gl}.voucher_type = 'Purchase Receipt' AND {gl}.voucher_no = {asset}.purchase_receipt
		 AND EXISTS (SELECT 1 FROM `tabPurchase Receipt` acq_pr
		             WHERE acq_pr.name = {gl}.voucher_no AND acq_pr.docstatus = 1))
		OR ({gl}.voucher_type = 'Purchase Invoice' AND {gl}.voucher_no = {asset}.purchase_invoice
		 AND EXISTS (SELECT 1 FROM `tabPurchase Invoice` acq_pi
		             WHERE acq_pi.name = {gl}.voucher_no AND acq_pi.docstatus = 1))
		OR ({gl}.voucher_type = 'Asset' AND {gl}.voucher_no = {asset}.name AND {asset}.docstatus = 1)
		OR ({gl}.voucher_type = 'Asset Capitalization'
		 AND EXISTS (SELECT 1 FROM `tabAsset Capitalization` acq_cap
		             WHERE acq_cap.name = {gl}.voucher_no AND acq_cap.target_asset = {asset}.name
		               AND acq_cap.docstatus = 1))
		OR ({gl}.voucher_type = 'Journal Entry'
		 AND EXISTS (SELECT 1 FROM `tabFinancial Treatment` acq_ft
		             JOIN `tabJournal Entry` acq_je ON acq_je.name = acq_ft.journal_entry
		             WHERE acq_ft.journal_entry = {gl}.voucher_no AND acq_ft.asset = {asset}.name
		               AND acq_ft.transaction_category = 'Addition' AND acq_ft.status = 'Posted'
		               AND acq_ft.transaction_type IN ({types}) AND acq_je.docstatus = 1))
	)"""


def apply_control_attribution(row, attribution):
	"""Put a Control Category asset's acquisition attribution on one leg
	(a JE row or a gl dict). Assigned, not filled — a blank acquisition
	dimension clears an inherited one."""
	centre, dimensions = attribution
	assign = row.__setitem__ if isinstance(row, dict) else row.set
	if centre:
		assign("cost_center", centre)
	for field, value in dimensions.items():
		assign(field, value)


def apply_asset_cost_centre_policy(doc, method=None):
	"""One rule, one place, for every entry the module posts.

	Depreciation, disposal, merge, reclassification and the opening
	booking each build their own journal entry, and each was choosing a
	cost centre for the balance-sheet legs on its own — so the same
	question had five answers, and a sixth posting path would have
	invented a seventh. The rule belongs here, at the choke point that
	already fills the `asset` dimension (GAP-023), not in the builders:

	    a balance-sheet leg carries the asset's ACQUISITION centre
	    AND its acquisition dimensions;
	    the P&L legs keep what the builder computed from movement
	    history (GAP-021 — expense follows use).

	Cost and accumulated depreciation therefore always net to a real
	book value at ONE centre, and no centre ever reports a fixed asset
	it does not hold.

	A Control Category asset (D-053) is the exception: EVERY row carrying
	it takes the acquisition attribution, whatever the account, because
	all of its legs are project P&L (`control_category_attribution`).

	V-08 says "a contra-asset leg carries the dimension of the asset leg
	it contras" — dimension, not cost centre, and the two axes have to
	move together or the invariant holds on one and breaks on the other.
	They did break apart: core's `get_gl_dict` copies the receipt row's
	dimensions onto the Fixed Asset cost leg, while our accumulated-
	depreciation credit carried none, so a project bought an asset and
	then showed its gross cost with no accumulated depreciation against
	it. Both legs now take the same answer from the same place.

	Note what this does NOT do: an asset that joined a project by
	TRANSFER was not acquired under it, so its cost leg carries no
	project and its contra gets none either. Only the expense follows the
	transfer (GAP-021). That is the difference between "who is using
	this" and "whose balance sheet is it on".

	This assigns rather than fills a blank, because it cannot fill one:
	frappe applies the `:Company` default in `_set_defaults()` BEFORE
	`validate` runs (document.py), so by the time any hook sees the row
	the centre is already the company's — which is precisely the defect
	(UAT ACC-ASS-2026-00185 booked its cost to Plant A and every
	depreciation credit to Main, leaving Main 410,000 short of an asset
	it never held).

	What is touched: for an ordinary asset, only rows that name it AND
	land on its own fixed-asset or accumulated-depreciation account; for
	a Control Category asset, EVERY row that names it, whatever the
	account, so a manual journal row naming a control asset has its
	centre and dimensions reassigned too (D-053). Rows that name no asset
	are left exactly as written.
	"""
	# D-026: a reversal must preserve the original attribution verbatim.
	if doc.get("is_reversal") or doc.get("reversal_of"):
		return

	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		return

	category_accounts, centres, dimensions, control_held = {}, {}, {}, {}
	for row in doc.get("accounts") or []:
		asset_name = row.get("asset") or (
			row.get("reference_name") if row.get("reference_type") == "Asset" else None
		)
		if not asset_name or not row.get("account"):
			continue
		# D-053: every leg of a Control Category asset is P&L of the
		# project that bought it, so the rule covers all of its rows —
		# asked of the single answer, not re-derived here.
		if asset_name not in control_held:
			control_held[asset_name] = control_category_attribution(asset_name)
		held = control_held[asset_name]
		if held is not None:
			apply_control_attribution(row, held)
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
		if (
			not aca
			or row.account
			not in (
				aca.fixed_asset_account,
				aca.accumulated_depreciation_account,
			)
		):
			continue  # expense, gain, loss, clearing, suspense — not ours
		if asset_name not in centres:
			# Captured at validate for every asset created since the field
			# existed, so the common path costs nothing beyond the row
			# already read; only pre-field assets pay for a derivation.
			centres[asset_name] = asset.acquisition_cost_center or acquisition_cost_center(
				asset_name
			)
			dimensions[asset_name] = acquisition_dimensions(asset_name)
		if centres[asset_name]:
			row.cost_center = centres[asset_name]
		for field, value in dimensions[asset_name].items():
			# Assigned, not filled: an Accounting Dimension may carry a
			# per-company default that frappe has already put on the row,
			# and a blank acquisition dimension is a real answer — this
			# asset was not bought under a project — so it has to be able
			# to CLEAR an inherited one, not merely decline to set it.
			row.set(field, value)


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
