# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""What asset_enterprise registers with qcs_platform (Build 0.1 §8, step 4).

One adapter answers for the four asset transactions this app posts and
for a Stock Entry that a capitalization raised. The refusals it used to
throw from `immutability.py` and `overrides/stock_entry.py` are the same
words, returned as values; the transactions themselves still cancel
natively - their controllers raise the counter-document (a Reversal AVA,
a Reversal Repair, a Reversal of Capitalized Maintenance) or refuse (a
reversal half, a Scrap Transaction), which is each document's own rule.
`invoice_diff.pr_before_cancel` does invoice-diff work on a receipt's
cancel and stays a hook, declared here as a legacy effect hook.
"""

import frappe
from frappe import _

from qcs_platform.contracts import API_VERSION, Action, Refusal, Registration

OWN = ("Asset Capitalization", "Asset Repair", "Asset Value Adjustment", "Scrap Transaction")

# doctype -> how to undo it instead of deleting (immutable ledger: a posted
# asset transaction is never removed - each has a reversal path that leaves
# both sides on the record; UAT 16/08/2026 deleted a capitalization and left
# the supersession trail naming a document that no longer existed)
REVERSAL_ROUTE = {
	"Asset Capitalization": _("submit a Reversal of Capitalized Maintenance against it"),
	"Asset Repair": _("cancel it - a Reversal Repair is created automatically"),
	"Asset Value Adjustment": _("cancel it - a Reversal AVA is created automatically"),
	"Scrap Transaction": _("restore the asset, or create a replacement asset"),
	# NOT Asset: frappe already refuses to delete a submitted one, and
	# cancelling a Purchase Receipt legitimately deletes the draft or
	# reversed assets it created
}


def _enterprise():
	from asset_enterprise.depreciation import enterprise_enabled

	return enterprise_enabled()


class AssetLedgerAdapter:
	app = "asset_enterprise"
	id = "asset.ledger"
	doctypes = OWN + ("Stock Entry",)
	ledger_doctypes = ()

	def governs(self, doc):
		if not _enterprise():
			return False
		if doc.doctype in OWN:
			return True
		# a Material Issue raised by an Asset Capitalization is an artefact
		# of that capitalization, not an independent document
		return bool(doc.get("asset_capitalization"))

	def can_cancel(self, doc):
		if doc.doctype != "Stock Entry":
			return None  # cancels natively; the controller raises the reversal or refuses
		# no route on purpose: the issue is not reversible by itself - the
		# capitalization's reversal posts the Material Receipt that returns the
		# materials, and cancelling the issue would return them a second time
		return Refusal(
			title=_("Reverse the Capitalization"),
			message=_(
				"{0} was raised by Asset Capitalization {1} and cannot be cancelled on its own - the "
				"materials are returned by reversing that capitalization, which posts a Material "
				"Receipt and leaves both movements on the record."
			).format(doc.name, doc.asset_capitalization),
			owner=self.app,
		)

	def can_delete(self, doc):
		if doc.doctype not in REVERSAL_ROUTE or doc.docstatus.is_draft():
			return None  # a draft posted nothing; a capitalization's issue is the stock owners' to judge
		return Refusal(
			title=_("Posted Documents Are Not Deleted"),
			message=_(
				"{0} {1} has been submitted and is part of the ledger - it cannot be deleted. To undo "
				"it, {2}. Deleting it would leave journal entries and depreciation schedules pointing "
				"at a document that no longer exists."
			).format(_(doc.doctype), doc.name, REVERSAL_ROUTE[doc.doctype]),
			owner=self.app,
		)

	def dependents(self, doc):
		return ()

	def actions(self, doc):
		if doc.doctype == "Stock Entry" and doc.get("asset_capitalization"):
			return (
				Action(
					label=_("Open Asset Capitalization"),
					route_to=f"/app/asset-capitalization/{doc.asset_capitalization}",
					primary=False,
				),
			)
		return ()

	def locked_fields(self, doc):
		return ()

	def views(self, doc):
		return ()

	def rows_may_change(self, row_doc):
		return False

	def posts_rows(self, doc):
		return False  # the capitalization posts; the issue's rows are core's or valuation's


def get_registration():
	return Registration(
		app="asset_enterprise",
		api_version=API_VERSION,
		ledger_adapters=(AssetLedgerAdapter(),),
		legacy_hooks=(("Purchase Receipt", "before_cancel"),),
	)
