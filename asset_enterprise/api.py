"""Small whitelisted API surface for the JS layer (C126: JS is UX-only;
these endpoints re-run the authoritative logic)."""

import frappe
from frappe import _
from frappe.utils import cint


@frappe.whitelist()
def recalculate(asset_name):
	from asset_enterprise.asset_values import recalculate_asset_values

	frappe.has_permission("Asset", "write", asset_name, throw=True)
	return recalculate_asset_values(asset_name, save=True)


@frappe.whitelist()
def enable_depreciation_defaults(asset_name):
	"""Prefill for the GAP-011 Enable Depreciation dialog from the Asset
	Category's finance-book defaults — the same values core copies onto a
	new Asset when the category is chosen at creation (client, 18/08:
	the dialog opened empty although the category carries them)."""
	from frappe.utils import flt

	from asset_enterprise.rounding import fa_module_round

	frappe.has_permission("Asset", "read", asset_name, throw=True)
	asset = frappe.db.get_value(
		"Asset",
		asset_name,
		["asset_category", "company", "net_purchase_amount", "available_for_use_date", "purchase_receipt"],
		as_dict=True,
	)
	if not asset:
		return {}

	# §4.4 basis prefill: the asset's own in-service date, else the
	# receipt posting date it arrived on (Ruba, 18/08).
	afu = asset.available_for_use_date or (
		asset.purchase_receipt
		and frappe.db.get_value("Purchase Receipt", asset.purchase_receipt, "posting_date")
	)
	if not asset.asset_category:
		return {"available_for_use_date": afu}

	rows = frappe.get_all(
		"Asset Finance Book",
		filters={"parent": asset.asset_category, "parenttype": "Asset Category"},
		fields=[
			"finance_book",
			"total_number_of_depreciations",
			"frequency_of_depreciation",
			"expected_value_after_useful_life",
			"salvage_value_percentage",
			"depreciation_start_date",
		],
		order_by="idx",
	)
	if not rows:
		return {"available_for_use_date": afu}
	default_fb = frappe.db.get_value("Company", asset.company, "default_finance_book")
	row = next((r for r in rows if r.finance_book == default_fb), rows[0])

	# Category-level salvage is normally a percentage of the asset's own
	# value; an absolute amount on the row wins when someone set one.
	salvage = flt(row.expected_value_after_useful_life) or fa_module_round(
		flt(asset.net_purchase_amount) * flt(row.salvage_value_percentage) / 100,
		asset.company,
	)
	# Posting-date default, core's rule (asset.py:617): the category
	# row's own date when it has one and it is not before the in-service
	# date, else the last day of the in-service month — never "today"
	# (Ruba, 18/08).
	from frappe.utils import get_last_day, getdate

	posting = row.depreciation_start_date
	if afu:
		if not posting or getdate(posting) < getdate(afu):
			posting = get_last_day(afu)

	return {
		"total_number_of_depreciations": row.total_number_of_depreciations,
		"frequency_of_depreciation": row.frequency_of_depreciation or 1,
		"expected_value_after_useful_life": salvage,
		"finance_book": row.finance_book,
		"available_for_use_date": afu,
		"depreciation_start_date": posting,
	}


@frappe.whitelist()
def tree_panel(asset_name):
	"""GAP-009: parent + children + subtree totals for the Asset form's
	tree panel."""
	frappe.has_permission("Asset", "read", asset_name, throw=True)
	parent = frappe.db.get_value("Asset", asset_name, "parent_asset")
	parent_name = parent and frappe.db.get_value("Asset", parent, "asset_name")
	children = frappe.get_all(
		"Asset",
		filters={"parent_asset": asset_name, "docstatus": ("<", 2)},
		fields=[
			"name",
			"asset_name",
			"status",
			"historical_asset_value",
			"net_book_value",
		],
		order_by="name",
	)
	return {
		"parent": parent,
		"parent_name": parent_name,
		"children": children,
		"totals": tree_aggregate(asset_name) if children else None,
	}


@frappe.whitelist()
def tree_aggregate(asset_name):
	"""GAP-009: aggregated HAV / Accum / NBV over the asset and every
	descendant (parent_asset chain), for the tree dashboard (TC-012)."""
	from asset_enterprise.asset_values import recalculate_asset_values

	frappe.has_permission("Asset", "read", asset_name, throw=True)

	nodes, queue = [asset_name], [asset_name]
	while queue:
		children = frappe.get_all(
			"Asset", filters={"parent_asset": queue.pop(0), "docstatus": 1}, pluck="name"
		)
		nodes.extend(children)
		queue.extend(children)

	totals = {
		"assets": len(nodes),
		"historical_asset_value": 0.0,
		"accumulated_depreciation_value": 0.0,
		"net_book_value": 0.0,
	}
	for name in nodes:
		values = recalculate_asset_values(name, save=False)
		totals["historical_asset_value"] += values["historical_asset_value"]
		totals["accumulated_depreciation_value"] += values["accumulated_depreciation_value"]
		totals["net_book_value"] += values["net_book_value"]
	return totals


@frappe.whitelist()
def scrap_posting_defaults(asset, scrapping_type=None):
	"""Where a scrap will post — resolved from the Scrapping Type (§3.5).

	The form shows the account and cost centre read-only before submit
	(client, 20/08). Resolution stays server-side; the JS only displays
	what this returns.
	"""
	from asset_enterprise.accounts import get_disposal_account, get_disposal_cost_center

	frappe.has_permission("Asset", "read", asset, throw=True)
	a = frappe.db.get_value(
		"Asset", asset, ["company", "asset_category", "cost_center"], as_dict=True
	)
	if not a:
		return {}
	allow = frappe.db.get_value(
		"Scrapping Type", scrapping_type, "allow_cost_center_override"
	) if scrapping_type else 0
	try:
		account = get_disposal_account(
			a.company, scrapping_type=scrapping_type, asset_category=a.asset_category
		)
	except frappe.ValidationError:
		# Draft form: report the gap rather than blocking the picker.
		frappe.clear_last_message()
		account = None
	return {
		"disposal_account": account,
		"cost_center": get_disposal_cost_center(a.company, scrapping_type) or a.cost_center,
		"allow_cost_center_override": 1 if allow else 0,
	}


@frappe.whitelist()
def ava_difference_account(asset, transaction_type=None):
	"""Difference Account for an Asset Value Adjustment, resolved from the
	Asset Category through the §3.5 chain.

	The form shows it read-only for the types that own an account
	(client, 24/08): impairment and revaluation are not the user's to
	route. Resolution stays server-side; the JS only displays what this
	returns.
	"""
	from asset_enterprise.accounts import get_enterprise_account

	frappe.has_permission("Asset", "read", asset, throw=True)
	field = {
		"Initial Impairment": "impairment_loss_account",
		"Upward Revaluation": "revaluation_surplus_oci_account",
		"Invoice Adjustment": "asset_invoice_difference_account",
	}.get(transaction_type)
	if not field:
		return {"account": None, "locked": 0}
	a = frappe.db.get_value("Asset", asset, ["company", "asset_category"], as_dict=True)
	if not a:
		return {"account": None, "locked": 0}
	try:
		account = get_enterprise_account(field, a.company, a.asset_category)
	except frappe.ValidationError:
		# Unconfigured: report the gap on the form rather than blocking it.
		frappe.clear_last_message()
		account = None
	# Locked only when there is something to lock it to. The field is
	# mandatory, so a locked EMPTY one would stop the document being saved
	# at all — the caller must be able to trust this flag on its own.
	return {
		"account": account,
		"locked": 1 if account else 0,
		"asset_category": a.asset_category,
	}


@frappe.whitelist()
def movement_cost_centre_impact(
	asset, transaction_date, target_cost_center, old_cost_center=None
):
	"""What a cost-centre transfer will do to depreciation, BEFORE it is
	submitted.

	The transfer itself posts nothing, so the effect only shows up later
	in the depreciation entries — which is why it kept surprising people.
	Two things are worth saying out loud:

	  * the period containing the transfer is split by days between the
	    two centres, in one entry (GAP-021);
	  * periods that ended BEFORE the transfer stay with the old centre
	    however late they are posted.
	"""
	from frappe.utils import add_days, date_diff, flt, get_first_day, getdate

	from asset_enterprise.depreciation import cost_centre_on
	from asset_enterprise.rounding import fa_module_round

	frappe.has_permission("Asset", "read", asset, throw=True)
	on = getdate(transaction_date)
	company, old_cc = frappe.db.get_value(
		"Asset", asset, ["company", "cost_center"]
	) or (None, None)
	# `old_cost_center` is passed by callers running AFTER the transfer has
	# been applied — by then the history already answers with the TARGET,
	# and the comparison below would short-circuit to "nothing changes".
	old_cc = old_cost_center or cost_centre_on(asset, on) or old_cc
	out = {
		"asset": asset,
		"old_cost_center": old_cc,
		"new_cost_center": target_cost_center,
		"split": None,
		"earlier_unposted": [],
	}
	if not company or old_cc == target_cost_center:
		return out

	rows = frappe.db.sql(
		"""
		select ds.schedule_date, ds.depreciation_amount, ds.days_in_period
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') = ''
		order by ds.schedule_date
		""",
		asset,
		as_dict=True,
	)
	for r in rows:
		end = getdate(r.schedule_date)
		days = cint(r.days_in_period) or 1
		start = add_days(end, -(days - 1))
		if start <= on <= end:
			before = max(0, date_diff(on, start))
			after = days - before
			old_part = fa_module_round(flt(r.depreciation_amount) * before / days, company)
			out["split"] = {
				"period_end": str(end),
				"days_before": before,
				"days_after": after,
				"old_amount": old_part,
				"new_amount": fa_module_round(flt(r.depreciation_amount) - old_part, company),
			}
		elif end < on:
			out["earlier_unposted"].append(
				{"period_end": str(end), "amount": flt(r.depreciation_amount)}
			)
	return out


@frappe.whitelist()
def reversible_partial_scraps(asset):
	"""Posted partial scraps of an asset that have not been reversed.

	The reversal endpoint takes ONE treatment, so where an asset carries
	several partial scraps the user has to say which — this feeds that
	choice, newest first.
	"""
	frappe.has_permission("Asset", "read", asset, throw=True)
	rows = frappe.db.sql(
		"""
		select ft.name, ft.posting_date, ft.amount, ft.journal_entry, ft.transaction_type
		from `tabFinancial Treatment` ft
		where ft.asset = %s and ft.transaction_category = 'Disposal'
		  and ft.status = 'Posted' and ft.transaction_type like 'Partial Disposal%%'
		order by ft.posting_date desc, ft.creation desc
		""",
		asset,
		as_dict=True,
	)
	from frappe.utils import get_first_day, get_last_day, getdate, nowdate

	today = getdate(nowdate())
	for r in rows:
		# Same window the server enforces (VR-033), so the form can offer
		# the right action rather than inviting a refusal.
		r.in_window = bool(
			get_first_day(today) <= getdate(r.posting_date) <= get_last_day(today)
		)
		r.scrap_transaction = frappe.db.get_value(
			"Scrap Transaction", {"journal_entry": r.journal_entry, "docstatus": 1}, "name"
		)
	return rows


# ---------------------------------------------------------------------------
# C3 — reversal posting date (GA-0005-01 §3.7 / GAP-030 / GAP-033).
# A Standard AVA / capitalized Asset Repair cancel raises its Reversal
# counter-document; the reversal posts on TODAY by default. The dialog
# lets a user with the company's Reversal Date Edit Role choose another
# date. Enforcement is server-side here — the JS is UX-only (C126).
# ---------------------------------------------------------------------------


def _reversal_date_edit_role(company):
	"""The per-company role allowed to change a reversal posting date
	from today (Asset Settings > Reversals > per-Company table)."""
	return frappe.db.get_value(
		"Asset Settings Reversal Role",
		{"parent": "Asset Settings", "company": company},
		"reversal_date_edit_role",
	)


def assert_reversal_not_before_source(source_posting_date, posting_date, label):
	"""VR-022 / GA-0001-01: a counter-document posts on or after the
	transaction it reverses — never before it.

	Held separately from the role gate below because the two rules have
	different reach. This one governs EVERY reversal, including the
	disposal-restore paths, where a chosen date is normal and needs no
	permission (Path 3 exists precisely to restore in a later period).
	Until 2026-08-26 those paths enforced nothing: cross_period_restore
	read the restore date and the disposal date on adjacent lines and
	never compared them.
	"""
	from frappe.utils import getdate, nowdate

	chosen = getdate(posting_date or nowdate())
	if source_posting_date and chosen < getdate(source_posting_date):
		frappe.throw(
			_(
				"Reversal posting date {0} is before the {1} posting date {2} — "
				"a reversal cannot precede the transaction it reverses (VR-022)."
			).format(chosen, label, getdate(source_posting_date)),
			title=_("Reversal Date Before Source"),
		)
	return chosen


def _assert_reversal_date(company, source_posting_date, posting_date, label):
	"""Validate a user-chosen reversal posting date.

	- It can never precede the transaction it reverses (VR-022, above).
	- Today is the default and needs no permission. Any OTHER date
	  requires the company's Reversal Date Edit Role; with no role
	  configured the reversal posts today, always.
	"""
	from frappe.utils import getdate, nowdate

	chosen = assert_reversal_not_before_source(source_posting_date, posting_date, label)
	if chosen != getdate(nowdate()):
		role = _reversal_date_edit_role(company)
		if not role:
			frappe.throw(
				_(
					"No Reversal Date Edit Role is configured for {0} — the reversal "
					"posts today. Set one in Asset Settings > Reversals to allow other dates."
				).format(company),
				title=_("Reversal Date Locked"),
			)
		if frappe.session.user != "Administrator" and role not in frappe.get_roles():
			frappe.throw(
				_(
					"Changing the reversal posting date from today requires the {0} role."
				).format(role),
				title=_("Reversal Date Locked"),
			)
	return chosen


@frappe.whitelist()
def cancel_ava_with_reversal(ava_name, posting_date=None):
	"""C3: cancel a Standard AVA and raise its Reversal AVA on a
	user-chosen posting date (default today; other dates need the
	company's Reversal Date Edit Role)."""
	from frappe.utils import getdate, nowdate

	doc = frappe.get_doc("Asset Value Adjustment", ava_name)
	frappe.has_permission("Asset Value Adjustment", "cancel", doc, throw=True)
	_assert_reversal_date(doc.company, doc.get("date"), posting_date, _("AVA"))
	frappe.flags["ae_ava_reversal_date"] = getdate(posting_date or nowdate())
	try:
		doc.cancel()
	finally:
		frappe.flags["ae_ava_reversal_date"] = None
	return True


@frappe.whitelist()
def cancel_repair_with_reversal(repair_name, posting_date=None):
	"""C3: cancel a capitalized Asset Repair and raise its Reversal
	Repair on a user-chosen posting date (default today; other dates
	need the company's Reversal Date Edit Role)."""
	from frappe.utils import getdate, nowdate

	doc = frappe.get_doc("Asset Repair", repair_name)
	frappe.has_permission("Asset Repair", "cancel", doc, throw=True)
	_assert_reversal_date(
		doc.company,
		doc.get("completion_date") or doc.get("failure_date"),
		posting_date,
		_("Asset Repair"),
	)
	frappe.flags["ae_repair_reversal_date"] = getdate(posting_date or nowdate())
	try:
		doc.cancel()
	finally:
		frappe.flags["ae_repair_reversal_date"] = None
	return True


@frappe.whitelist()
def reversal_date_editable(company):
	"""C3 UX helper: may the current user change the reversal posting
	date from today for this company? (Enforcement lives in the cancel
	endpoints; this only decides whether the dialog's date field is
	editable.)"""
	role = _reversal_date_edit_role(company)
	if not role:
		return {"editable": False, "role": None}
	editable = frappe.session.user == "Administrator" or role in frappe.get_roles()
	return {"editable": editable, "role": role}
