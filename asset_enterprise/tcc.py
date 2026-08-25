"""Transaction Category Controller — GA-0005-01 v2.14 §3.

The sole entry point for fixed-asset financial impact. Source documents
(Asset, AVA, Asset Repair, Asset Capitalization, PR/PI hooks, Mass
Depreciation) never write Financial Treatments or asset values
directly — they call:

    tcc.apply(
        source_doc=doc, category="Addition", transaction_type="Purchase",
        asset=asset_name, amount=..., posting_date=...,
        hav_delta=..., accum_delta=..., life_delta_months=...,
        journal_entry=je_name,
    )

Callers may reference either a Journal Entry (`journal_entry=`) or, per
GA-0006 amendment AM-01, the voucher whose own GL carries the treatment
(`voucher_type=`/`voucher_no=`) — used by Project Accounting settlement
runs, which post every leg under the run's voucher rather than
manufacturing a Journal Entry to hold the reference.

and, for Reverse Mode (§3.7 — reverse the transaction, which generates
the reversal JE; the original FT is never deleted):

    tcc.reverse(original_ft_name, reversal_source_doc, posting_date, journal_entry)

Every apply/reverse:
1. writes an append-only Financial Treatment with signed deltas,
2. recalculates the asset's ledger-derived values (GAP-006),
3. enriches Asset Activity with the post-treatment snapshot (§5.2).

Categories (seeded masters): Addition / Disposal / Impairment /
Revaluation / Useful Life Adjustment / Depreciation.
"""

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from asset_enterprise.asset_values import recalculate_asset_values

CATEGORIES = frozenset(
	{
		"Addition",
		"Disposal",
		"Impairment",
		"Revaluation",
		"Useful Life Adjustment",
		"Depreciation",
	}
)


def apply(
	source_doc,
	category,
	asset,
	posting_date=None,
	transaction_type=None,
	amount=0,
	hav_delta=0,
	accum_delta=0,
	life_delta_months=0,
	journal_entry=None,
	voucher_type=None,
	voucher_no=None,
	status="Posted",
):
	"""Record one Financial Treatment and refresh derived values.

	Returns the Financial Treatment doc. `source_doc` may be a Document
	or a (doctype, name) tuple.
	"""
	_validate_category(category)
	source_doctype, source_name = _source_ref(source_doc)
	company = frappe.db.get_value("Asset", asset, "company")

	ft = frappe.get_doc(
		{
			"doctype": "Financial Treatment",
			"source_doctype": source_doctype,
			"source_name": source_name,
			"transaction_category": category,
			"transaction_type": transaction_type,
			# C6 §9.2: carry the source's sub-type onto the treatment.
			"transaction_sub_type": (
				source_doc.get("transaction_sub_type") if not isinstance(source_doc, (tuple, list)) else None
			),
			"asset": asset,
			"company": company,
			"posting_date": posting_date or nowdate(),
			"amount": flt(amount),
			"hav_delta": flt(hav_delta),
			"accum_delta": flt(accum_delta),
			"life_delta_months": flt(life_delta_months),
			"journal_entry": journal_entry,
			# GA-0006 AM-01: callers that post GL under their own voucher
			# (e.g. Project Settlement Run) reference it here instead of
			# manufacturing a Journal Entry purely to hold the reference.
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			# C6 §9.2: snapshot of the account legs behind this treatment.
			"account_set": _account_set(journal_entry, voucher_type, voucher_no),
			"status": status,
		}
	)
	ft.flags.ignore_permissions = True
	ft.insert()

	values = recalculate_asset_values(asset)
	_add_asset_activity(ft, values)
	return ft


def reverse(
	original_ft, reversal_source_doc, posting_date=None, journal_entry=None,
	voucher_type=None, voucher_no=None,
):
	"""Reverse Mode (§3.7): mirror the original FT under the SAME category.

	- original FT -> status Reversed (never deleted; JE stays posted)
	- mirror FT with negated deltas, reversal_reference -> original
	- both drop out of the asset_values fold as a pair; audit keeps both

	`reversal_source_doc` is the counter-document that carries the
	reversal (Reversal AVA / Reversal Repair / Reversal of Capitalized
	Maintenance / restore JE holder).
	"""
	original = frappe.get_doc("Financial Treatment", original_ft)
	if original.status != "Posted":
		frappe.throw(
			_("Financial Treatment {0} is {1} — only Posted treatments can be reversed.").format(
				original.name, original.status
			)
		)

	source_doctype, source_name = _source_ref(reversal_source_doc)
	mirror = frappe.get_doc(
		{
			"doctype": "Financial Treatment",
			"source_doctype": source_doctype,
			"source_name": source_name,
			"transaction_category": original.transaction_category,
			"transaction_type": f"Reversal of {original.transaction_type or original.transaction_category}",
			# C6 §9.2: the mirror carries the original's sub-type.
			"transaction_sub_type": original.get("transaction_sub_type"),
			"asset": original.asset,
			"company": original.company,
			"posting_date": posting_date or nowdate(),
			"amount": -flt(original.amount),
			"hav_delta": -flt(original.hav_delta),
			"accum_delta": -flt(original.accum_delta),
			"life_delta_months": -flt(original.life_delta_months),
			"journal_entry": journal_entry,
			"voucher_type": voucher_type or original.get("voucher_type"),
			"voucher_no": voucher_no or original.get("voucher_no"),
			# C6 §9.2: the design's reversal flag + link (kept in step
			# with reversal_reference, which the values fold keys on).
			"is_reversal": 1,
			"reverses": original.name,
			"account_set": _account_set(journal_entry, voucher_type or original.get("voucher_type"),
				voucher_no or original.get("voucher_no")),
			"status": "Posted",
			"reversal_reference": original.name,
		}
	)
	mirror.flags.ignore_permissions = True
	mirror.insert()

	original.db_set("status", "Reversed", update_modified=False)

	values = recalculate_asset_values(original.asset)
	_add_asset_activity(mirror, values)
	return mirror


def _validate_category(category):
	if category not in CATEGORIES:
		frappe.throw(_("Unknown Transaction Category: {0}").format(category))
	row = frappe.db.get_value("Transaction Category", category, "enabled")
	if not frappe.db.exists("Transaction Category", category):
		frappe.throw(
			_("Transaction Category {0} is not seeded — run asset_enterprise migrate.").format(category)
		)
	# C6 §9.2: a disabled category is refused by the controller.
	if row is not None and not row:
		frappe.throw(
			_("Transaction Category {0} is disabled — enable it in the master to use it.").format(
				category
			)
		)


def _account_set(journal_entry=None, voucher_type=None, voucher_no=None):
	"""C6 §9.2: the account legs behind a treatment, for audit.

	From the linked Journal Entry's account rows, or — when the GL was
	posted under a run voucher (GA-0006 AM-01) — from that voucher's GL
	rows. Frappe's JSON field stores an object, not a bare list, so the
	accounts ride inside a dict."""
	if journal_entry:
		accounts = [
			r[0]
			for r in frappe.db.sql(
				"select account from `tabJournal Entry Account` where parent = %s order by idx",
				journal_entry,
			)
		]
	elif voucher_type and voucher_no:
		accounts = [
			r[0]
			for r in frappe.db.sql(
				"select distinct account from `tabGL Entry` where voucher_type = %s "
				"and voucher_no = %s and is_cancelled = 0 order by account",
				(voucher_type, voucher_no),
			)
		]
	else:
		accounts = []
	return {"accounts": accounts}


def _source_ref(source_doc):
	if isinstance(source_doc, (tuple, list)):
		return source_doc[0], source_doc[1]
	return source_doc.doctype, source_doc.name


def _total_useful_life_months(asset_name):
	fb = frappe.db.get_value(
		"Asset Finance Book",
		{"parent": asset_name},
		["total_number_of_depreciations", "frequency_of_depreciation"],
		as_dict=True,
	)
	if not fb or not fb.total_number_of_depreciations:
		return 0
	return flt(fb.total_number_of_depreciations) * flt(fb.frequency_of_depreciation or 1)


def _add_asset_activity(ft, values):
	"""§5.2 Asset History enrichment: one Asset Activity row per
	treatment carrying the post-treatment financial snapshot."""
	frappe.get_doc(
		{
			"doctype": "Asset Activity",
			"asset": ft.asset,
			"date": frappe.utils.now(),
			"user": frappe.session.user or "Administrator",
			"subject": _("{0}{1} of {2} (FT {3})").format(
				ft.transaction_category,
				f" — {ft.transaction_type}" if ft.transaction_type else "",
				flt(ft.amount),
				ft.name,
			),
			"financial_effect": 1,
			"transaction_type": ft.transaction_type,
			"transaction_category": ft.transaction_category,
			"transaction_amount": ft.amount,
			"historical_asset_value_after": values["historical_asset_value"],
			"accumulated_depreciation_after": values["accumulated_depreciation_value"],
			"net_book_value_after": values["net_book_value"],
			"remaining_useful_life_after": values["remaining_useful_life_months"],
			"useful_life_after": _total_useful_life_months(ft.asset),
			"linked_journal_entry": ft.journal_entry,
			"source_doctype": ft.source_doctype,
			"source_name": ft.source_name,
			"reversal_reference": ft.reversal_reference,
		}
	).insert(ignore_permissions=True)


def add_snapshot_activity(asset_name, subject, transaction_type=None, journal_entry=None):
	"""§5.2 (Phase 11b): value-snapshot Asset Activity row for non-FT
	events (movements, restore/repair/AVA notes) so every history row
	carries the financial state, not just TCC-generated ones."""
	values = recalculate_asset_values(asset_name, save=False)
	frappe.get_doc(
		{
			"doctype": "Asset Activity",
			"asset": asset_name,
			"date": frappe.utils.now(),
			"user": frappe.session.user or "Administrator",
			"subject": subject,
			"transaction_type": transaction_type,
			"historical_asset_value_after": values["historical_asset_value"],
			"accumulated_depreciation_after": values["accumulated_depreciation_value"],
			"net_book_value_after": values["net_book_value"],
			"remaining_useful_life_after": values["remaining_useful_life_months"],
			"useful_life_after": _total_useful_life_months(asset_name),
			"linked_journal_entry": journal_entry,
		}
	).insert(ignore_permissions=True)
