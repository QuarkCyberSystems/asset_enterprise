# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""This app's rules on the platform's sockets in core's shared purchase and
journal classes (qcs_platform Build 0.2 step 4, §4.10 / §4.12; N-4 RULED
(a)). Until step 4 these were wraps this app installed on the core classes
at package import (`overrides/patches.py`); the classes' overrides are the
platform's, so the platform owns the methods and asks these functions
through the hooks of the same name (hooks.py). The registration requires
the sockets (platform.py): while one is unhealthy the platform
refuses the postings that rely on it (`capability_uses`), and the migrate
fails.

Every rule applies only while Enterprise Assets is enabled; with it off a
site behaves exactly like core.
"""

import frappe
from frappe import _

from asset_enterprise.depreciation import enterprise_enabled


def attribute_purchase_gl(doc, gl_entries):
	"""`purchase_gl_post_processors` (GAP-006 / §5.1, audit C1): core books an
	asset purchase with neither the `asset` dimension nor against_voucher,
	and writes ONE row per receipt LINE - so a qty-4 line books a single
	amount for four assets and the acquisition cost cannot be attributed in
	the ledger. Stamp the leg, splitting it when a line made several assets
	(see gl_attribution for the rules).

	BuyingController.on_submit -> process_fixed_asset() creates the assets
	BEFORE make_gl_entries() runs on both doctypes, so the mapping is exact
	rather than a guess. The platform runs this after core's process_gl_map
	- the position the wrap had; merge_similar_entries keys on accounting
	dimensions, so rows for different assets never merge back."""
	from asset_enterprise.gl_attribution import attribute_asset_legs

	return attribute_asset_legs(doc, gl_entries)


def keep_receipt_assets(doc, field):
	"""`receipt_asset_delete_policy` (GAP-004.4): cancelling a receipt must
	REVERSE the assets it created, never destroy them. Core does the
	opposite - `update_fixed_asset(delete_asset=True)` calls
	frappe.delete_doc("Asset", ..., force=1) for every auto-created asset,
	wiping the record and its movements whatever its docstatus, so the
	reversal `invoice_diff.pr_before_cancel` had just performed vanished
	along with the asset (client, 24/08). Assets already cancelled are
	skipped by core's own loop once deletion is off."""
	from qcs_platform.core.sockets import KEEP

	return KEEP if enterprise_enabled() else None


def reverse_original_assets(doc, original):
	"""`receipt_reversal_assets` (GAP-004.4, D-095): a receipt reversed by its
	Cancellation document - ERPNext's own return of it, which is how a
	receipt carrying periodic-valuation rows is reversed - reverses the
	assets it created exactly as its native cancel would, as the
	Cancellation is submitted. Core's return refuses while those assets
	stand; the platform asks this first.

	Only the assets of the rows the Cancellation carries are reversed, and
	such a row is reversed whole: which of a row's assets a reduced quantity
	would give back is not the document's to say. An asset linked to the
	receipt without a row (`purchase_receipt_item` is stamped by core's
	`make_asset` only) goes with the whole receipt, as on native cancel; a
	Cancellation that leaves rows out is refused naming it. One that leaves
	an asset row out is refused by core's own check behind this one."""
	if not enterprise_enabled():
		return
	import frappe
	from frappe import _
	from frappe.utils import flt

	from asset_enterprise.invoice_diff import reverse_receipt_assets

	carried = {r.purchase_receipt_item: r for r in doc.get("items") or [] if r.get("purchase_receipt_item")}
	original_qty = {r.name: flt(r.qty) for r in frappe.get_all(
		"Purchase Receipt Item", filters={"parent": original, "parenttype": "Purchase Receipt"}, fields=["name", "qty"])}
	standing = frappe.get_all("Asset", filters={"purchase_receipt": original, "docstatus": 1},
		fields=["name", "purchase_receipt_item"])
	if not standing:
		return
	reversing = []
	for row_name in sorted({a.purchase_receipt_item for a in standing if a.purchase_receipt_item in carried}):
		row = carried[row_name]
		if abs(flt(row.qty)) != original_qty.get(row_name):
			frappe.throw(
				_("Row {0}: {1} created assets on {2}; its reversal must carry the whole row ({3}), not {4}.").format(
					row.idx, row.item_code, original, original_qty.get(row_name), abs(flt(row.qty))),
				title=_("Asset Row Reversed Whole"),
			)
		reversing.extend(a.name for a in standing if a.purchase_receipt_item == row_name)
	unrowed = [a.name for a in standing if a.purchase_receipt_item not in original_qty]
	if unrowed:
		if set(original_qty) - set(carried):
			frappe.throw(
				_("{0} {1} linked to {2} without a receipt row; they are reversed only with the whole receipt, "
					"and this reversal leaves rows of it out.").format(
					_("Asset") if len(unrowed) == 1 else _("Assets"), ", ".join(unrowed), original),
				title=_("Asset Reversed With the Whole Receipt"),
			)
		reversing.extend(unrowed)
	if reversing:
		reverse_receipt_assets(original, assets=reversing)


def skip_depreciation_link_guess(doc, asset, je_row):
	"""`depreciation_journal_link_policy` (GAP-031 / §4.6): core GUESSES which
	schedule row a depreciation JE belongs to - it matches on
	`schedule_date == posting_date` AND an equal amount - because core only
	ever posts a row on its own schedule date. This app's engine does not: a
	mass run posts several periods at ONE posting date (§4.6 makes that date
	a month end), and the scheduler, the final-row path and the
	immediate-charge path all post rows dated differently from the entry.

	When the guess lands on a DIFFERENT row than the one being posted, that
	row is silently marked as posted with another period's journal entry:
	the asset then reads more accumulated depreciation than the ledger
	holds, and the row can never be posted for real (reproduced on dev - a
	four-period run stamped the 31/08 row with the 31/05 entry, derived
	accum 12,300.00 against 9,200.00 of GL).

	`_post_one` stamps the row it is actually posting, so under Enterprise
	Assets core's guess is never needed and can only ever be wrong. Manual
	Depreciation-Entry JEs are deliberately left unlinked too - GAP-006
	counts them through the manual-GL sweep, and letting one claim a
	schedule row would mark a period posted with no Financial Treatment
	behind it."""
	from qcs_platform.core.sockets import SKIP

	return SKIP if enterprise_enabled() else None


# ------------------------------------------ which postings rely on a socket


def _asset_rows(doc):
	return any(row.get("is_fixed_asset") for row in doc.get("items") or [])


def purchase_with_asset_rows(doc, event=None):
	"""A receipt or invoice with a fixed-asset row: its GL carries asset
	legs the purchase GL socket attributes (submit), and its cancel asks the
	receipt-asset socket whether core may delete the assets (cancel; core
	skips returns)."""
	if event == "cancel" and doc.get("is_return"):
		return False
	return _asset_rows(doc) and enterprise_enabled()


def receipt_cancellation_with_asset_rows(doc, event=None):
	"""A receipt's Cancellation document with a fixed-asset row: its submit
	reverses the original's assets through the receipt-reversal socket."""
	return bool(doc.get("is_cancellation") and doc.get("cancellation_against")) and purchase_with_asset_rows(doc, event)


def depreciation_journal(doc, event=None):
	"""A Depreciation Entry journal with an asset row: core links it to a
	schedule row on submit (`update_asset_on_depreciation`)."""
	return (
		doc.get("voucher_type") == "Depreciation Entry"
		and any(row.get("reference_type") == "Asset" and row.get("reference_name") for row in doc.get("accounts") or [])
		and enterprise_enabled()
	)


def enterprise_site(doc=None, event=None):
	"""Any document of the rule's doctypes on an Enterprise Assets site:
	- a receipt or invoice - `consumption` reads every invoice line by its
	  GL row's voucher row (P8), whatever the row is (end review 0.2 M-1);
	- a repost - it rebuilds the GL of every voucher its walk reaches
	  through that voucher's `get_gl_entries`, and which vouchers it
	  reaches is not known at its submit."""
	return enterprise_enabled()


def landed_cost_rebuilds_gl(doc=None, event=None):
	"""A Landed Cost Voucher that rebuilds its receipts' GL inside its own
	submit or cancel: Immutable Ledger off (upstream `update_landed_cost`
	-> `make_gl_entries`, through `get_gl_entries` and `get_gl_dict`) on
	an Enterprise Assets site. With the ledger on it creates a repost,
	which is gated at its own submit and start (end review 0.2 S-1)."""
	from qcs_platform.core.guards import is_immutable_ledger_enabled

	return enterprise_enabled() and not is_immutable_ledger_enabled()


def basis_correction(action, pre, destination, delta, clearing_account):
	"""Platform `basis_correction_owners` (GA-0009-01 GAP-054; manual §9.8):
	a non-monetary advance applied to an invoice slice that acquired an asset
	corrects the asset's acquisition cost - through this module, never a
	journal. A decrease is the asset's own reduction for another module's
	document (`api.reduce_asset_value`, D-081: absorbed on the register, the
	rest by the post-disposal rule); its reversal undoes exactly that. An
	increase has no ruled mechanics yet (G05) and is refused by name."""
	if destination.get("kind") != "Asset" or not enterprise_enabled():
		return None
	from frappe.utils import flt

	from asset_enterprise import api

	if action == "reverse":
		undone = api.undo_asset_value_reduction(pre.doctype, pre.reversal_of, pre.posting_date)
		return ("Asset Value Adjustment", undone.reversed[0]) if undone.reversed else ("Asset", destination["assets"][0])
	if flt(delta) > 0:
		frappe.throw(_("NONMONETARY_ROUTE_UNRULED: an asset basis increase of {0} has no ruled mechanics (G05).").format(delta),
			title=_("Basis Rule Not Ruled"))
	assets = destination.get("assets") or []
	if not assets:
		frappe.throw(_("The invoice slice {0} names no asset to correct.").format(pre.origin_slice))
	share = abs(flt(delta)) / len(assets)
	first = None
	for i, asset in enumerate(assets):
		amount = abs(flt(delta)) - share * (len(assets) - 1) if i == len(assets) - 1 else share
		done = api.reduce_asset_value(asset, amount, pre.posting_date, clearing_account, pre.doctype, pre.name,
			label=pre.origin_slice, dimensions=destination.get("dimensions"))
		first = first or (("Asset Value Adjustment", done.ava) if done.ava else ("Journal Entry", done.journal_entry))
	return first
