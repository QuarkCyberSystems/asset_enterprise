"""The stock an asset transaction consumed comes back when it is reversed.

A Capitalized Maintenance and a capitalized Asset Repair each consume stock
through a Material Issue (the capitalization's own, linked by
`asset_capitalization`; core's repair issue, linked by `asset_repair`).
Reversing the transaction returns that stock, and how is the issue's own
ledger owner's call:

- where another owner of the issue offers a reversal document for it
  (periodic_valuation's Create Cancellation on a routed item), that document
  is raised and submitted inside the platform's `performing_reversal`
  (D-093), so the valuation event reads as a cancellation of the issue,
  netting what it moved (TSK-2026-00952 point 4);
- where that owner REFUSES its reversal - a Cancellation is never
  backdated into a previous period (DR-71) - the asset transaction's
  reversal is refused with the owner's reason, nothing posted, so the
  asset and stock legs always share one date (TSK-2026-01005, D-105);
- where nobody offers one (stock no other owner values), a Material
  Receipt to the source warehouse at the issue's rate returns it (TC-047c).
"""

import frappe
from frappe import _
from frappe.utils import flt, formatdate


def return_consumed_stock(issue, reversal_doc, posting_date, fallback_items, link_field=None):
	"""Return the stock `issue` (a Material Issue, or None) took, for
	`reversal_doc` dated `posting_date`; the returning Stock Entry's name,
	or None when there is nothing to return. `fallback_items` are the
	Material Receipt rows used where no owner offers a reversal of the
	issue; `link_field` names the Stock Entry field that ties the returning
	entry to `reversal_doc` (as the issue was tied to the original)."""
	if issue:
		returned, refusal = _reverse_issue_through_owner(issue, reversal_doc, posting_date, link_field)
		if returned:
			return returned
		if refusal:
			frappe.throw(
				_(
					"{0} cannot be reversed on {1}: the stock it consumed comes back only as the "
					"reversal of its issue {2}, and that reversal is refused - {3} Date the reversal "
					"in the current period."
				).format(reversal_doc.get(_source_field(reversal_doc)) or reversal_doc.name,
					formatdate(posting_date), issue, refusal.rstrip(".") + "."),
				title=_("Reversal Date Refused for the Stock"),
			)
	if not fallback_items:
		return None
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": reversal_doc.company,
		"posting_date": posting_date,
		"set_posting_time": 1,
		"items": fallback_items,
	})
	if link_field:
		se.set(link_field, reversal_doc.name)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def _source_field(reversal_doc):
	return {"Asset Capitalization": "reversal_of_capitalization", "Asset Repair": "reversal_of_repair"}.get(
		reversal_doc.doctype
	)


def _reverse_issue_through_owner(issue, reversal_doc, posting_date, link_field):
	"""(name of the owner's reversal document, None) when another owner of
	the issue offers one and it posts; (None, refusal text) when the offer
	is refused; (None, None) when nobody offers. The attempt runs under a
	savepoint: a refused offer leaves nothing behind."""
	from qcs_platform.ledger.dispatcher import performing_reversal, reversal_offer

	issue_doc = frappe.get_doc("Stock Entry", issue)
	offer = reversal_offer(issue_doc, by="asset_enterprise")
	if offer is None:
		return None, None
	frappe.db.savepoint("ae_issue_reversal")
	try:
		with performing_reversal("asset_enterprise", "Stock Entry", issue):
			name = frappe.get_attr(offer.method)("Stock Entry", issue)
			reversal = frappe.get_doc("Stock Entry", name)
			reversal.posting_date = posting_date
			reversal.set_posting_time = 1
			# the issue's reversal belongs to the transaction's reversal, as
			# the issue belonged to the transaction
			if link_field:
				reversal.set(link_field, reversal_doc.name)
			reversal.flags.ignore_permissions = True
			reversal.save()
			reversal.submit()
			return reversal.name, None
	except frappe.ValidationError as exc:
		frappe.db.rollback(save_point="ae_issue_reversal")
		frappe.clear_last_message()
		return None, frappe.utils.strip_html(str(exc))


def issue_rows_as_receipt(issue):
	"""A Material Issue's rows as Material Receipt rows: back to the source
	warehouse at the issue's rate, crediting the account it debited."""
	return [
		{
			"item_code": r.item_code,
			"qty": flt(r.qty),
			"t_warehouse": r.s_warehouse,
			"basic_rate": flt(r.basic_rate) or None,
			"cost_center": r.cost_center,
			"expense_account": r.expense_account,
		}
		for r in frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": issue},
			fields=["item_code", "qty", "s_warehouse", "basic_rate", "cost_center", "expense_account"],
		)
		if flt(r.qty)
	]
