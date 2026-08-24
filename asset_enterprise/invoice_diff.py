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


def stamp_asset_dimension(doc, method=None):
	"""GAP-023 / TC-038: with "Asset" registered as an Accounting
	Dimension, GL rows can be filtered and grouped by asset — but only
	if the dimension field is actually filled. Every asset journal entry
	already names its asset in reference_type/reference_name, so copy it
	across rather than touching each builder."""
	if not frappe.get_meta("Journal Entry Account").has_field("asset"):
		return
	for row in doc.get("accounts") or []:
		if row.get("asset"):
			continue
		if row.get("reference_type") != "Asset" or not row.get("reference_name"):
			continue
		# A merge or reversal posts against an asset that is on its way to
		# docstatus 2; the dimension is a Link field and frappe refuses to
		# link a cancelled document, so leave those rows alone.
		if frappe.db.get_value("Asset", row.reference_name, "docstatus") == 2:
			continue
		row.asset = row.reference_name


def pr_on_submit(doc, method=None):
	"""Sheet item 2 (client, 16/08/2026): the assets a receipt creates are
	submitted for the user. ERPNext locks Available for Use Date and
	Calculate Depreciation after submit, so depreciation is switched on
	afterwards through Enable Depreciation — which now takes its basis
	from the asset's in-service date, keeping the catch-up intact."""
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
			_stamp_receiving_date_basis(doc, linked)
			_submit_receipt_assets(doc, linked)


def _stamp_receiving_date_basis(pr_doc, asset_names):
	"""GAP-003: when the category starts depreciation from the receiving
	date, fill the draft asset's Available-for-use Date at creation.

	The rule already ran at validate, but core creates these assets
	during the receipt's own submit, so the draft the user opens showed
	an empty date and looked like the setting did nothing (client
	finding 16/08/2026, item 2)."""
	for name in asset_names:
		asset = frappe.db.get_value(
			"Asset", name, ["available_for_use_date", "asset_category", "docstatus"], as_dict=True
		)
		if not asset or asset.available_for_use_date or asset.docstatus != 0:
			continue
		if not frappe.db.get_value(
			"Asset Category", asset.asset_category, "calculate_from_receiving_date"
		):
			continue
		frappe.db.set_value(
			"Asset", name, "available_for_use_date", pr_doc.posting_date, update_modified=False
		)


def _submit_receipt_assets(pr_doc, asset_names):

	"""Submit the assets the receipt just created (client decision
	16/08/2026, sheet item 2). Depreciation can still be switched on
	afterwards via Enable Depreciation, so the draft step only cost the
	user a click per asset.

	Per GAP-002 the available-for-use date is needed only when the asset
	depreciates, so a non-depreciating asset submits without one. Only a
	DEPRECIATING asset with no date waits as a draft — that date cannot
	be set after submit, so submitting would trap it. The receipt itself
	never fails because one asset needs a decision.
	"""
	for name in asset_names:
		state = frappe.db.get_value(
			"Asset", name, ["docstatus", "available_for_use_date", "calculate_depreciation"],
			as_dict=True,
		)
		if not state or state.docstatus != 0:
			continue
		# GAP-002: the date is required only when the asset DEPRECIATES and
		# its category is not on the receiving-date basis. A
		# non-depreciating asset submits without one quite legitimately
		# (TC-003), so only a depreciating asset with no date has to wait
		# — for that one the date cannot be set after submit, which would
		# trap it.
		if not state.available_for_use_date and state.calculate_depreciation:
			continue
		frappe.db.savepoint("ae_asset_submit")
		try:
			asset = frappe.get_doc("Asset", name)
			asset.flags.ignore_permissions = True
			asset.submit()
		except Exception as e:
			frappe.db.rollback(save_point="ae_asset_submit")
			frappe.msgprint(
				_(
					"Asset {0} was created as a draft — it needs attention before it can "
					"be submitted: {1}"
				).format(name, str(e)[:160]),
				title=_("Asset Left as Draft"),
				indicator="orange",
			)



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


def _uncovered_assets_for_pr_row(pr_detail, exclude_pi=None):
	"""Assets created from a PR row that no submitted PI allocation
	already covers (the qty-only "fully invoiced" rule, CH-04)."""
	assets = frappe.get_all(
		"Asset",
		filters={"purchase_receipt_item": pr_detail, "docstatus": ["<", 2]},
		order_by="creation asc",
		pluck="name",
	)
	if not assets:
		return []
	covered = {
		row[0]
		for row in frappe.db.sql(
			"""
			select paa.asset from `tabPI Asset Allocation` paa
			join `tabPurchase Invoice` pi on pi.name = paa.parent
			where paa.asset in %(assets)s and pi.docstatus = 1
			  and pi.name != %(pi)s
			""",
			{"assets": assets, "pi": exclude_pi or ""},
		)
	}
	return [name for name in assets if name not in covered]


def autofill_asset_allocation(doc):
	"""GAP-012: the allocation table exists to disambiguate PARTIAL
	invoices (design §GAP-012 N2) — it is not a switch that turns the
	invoice-difference treatment on. For an ordinary invoice covering
	the whole receipt row, resolve the assets automatically so the
	delta routes without the user having to know the table exists.

	Only a genuinely ambiguous partial invoice (more uncovered assets
	on the PR row than this invoice's qty) still needs a manual pick —
	and then we say so instead of silently doing nothing.
	"""
	if doc.get("pi_asset_allocation"):
		return  # an explicit selection always wins

	from frappe.utils import cint

	resolved, ambiguous = [], []
	for item in doc.items:
		if not item.get("is_fixed_asset") or not item.get("pr_detail"):
			continue
		candidates = _uncovered_assets_for_pr_row(item.pr_detail, exclude_pi=doc.name)
		if not candidates:
			continue
		needed = max(1, cint(item.qty))
		if len(candidates) <= needed:
			resolved.extend(candidates)
		else:
			ambiguous.append((item, candidates, needed))

	if ambiguous:
		item, candidates, needed = ambiguous[0]
		frappe.throw(
			_(
				"Row {0}: this invoice covers {1} of the {2} assets still uninvoiced "
				"on receipt row {3}. Pick which ones under <b>Asset Allocation</b> "
				"so the invoice-vs-receipt difference lands on the right assets "
				"(GAP-012)."
			).format(item.idx, needed, len(candidates), item.get("purchase_receipt") or ""),
			title=_("Select the Assets this Invoice Covers"),
		)

	for asset_name in resolved:
		doc.append(
			"pi_asset_allocation",
			{
				"asset": asset_name,
				"purchase_receipt": frappe.db.get_value("Asset", asset_name, "purchase_receipt"),
			},
		)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def allocatable_assets(doctype, txt, searchfield, start, page_len, filters):
	"""Link query for PI Asset Allocation → Asset: only assets received
	on a PR this invoice references, minus the ones a submitted invoice
	already covers (design §GAP-012 — "the Asset lookup filters out
	fully-invoiced Assets")."""
	invoice = (filters or {}).get("purchase_invoice")
	pr_details = frappe.get_all(
		"Purchase Invoice Item",
		filters={"parent": invoice, "is_fixed_asset": 1},
		pluck="pr_detail",
	)
	names = []
	for pr_detail in filter(None, pr_details):
		names.extend(_uncovered_assets_for_pr_row(pr_detail, exclude_pi=invoice))
	if not names:
		return []
	return frappe.db.sql(
		"""
		select name, asset_name from `tabAsset`
		where name in %(names)s and (name like %(txt)s or asset_name like %(txt)s)
		order by name limit %(start)s, %(page_len)s
		""",
		{
			"names": names,
			"txt": f"%{txt or ''}%",
			"start": start or 0,
			"page_len": page_len or 20,
		},
	)


def pi_validate(doc, method=None):
	if not _enterprise():
		return
	autofill_asset_allocation(doc)
	if not doc.get("pi_asset_allocation"):
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


# Case A.02 routing: an asset that has left the register cannot take a
# value adjustment, so its invoice difference is EXPENSED. "Cancelled"
# belongs here for the same reason the others do — a reclassification
# source now carries it (client, 24/08) and disposal.py has always
# listed it.
DISPOSED_STATUSES = ("Scrapped", "Sold", "Capitalized", "Cancelled")


def pi_on_submit(doc, method=None):
	if not _enterprise() or not doc.get("pi_asset_allocation"):
		return

	from asset_enterprise.accounts import get_enterprise_account

	transfer_legs = []  # Phase 11c D1: delta moves OUT of ARBNB at PI time

	for row in doc.pi_asset_allocation:
		price_delta, fx_delta = _compute_deltas(doc, row.asset)
		frappe.db.set_value(
			"PI Asset Allocation",
			row.name,
			{"pi_delta_amount": price_delta, "fx_delta_amount": fx_delta},
			update_modified=False,
		)

		asset = frappe.db.get_value(
			"Asset", row.asset, ["docstatus", "asset_category", "company", "status"], as_dict=True
		)
		if asset.docstatus != 1:
			continue  # draft assets absorb the delta on their own submit values

		# §12.4/§12.5 (Phase 11c D1): the PI parked the full amount in
		# ARBNB — move the price delta to its destination account, and
		# the FX component to Exchange Gain/Loss (never capitalized).
		if asset.status in DISPOSED_STATUSES:
			# Case A.02: disposed asset — delta is EXPENSED, no AVA.
			if price_delta:
				dest = get_enterprise_account(
					"post_disposal_invoice_diff_account", asset.company, asset.asset_category
				)
				transfer_legs.append((dest, price_delta, row.asset))
				from asset_enterprise import tcc

				tcc.apply(
					source_doc=doc,
					category="Addition",
					transaction_type="Post-Disposal Invoice Adjustment",
					asset=row.asset,
					posting_date=doc.posting_date,
					amount=abs(price_delta),
				)
			if fx_delta:
				transfer_legs.append((_exchange_account(doc.company), fx_delta, row.asset))
			continue

		if fx_delta:
			transfer_legs.append((_exchange_account(doc.company), fx_delta, row.asset))
		if not price_delta:
			continue
		transfer_legs.append(
			(
				get_enterprise_account(
					"asset_invoice_difference_account", asset.company, asset.asset_category
				),
				price_delta,
				row.asset,
			)
		)

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
		# VR-005 (Phase 11b): flag the PI item row covering this asset.
		pr_detail = frappe.db.get_value("Asset", row.asset, "purchase_receipt_item")
		for item in doc.items:
			if item.get("pr_detail") == pr_detail:
				frappe.db.set_value(
					"Purchase Invoice Item", item.name, "asset_linked", 1, update_modified=False
				)

	_post_delta_transfer_je(doc, transfer_legs)


def _exchange_account(company):
	account = frappe.db.get_value("Company", company, "exchange_gain_loss_account")
	if not account:
		frappe.throw(
			_(
				"Set the Exchange Gain / Loss account on Company {0} — required to "
				"route the FX portion of an invoice-vs-receipt delta (GAP-012 / N3)."
			).format(company)
		)
	return account


def _post_delta_transfer_je(doc, transfer_legs):
	"""Phase 11c D1 (recommended Option A): one JE per PI moving the
	deltas OUT of Asset Received But Not Billed into their destination
	accounts (invoice-difference clearing / post-disposal expense /
	exchange gain-loss). Net effect with the auto-AVA: both ARBNB and
	the clearing account reconcile to zero — §12.4/§12.5's invariant —
	without overriding core's own invoice posting."""
	legs = [(acct, amt, asset) for acct, amt, asset in transfer_legs if flt(amt)]
	if not legs:
		return
	arbnb = frappe.db.get_value("Company", doc.company, "asset_received_but_not_billed")
	if not arbnb:
		frappe.throw(
			_("Set 'Asset Received But Not Billed' on Company {0}.").format(doc.company)
		)

	def _side(amount):
		return (
			{"debit_in_account_currency": abs(amount)}
			if amount > 0
			else {"credit_in_account_currency": abs(amount)}
		)

	accounts = []
	total = 0.0
	for acct, amt, asset_name in legs:
		accounts.append(
			{
				"account": acct,
				"reference_type": "Asset",
				"reference_name": asset_name,
				**_side(amt),
			}
		)
		total = fa_module_round(total + amt, doc.company)
	accounts.append({"account": arbnb, **_side(-total)})

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": doc.company,
			"posting_date": doc.posting_date,
			"user_remark": _("Invoice delta transfer for {0} (GAP-012)").format(doc.name),
			"accounts": accounts,
		}
	)
	je.flags.ignore_permissions = True
	je.submit()
	doc.add_comment(
		"Comment",
		_("Invoice-vs-receipt delta moved out of ARBNB via {0} (GAP-012).").format(je.name),
	)


def pi_on_cancel(doc, method=None):
	"""Cancel the auto-AVAs — each flows through the Phase 4
	Reversal-AVA path, unwinding the delta via counter-documents."""
	if not _enterprise():
		return
	for item in doc.items:
		if item.get("asset_linked"):
			frappe.db.set_value(
				"Purchase Invoice Item", item.name, "asset_linked", 0, update_modified=False
			)
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

	# Phase 11c D1: mirror the delta-transfer JE (immutable — original
	# stays posted) and pair any Case A.02 treatments.
	transfer_je = frappe.db.get_value(
		"Journal Entry",
		{"user_remark": ("like", f"Invoice delta transfer for {doc.name}%"), "docstatus": 1},
		"name",
	)
	if transfer_je:
		from asset_enterprise.restore import _mirror_je

		_mirror_je(
			transfer_je, _("Reversal of invoice delta transfer for {0}").format(doc.name)
		)
	from asset_enterprise import tcc

	for ft in frappe.get_all(
		"Financial Treatment",
		filters={
			"source_doctype": "Purchase Invoice",
			"source_name": doc.name,
			"transaction_type": "Post-Disposal Invoice Adjustment",
			"status": "Posted",
		},
		pluck="name",
	):
		tcc.reverse(ft, ("Purchase Invoice", doc.name))


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
