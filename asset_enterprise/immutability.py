"""Deletion guard for asset transactions (immutable ledger).

An Asset Capitalization on the UAT server was deleted after it had
posted — its journal entries and superseded schedules survived, but the
supersession trail then named a document that no longer existed
(16/08/2026). Under the immutable-ledger rule these documents are never
removed: each has a reversal path that leaves both sides on the record.

Drafts stay freely deletable — nothing has been posted from them.
"""

import frappe
from frappe import _

# doctype -> how to undo it instead of deleting
REVERSAL_ROUTE = {
	"Asset Capitalization": _(
		"submit a Reversal of Capitalized Maintenance against it"
	),
	"Asset Repair": _("cancel it — a Reversal Repair is created automatically"),
	"Asset Value Adjustment": _("cancel it — a Reversal AVA is created automatically"),
	"Scrap Transaction": _("restore the asset, or create a replacement asset"),
	"Asset": _("reverse the asset once its depreciation entries are reversed"),
}


def block_deletion_of_posted_document(doc, method=None):
	"""on_trash guard: a document that reached submit is ledger history."""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		return
	if doc.doctype not in REVERSAL_ROUTE:
		return
	if not doc.docstatus.is_submitted() and not doc.docstatus.is_cancelled():
		return  # draft — never posted anything

	frappe.throw(
		_(
			"{0} {1} has been submitted and is part of the ledger — it cannot be "
			"deleted. To undo it, {2}. Deleting it would leave journal entries and "
			"depreciation schedules pointing at a document that no longer exists."
		).format(_(doc.doctype), doc.name, REVERSAL_ROUTE[doc.doctype]),
		title=_("Posted Documents Are Not Deleted"),
	)
