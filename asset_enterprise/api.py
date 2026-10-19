"""Small whitelisted API surface for the JS layer (C126: JS is UX-only;
these endpoints re-run the authoritative logic)."""

import frappe
from frappe import _
from frappe.utils import cint, flt


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

	from asset_enterprise.control_category import applies
	if applies(asset):
		return {"control_category_one_day": True, "total_number_of_depreciations": 1,
			"frequency_of_depreciation": 1, "expected_value_after_useful_life": 0,
			"available_for_use_date": afu, "depreciation_start_date": afu,
			"finance_book": frappe.db.get_value("Company", asset.company, "default_finance_book")}

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
	# Posting-date default: the last day of the in-service month (client,
	# 27/09, FA-007) — never "today" (Ruba, 18/08). The category row's
	# stored date no longer wins: it is one fixed calendar date for every
	# asset of the category, so an asset in service in February first
	# posted in September. It is used only when there is no in-service
	# date to derive from.
	from frappe.utils import get_last_day

	posting = get_last_day(afu) if afu else row.depreciation_start_date

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
	asset, transaction_date, target_cost_center, old_cost_center=None, already_applied=0
):
	"""What a cost-centre transfer will do to depreciation, BEFORE it is
	submitted.

	The transfer itself posts nothing, so the effect only shows up later
	in the depreciation entries — which is why it kept surprising people.
	Two things are worth saying out loud:

	  * the period containing the transfer is split by days between the
	    centres, in one entry (GAP-021);
	  * periods that ended BEFORE the transfer stay with the old centre
	    however late they are posted.

	The split is `attribution_split`'s — the function the entry itself
	posts with — asked with this transfer replayed as a pending movement,
	so the preview carries the same boundaries (D-009 next-day centre),
	the same other movements in the period and the same rounding as the
	ledger, and a Control Category asset (D-053) shows no split at all.
	A caller running AFTER the transfer was submitted passes
	`already_applied` (and the centre it left, `old_cost_center`): the
	history then already contains it.
	"""
	from frappe.utils import add_days, flt, getdate

	from asset_enterprise.depreciation import attribution_split, cost_centre_on

	frappe.has_permission("Asset", "read", asset, throw=True)
	on = getdate(transaction_date)
	company, old_cc = frappe.db.get_value(
		"Asset", asset, ["company", "cost_center"]
	) or (None, None)
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
	pending = None if cint(already_applied) else {
		"transaction_date": on, "target_cost_center": target_cost_center,
		"source_cost_center": old_cc,
	}

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
		start = add_days(end, -((cint(r.days_in_period) or 1) - 1))
		if start <= on <= end:
			segments = attribution_split(
				asset, start, end, flt(r.depreciation_amount), company,
				fallback=old_cc, pending=pending, with_days=True,
			)
			if len({cc for cc, _dims, _part, _from, _days in segments}) < 2:
				continue  # nothing changes centre inside the period
			# The transfer day belongs to the centre giving the asset up
			# (D-009): segments starting after it are the receiving side.
			before = [seg for seg in segments if getdate(seg[3]) <= on]
			after = [seg for seg in segments if getdate(seg[3]) > on]
			out["split"] = {
				"period_end": str(end),
				"days_before": sum(seg[4] for seg in before),
				"days_after": sum(seg[4] for seg in after),
				"old_amount": flt(sum(seg[2] for seg in before)),
				"new_amount": flt(sum(seg[2] for seg in after)),
				"segments": [
					{"cost_center": cc, "from": str(frm), "days": days, "amount": part}
					for cc, _dims, part, frm, days in segments
				],
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

	# The role gate first: a user who may not move the date is told the
	# reversal posts today, not that a date they cannot use anyway falls
	# before the entry (client ticket FA-003, 29/09 — a 31/03 date on a
	# Mass Depreciation Reversal got the VR-022 message).
	chosen = getdate(posting_date or nowdate())
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
	return assert_reversal_not_before_source(source_posting_date, chosen, label)


@frappe.whitelist()
def reverse_document(doctype, name, posting_date=None):
	"""Reverse a posted Asset Value Adjustment, Capitalized Maintenance or
	capitalized Asset Repair by its counter-document (GAP-030; the
	original stays submitted, marked Reversed). The user needs the cancel
	right on it; a date other than today needs the company's Reversal Date
	Edit Role. Returns the counter-document's name."""
	from asset_enterprise import reversal

	return reversal.reverse(doctype, name, posting_date=posting_date, check_permission=True)


@frappe.whitelist()
def reversal_date_editable(company):
	"""C3 UX helper: may the current user change the reversal posting
	date from today for this company? (Enforcement lives in
	reverse_document / assert_reversal_date; this only decides whether the
	dialog's date field is editable.)"""
	role = _reversal_date_edit_role(company)
	if not role:
		return {"editable": False, "role": None}
	editable = frappe.session.user == "Administrator" or role in frappe.get_roles()
	return {"editable": editable, "role": role}


# --------------------------------------------------------------------------
# Server-side contract for other apps (PA code audit 14/10/2026, A04 / D6).
# Project Accounting calls these instead of keeping its own copies of the
# asset module's rules; they are plain functions, not HTTP endpoints.
# --------------------------------------------------------------------------


def value_events_other_than(asset, exclude_sources=None):
	"""Live value events on `asset` that were NOT raised by any of
	`exclude_sources` — every posted, unreversed Financial Treatment that
	changed the asset's value (any type: Asset Value Adjustment, Capitalized
	Maintenance, capitalized repair, reclassification, invoice difference,
	partial scrap...), depreciation excepted.

	`exclude_sources`: document names (or (doctype, name) pairs) whose own
	treatments do not count — matched against the treatment's source and
	its voucher. Financial Treatments are the single record of value events
	(§3), so a new kind of event is covered without changing this.

	A04: an asset Project Accounting created must not be reversed while
	something else has changed its value since — the reversal would cancel
	the asset and leave that value posted against nothing."""
	names = set()
	for item in exclude_sources or []:
		names.add(item[1] if isinstance(item, (list, tuple)) else item)
	rows = frappe.get_all(
		"Financial Treatment",
		filters={"asset": asset, "status": "Posted", "is_reversal": 0},
		fields=[
			"name", "source_doctype", "source_name", "voucher_type", "voucher_no",
			"transaction_category", "transaction_type", "posting_date", "hav_delta", "accum_delta",
		],
		order_by="posting_date asc, creation asc",
	)
	from frappe.utils import flt

	return [
		r
		for r in rows
		if r.transaction_category != "Depreciation"
		and (flt(r.hav_delta) or flt(r.accum_delta))
		and not frappe.db.exists("Financial Treatment", {"reverses": r.name, "docstatus": ("<", 2)})
		and r.source_name not in names
		and r.voucher_no not in names
	]


def assert_value_reversible(asset, amount, context=None):
	"""VR-042: refuse a reversal that would take more value off `asset`
	than its current net book value can cover."""
	from asset_enterprise.asset_values import assert_nbv_covers_reversal

	return assert_nbv_covers_reversal(asset, amount, context=context)


def live_depreciation_entries(asset):
	"""Depreciation journal entries that still stand on `asset` (GAP-027 /
	VR-031): every entry stamped on any of its schedules — a superseded
	generation copies its posted rows, so one entry is counted once — plus
	manual depreciation entries, less those reversed per GA-0001-01 (a live
	Reversal Journal Entry points back through `reversal_of`)."""
	je_names = set(
		frappe.db.sql_list(
			"""select ds.journal_entry from `tabDepreciation Schedule` ds
			   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			   where ads.asset = %s and ads.docstatus = 1 and ifnull(ds.journal_entry, '') != ''""",
			asset,
		)
	)
	je_names |= {d.name for d in frappe.get_doc("Asset", asset).get_manual_depreciation_entries()}
	reversible = frappe.get_meta("Journal Entry").has_field("reversal_of")
	return sorted(
		je for je in je_names
		if not (reversible and frappe.db.exists("Journal Entry", {"reversal_of": je, "docstatus": 1}))
	)


def assert_reversal_date(company, source_posting_date, posting_date, label):
	"""The reversal-date rules every reversal follows: today needs nothing;
	any other date needs the company's Reversal Date Edit Role (Asset
	Settings > Reversals); never before the transaction reversed (VR-022).
	Returns the date to post on."""
	return _assert_reversal_date(company, source_posting_date, posting_date, label)


from contextlib import contextmanager  # noqa: E402


@contextmanager
def raising_project_settlement():
	"""Inside this block an Asset Value Adjustment of type Project
	Settlement may be created — the type is raised only by a settlement
	(PA-008), never typed by hand. Restores the previous state on exit."""
	previous = frappe.flags.get("ae_project_settlement")
	frappe.flags["ae_project_settlement"] = True
	try:
		yield
	finally:
		frappe.flags["ae_project_settlement"] = previous


# --------------------------------------------------------------------------
# D-081 (Vivek, 15/10/2026): Project Accounting's Negative Settlement reduces
# an asset its original settlement added to. The asset side is this module's
# — one rule with Case A.02 (§12.5), never a copy in PA.
# --------------------------------------------------------------------------

AVA_SOURCE_FIELDS = ("reduction_source_doctype", "reduction_source_name")


def _reduction_split(asset, amount, posting_date):
	"""(asset row, absorbed, to_expense, blocked reason or None)."""
	from frappe.utils import flt, getdate

	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import due_unposted_rows
	from asset_enterprise.rounding import fa_module_round
	from asset_enterprise.status import off_register

	row = frappe.db.get_value(
		"Asset", asset, ["name", "company", "status", "docstatus", "asset_category"], as_dict=True
	)
	if not row or row.docstatus != 1:
		frappe.throw(_("{0} is not a submitted asset — nothing to reduce.").format(asset))
	amount = fa_module_round(flt(amount), row.company)
	if amount <= 0:
		frappe.throw(_("The reduction amount must be positive."))
	if off_register(row.status):
		return row, 0.0, amount, None
	salvage = flt(
		frappe.db.get_value("Asset Finance Book", {"parent": asset}, "expected_value_after_useful_life") or 0
	)
	nbv = flt(recalculate_asset_values(asset, save=False)["net_book_value"])
	absorbed = fa_module_round(max(0.0, min(amount, nbv - salvage)), row.company)
	blocked = None
	if absorbed and due_unposted_rows(asset, getdate(posting_date)):
		blocked = _(
			"Depreciation is not up to date for {0} — post the due periods before reducing its "
			"value (VR-043)."
		).format(asset)
	return row, absorbed, fa_module_round(amount - absorbed, row.company), blocked


def reduce_asset_value(asset, amount, posting_date, debit_account, source_doctype, source_name,
		label=None, dimensions=None, dry_run=False):
	"""Reduce `asset` by `amount` for another module's document (D-081).

	- On the register: an Asset Value Adjustment decrease (type Project
	  Settlement) takes what the asset can absorb — never below its salvage
	  (VR-042 cover) — Dr `debit_account` / Cr Fixed Asset, and the schedule
	  re-prices from `posting_date` (D-075).
	- The rest, or all of it for an asset off the register: Dr `debit_account`
	  / Cr the Post-Disposal Invoice Difference account, with the Case A.02
	  treatment on the asset's history (invoice_diff, one rule).

	`dimensions` ({fieldname: value}; cost_center included) go on both
	documents. The date passes assert_reversal_date. `dry_run=True` returns
	the split and writes nothing. Returns {asset, absorbed, to_expense,
	expense_account, ava, journal_entry, blocked}."""
	from frappe.utils import getdate

	from asset_enterprise.depreciation import cost_centre_on
	from asset_enterprise.invoice_diff import post_disposal_account, record_post_disposal_adjustment

	row, absorbed, to_expense, blocked = _reduction_split(asset, amount, posting_date)
	expense_account = post_disposal_account(asset) if to_expense else None
	result = frappe._dict(asset=asset, absorbed=absorbed, to_expense=to_expense,
		expense_account=expense_account, ava=None, journal_entry=None, blocked=blocked)
	if dry_run:
		return result
	if blocked:
		frappe.throw(blocked, title=_("Depreciation Outstanding"))
	posting_date = assert_reversal_date(row.company, None, posting_date, label or _("Asset reduction"))
	dims = dict(dimensions or {})
	cost_center = dims.pop("cost_center", None) or cost_centre_on(asset, posting_date) \
		or frappe.db.get_value("Asset", asset, "cost_center")
	remark = _("Reduction of {0} for {1} {2}").format(asset, source_doctype, label or source_name)

	if absorbed:
		from asset_enterprise.asset_values import recalculate_asset_values
		from asset_enterprise.rounding import fa_module_round

		current = flt(recalculate_asset_values(asset, save=False)["net_book_value"])
		meta = frappe.get_meta("Asset Value Adjustment")
		ava = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": asset, "company": row.company,
			"date": getdate(posting_date), "transaction_type": "Project Settlement",
			"current_asset_value": current,
			"new_asset_value": fa_module_round(current - absorbed, row.company),
			"difference_account": debit_account, "cost_center": cost_center,
			"reduction_source_doctype": source_doctype, "reduction_source_name": source_name,
			**{k: v for k, v in dims.items() if meta.has_field(k)},
		})
		ava.flags.ignore_permissions = True
		with raising_project_settlement():
			ava.insert()
			ava.submit()
		result.ava = ava.name

	if to_expense:
		je_meta = frappe.get_meta("Journal Entry Account")
		row_dims = {k: v for k, v in dims.items() if je_meta.has_field(k)}
		je = frappe.get_doc({
			"doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": row.company,
			"posting_date": getdate(posting_date), "user_remark": remark,
			"accounts": [
				{"account": debit_account, "debit_in_account_currency": to_expense,
				 "cost_center": cost_center, **row_dims},
				{"account": expense_account, "credit_in_account_currency": to_expense,
				 "cost_center": cost_center, "reference_type": "Asset", "reference_name": asset, **row_dims},
			],
		})
		je.flags.ignore_permissions = True
		je.submit()
		record_post_disposal_adjustment((source_doctype, source_name), asset, getdate(posting_date),
			to_expense, journal_entry=je.name)
		result.journal_entry = je.name
	return result


def undo_asset_value_reduction(source_doctype, source_name, posting_date):
	"""Reverse everything reduce_asset_value created for that source on
	`posting_date`: the AVA through its Reversal AVA, the Case A.02 entry
	through a mirror journal and the treatment's reversal. Returns
	{reversed: [document names]}."""
	from frappe.utils import getdate

	from asset_enterprise import tcc
	from asset_enterprise.invoice_diff import POST_DISPOSAL_ADJUSTMENT
	from asset_enterprise.restore import _mirror_je

	reversed_docs = []
	for name in frappe.get_all("Asset Value Adjustment", filters={
			"reduction_source_doctype": source_doctype, "reduction_source_name": source_name,
			"docstatus": 1, "reversal_of_ava": ("is", "not set"), "reversed_by_ava": ("is", "not set")},
			pluck="name"):
		from asset_enterprise import reversal

		reversed_docs.append(
			reversal.reverse("Asset Value Adjustment", name, posting_date=posting_date, check_permission=False)
		)
	for ft in frappe.get_all("Financial Treatment", filters={
			"source_doctype": source_doctype, "source_name": source_name, "status": "Posted",
			"is_reversal": 0, "transaction_type": POST_DISPOSAL_ADJUSTMENT},
			fields=["name", "journal_entry", "company", "posting_date", "asset"]):
		date = assert_reversal_date(ft.company, ft.posting_date, posting_date, _("Asset reduction"))
		mirror = _mirror_je(ft.journal_entry, _("Reversal of asset reduction for {0} {1} (mirrors {2})")
			.format(source_doctype, source_name, ft.journal_entry), posting_date=getdate(date)) if ft.journal_entry else None
		tcc.reverse(ft.name, (source_doctype, source_name), posting_date=getdate(date), journal_entry=mirror)
		if mirror:
			reversed_docs.append(mirror)
	return frappe._dict(reversed=[d for d in reversed_docs if d])

