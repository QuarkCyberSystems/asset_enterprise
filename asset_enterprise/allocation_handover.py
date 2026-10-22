# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Cost Allocation Handover - Stage B of an Asset allocation, on this
module's existing addition path (GA-0002-02 GAP-030, CAR-16, CAR-22, bench
D-079).

A cost allocation run assigns cost to an asset without touching it: it
debits the Asset allocation account on a technical cost centre and keeps
the position pending. An Asset Value Adjustment of type Cost Allocation
Handover takes pending positions through `qcs_platform.handover` - never by
importing the allocating app - as an Addition: core's revaluation entry
debits the asset and credits the allocation account the positions name, on
their cost centre, in the statutory book; the module records the value
change and recalculates depreciation as for any addition.

Reversed, never cancelled (GAP-030 step 4): the Reversal AVA releases the
positions it took before its mirror entry posts, and they are pending again.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

TYPE = "Cost Allocation Handover"
RECEIVER_TYPE = "ASSET"


def lines(ava):
	try:
		return json.loads(ava.get("handover_lines") or "[]")
	except ValueError:
		frappe.throw(_("The handover lines of {0} are not readable; take them again with Get Pending Allocations.").format(ava.name))


@frappe.whitelist()
def get_pending(asset, company):
	"""The asset's pending positions, ready to take: {available, lines,
	total, account, cost_center}."""
	frappe.has_permission("Asset Value Adjustment", "create", throw=True)
	from qcs_platform import handover

	if not handover.available(RECEIVER_TYPE):
		return {"available": False, "lines": [], "total": 0}
	pending = handover.pending(RECEIVER_TYPE, company, {"object": asset})
	return {
		"available": True,
		"lines": [{"key": p.key, "amount": p.amount, "bucket": p.bucket} for p in pending],
		"total": flt(sum(p.amount for p in pending), 2),
		"account": pending[0].account if pending else None,
		"cost_center": pending[0].cost_center if pending else None,
	}


def validate(ava):
	"""The addition is exactly what it takes: the lines' total, their
	allocation account, their cost centre (VR-040)."""
	if ava.get("transaction_type") != TYPE or ava.get("reversal_of_ava"):
		return
	taken = lines(ava)
	if not taken:
		frappe.throw(
			_("A Cost Allocation Handover takes pending allocated cost: use Get Pending Allocations."),
			title=_("Nothing Taken"),
		)
	from qcs_platform import handover

	pending = {p.key: p for p in handover.pending(RECEIVER_TYPE, ava.company, {"object": ava.asset})}
	if ava.docstatus == 0 or ava.is_new():
		missing = [line["key"] for line in taken if line["key"] not in pending]
		if missing:
			frappe.throw(
				_("{0} is not pending for {1}; take the lines again with Get Pending Allocations.").format(
					", ".join(missing), ava.asset
				),
				title=_("Not Pending"),
			)
	named = [pending[line["key"]] for line in taken if line["key"] in pending]
	accounts = {p.account for p in named}
	centres = {p.cost_center for p in named}
	if len(accounts) > 1 or len(centres) > 1:
		frappe.throw(_("The positions taken name different allocation accounts or cost centres; take them in separate adjustments."))
	if named:
		ava.difference_account = named[0].account
		ava.cost_center = named[0].cost_center
	total = flt(sum(flt(line["amount"]) for line in taken), 2)
	if total <= 0:
		frappe.throw(
			_("A Cost Allocation Handover adds value; a negative position is a correction the asset takes "
				"through Initial Impairment once it is reviewed."),
			title=_("Negative Position"),
		)
	ava.set_current_asset_value()  # core's: the carrying value, filled when empty
	ava.new_asset_value = flt(flt(ava.current_asset_value) + total, 2)


def consume(ava):
	"""before_submit: take the lines; the allocating app refuses a take
	beyond the position and admits this document's revaluation entry."""
	if ava.get("transaction_type") != TYPE or ava.get("reversal_of_ava"):
		return
	from qcs_platform import handover
	from qcs_platform.contracts import HandoverLine

	handover.consume(ava, [HandoverLine(RECEIVER_TYPE, line["key"], flt(line["amount"])) for line in lines(ava)])


def release_for_reversal(reversal):
	"""before_submit of a Reversal AVA: give the original's positions back
	before the mirror entry posts."""
	if not reversal.get("reversal_of_ava"):
		return
	original = frappe.get_doc("Asset Value Adjustment", reversal.reversal_of_ava)
	if original.get("transaction_type") != TYPE:
		return
	from qcs_platform import handover

	handover.release(original)
