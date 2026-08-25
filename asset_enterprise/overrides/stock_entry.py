"""Stock Entry guards — GA-0005-01.

A Material Issue raised by a Capitalized Maintenance is an ARTEFACT of
that capitalization, not an independent document. Under the immutable
ledger it is never unwound on its own: the capitalization's reversal
posts a Material Receipt that returns the materials, leaving both
movements on the record.

Cancelling the issue directly would return the stock a second time when
that reversal runs — and the desk used to offer exactly that, listing
the Material Issue in its "Cancel All Documents" prompt.
"""

import frappe
from frappe import _


def block_capitalization_issue_cancel(doc, method=None):
	cap = doc.get("asset_capitalization")
	if not cap:
		return
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		return
	frappe.throw(
		_(
			"{0} was raised by Asset Capitalization {1} and cannot be cancelled on its "
			"own — the materials are returned by reversing that capitalization, which "
			"posts a Material Receipt and leaves both movements on the record."
		).format(doc.name, cap),
		title=_("Reverse the Capitalization"),
	)
