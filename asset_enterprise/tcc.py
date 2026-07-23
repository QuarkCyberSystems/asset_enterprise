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
			"asset": asset,
			"company": company,
			"posting_date": posting_date or nowdate(),
			"amount": flt(amount),
			"hav_delta": flt(hav_delta),
			"accum_delta": flt(accum_delta),
			"life_delta_months": flt(life_delta_months),
			"journal_entry": journal_entry,
			"status": status,
		}
	)
	ft.flags.ignore_permissions = True
	ft.insert()

	values = recalculate_asset_values(asset)
	_add_asset_activity(ft, values)
	return ft


def reverse(original_ft, reversal_source_doc, posting_date=None, journal_entry=None):
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
			"asset": original.asset,
			"company": original.company,
			"posting_date": posting_date or nowdate(),
			"amount": -flt(original.amount),
			"hav_delta": -flt(original.hav_delta),
			"accum_delta": -flt(original.accum_delta),
			"life_delta_months": -flt(original.life_delta_months),
			"journal_entry": journal_entry,
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
	if not frappe.db.exists("Transaction Category", category):
		frappe.throw(
			_("Transaction Category {0} is not seeded — run asset_enterprise migrate.").format(category)
		)


def _source_ref(source_doc):
	if isinstance(source_doc, (tuple, list)):
		return source_doc[0], source_doc[1]
	return source_doc.doctype, source_doc.name


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
			"linked_journal_entry": ft.journal_entry,
			"source_doctype": ft.source_doctype,
			"source_name": ft.source_name,
			"reversal_reference": ft.reversal_reference,
		}
	).insert(ignore_permissions=True)
