"""Which invoice cost lines a capitalizing document consumes (D-054).

A capitalized Asset Repair names its cost by invoice row (a Purchase
Invoice and an expense account); a Capitalized Maintenance service row
names an expense account and, optionally, the Purchase Invoice the service
was bought on. Neither names the invoice LINE. Core posts one credit per
repair invoice row and then merges credits on one account, so a repair
spanning two lines of one invoice, or two invoices on one account, landed
as one credit that a reader had to guess a line for (chief r4 B-1).

This module decides the lines, once, at submit, and records them on the
document (`consumed_cost_lines`, child doctype Asset Consumed Cost Line):

    the invoice's live GL rows on the account, one line per (invoice row,
    cost centre) - the lines the ledger holds;
    ranked: the lines whose accounting dimensions match the consumer's
    (a project first: the dimensions other than cost centre and asset),
    then the consumer's cost centre, then the earliest;
    each line capped at its amount less what other submitted consumers
    already took from it; what cannot be placed is refused.

Each consumed line then gets ONE credit, whose voucher row is the consumed
line's row (the repair's own GL, or the service Journal Entry's row), and
whose dimensions are the consumed line's: capitalizing a cost takes it off
the dimension it was booked on. The cost centre and the asset dimension
stay the consumer's (core's and GAP-023's attribution).

The record is the owner's statement of what it consumed; a reader (the
project ledger) follows the credit's voucher row to it and never infers a
line.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

LINE_DOCTYPE = "Asset Consumed Cost Line"
TABLE = "consumed_cost_lines"
CONSUMERS = ("Asset Repair", "Asset Capitalization")


def _dimensions():
	from asset_enterprise.gl_attribution import dimension_fields

	return [f for f in dimension_fields("GL Entry") if f != "cost_center"]


def invoice_lines(purchase_invoice, account):
	"""The invoice's live lines on `account`: one per (invoice row, cost
	centre, dimensions), earliest first, with its net - one query."""
	dims = _dimensions()
	cols = "".join(f", gl.`{d}`" for d in dims)
	return frappe.db.sql(
		f"""select gl.voucher_detail_no, gl.cost_center {cols},
		       sum(gl.debit - gl.credit) as amount, min(concat(gl.creation, '|', gl.name)) as first
		from `tabGL Entry` gl
		where gl.voucher_type = 'Purchase Invoice' and gl.voucher_no = %s and gl.account = %s
		  and gl.is_cancelled = 0
		group by gl.voucher_detail_no, gl.cost_center {cols}
		having sum(gl.debit - gl.credit) > 0
		order by first""",
		(purchase_invoice, account),
		as_dict=True,
	)


def consumed_elsewhere(purchase_invoice, account, exclude_parent=None):
	"""{(invoice row, cost centre): amount} other SUBMITTED consumers took
	from the invoice's lines on `account` - one query. A cancelled consumer
	(its reversal document stands) has given its lines back."""
	if not frappe.db.table_exists(LINE_DOCTYPE):
		return {}
	joins = " ".join(
		f"left join `tab{dt}` p{i} on l.parenttype = '{dt}' and p{i}.name = l.parent"
		for i, dt in enumerate(CONSUMERS)
	)
	status = "coalesce(" + ", ".join(f"p{i}.docstatus" for i in range(len(CONSUMERS))) + ")"
	return {
		(detail or None, cc or None): flt(amount)
		for detail, cc, amount in frappe.db.sql(
			f"""select l.purchase_invoice_item, l.cost_center, sum(l.amount)
			from `tab{LINE_DOCTYPE}` l {joins}
			where l.purchase_invoice = %s and l.expense_account = %s and l.parent != %s
			  and {status} = 1
			group by l.purchase_invoice_item, l.cost_center""",
			(purchase_invoice, account, exclude_parent or ""),
		)
	}


def _rank(line, dims, cost_center):
	matched = sum(1 for d, v in dims.items() if v and line.get(d) == v)
	return (-matched, 0 if cost_center and line.cost_center == cost_center else 1, line.first)


def allocate(doc, requests):
	"""Fill `doc.consumed_cost_lines` from `requests`: [(source row name,
	purchase invoice, expense account, amount, {dimension: value},
	cost centre)] - one line row per consumed invoice line. Refuses an
	amount the invoice's lines on that account cannot cover."""
	doc.set(TABLE, [])
	if not requests:
		return
	precision = frappe.get_precision(LINE_DOCTYPE, "amount") or 2
	taken = {}
	for source_row, invoice, account, amount, dims, cost_center in requests:
		remaining = flt(amount, precision)
		if remaining <= 0:
			continue
		lines = invoice_lines(invoice, account)
		elsewhere = consumed_elsewhere(invoice, account, exclude_parent=doc.name)
		for line in sorted(lines, key=lambda ln: _rank(ln, dims, cost_center)):
			key = (invoice, account, line.voucher_detail_no or None, line.cost_center or None)
			capacity = flt(line.amount) - elsewhere.get(key[2:], 0) - taken.get(key, 0)
			take = flt(min(capacity, remaining), precision)
			if take <= 0:
				continue
			doc.append(TABLE, {
				"source_row": source_row,
				"purchase_invoice": invoice,
				"purchase_invoice_item": line.voucher_detail_no,
				"expense_account": account,
				"cost_center": line.cost_center,
				"amount": take,
				"dimensions": json.dumps({d: line.get(d) for d in _dimensions()}, sort_keys=True),
			})
			taken[key] = taken.get(key, 0) + take
			remaining = flt(remaining - take, precision)
			if remaining <= 0:
				break
		if remaining > 0:
			frappe.throw(
				_(
					"{0} capitalizes {1} of Purchase Invoice {2} on {3}, but only {4} of that invoice's "
					"cost on the account is left to capitalize (other repairs and capitalizations took the "
					"rest). Reduce the amount or name another invoice."
				).format(
					_(doc.doctype),
					frappe.format(amount, "Currency"),
					invoice,
					account,
					frappe.format(flt(amount) - remaining, "Currency"),
				),
				title=_("Invoice Cost Already Capitalized"),
			)


def line_dimensions(line):
	"""The consumed line's dimensions for its credit (cost centre and asset
	stay the consumer's)."""
	try:
		values = json.loads(line.get("dimensions") or "{}")
	except ValueError:
		values = {}
	return {d: values.get(d) for d in _dimensions()}


def mark_voucher(line, voucher_type, voucher_no, voucher_detail_no):
	"""Record the GL voucher row that carries this line's credit - what a
	reader follows from the credit back to the line."""
	frappe.db.set_value(
		LINE_DOCTYPE,
		line.name,
		{"gl_voucher_type": voucher_type, "gl_voucher_no": voucher_no, "gl_voucher_detail_no": voucher_detail_no},
		update_modified=False,
	)
	line.gl_voucher_type, line.gl_voucher_no, line.gl_voucher_detail_no = voucher_type, voucher_no, voucher_detail_no


def lines_for(doc, source_row):
	return [line for line in (doc.get(TABLE) or []) if line.source_row == source_row]


def header_dimensions(doc):
	return {d: doc.get(d) for d in _dimensions()}
