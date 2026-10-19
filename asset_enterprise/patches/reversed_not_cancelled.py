"""GAP-030 step 4 (19/10/2026): a reversed asset document stays submitted.

Until this release a reversal was raised from the original's on_cancel,
so the framework left the original Cancelled (docstatus 2) although no
ledger entry of it was cancelled - its counter-document posted the
mirror (client, ACC-AVA-2026-00046). This puts those originals back to
Submitted, marked Reversed (`reversed_by_*`, `reversed_on`), and gives
every counter-document the platform's reversal pair (`is_cancellation` /
`cancellation_against`) - the state reversal.CounterDocumentReversal
writes from now on.

An original is restored only where the record agrees it was never undone
in the ledger: its counter-document is submitted and no GL or stock
ledger row and no serial / batch bundle of the original is cancelled.
Anything else is left as it is and named in the migrate log. Idempotent.
"""

import frappe
from frappe.utils import getdate


def execute():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
	from qcs_platform.core import fields as platform_fields

	from asset_enterprise.reversal import REVERSAL_LINKS
	from asset_enterprise.setup.custom_fields import CUSTOM_FIELDS

	# this app's fields (`reversed_on`) and the platform's pair are both
	# installed at after_migrate, which runs after this patch; install
	# them on these doctypes now (both idempotent)
	create_custom_fields({dt: CUSTOM_FIELDS[dt] for dt in REVERSAL_LINKS}, ignore_validate=True)
	platform_fields.install(doctypes=list(REVERSAL_LINKS))

	for doctype, link_field in REVERSAL_LINKS.items():
		of_field = frappe.get_controller(doctype).reversal_of_field
		meta = frappe.get_meta(doctype)
		if not (meta.has_field(of_field) and meta.has_field(link_field)):
			continue
		for counter_name in frappe.get_all(
			doctype, filters={of_field: ("is", "set"), "docstatus": 1}, pluck="name", order_by="creation"
		):
			_settle_pair(doctype, link_field, of_field, frappe.get_doc(doctype, counter_name))


def _settle_pair(doctype, link_field, of_field, counter):
	original = counter.get(of_field)
	if counter.meta.has_field("cancellation_against") and counter.get("cancellation_against") != original:
		counter.db_set({"is_cancellation": 1, "cancellation_against": original}, update_modified=False)

	row = frappe.db.get_value(doctype, original, ["docstatus", link_field, "reversed_on"], as_dict=True)
	if not row:
		print(f"reversed_not_cancelled: {doctype} {counter.name} reverses {original}, which does not exist - skipped")
		return
	reversed_on = getdate(counter.reversal_source_date())
	if row.docstatus == 1:
		if not row.get(link_field) or not row.reversed_on:
			frappe.db.set_value(
				doctype, original,
				{link_field: row.get(link_field) or counter.name, "reversed_on": row.reversed_on or reversed_on},
				update_modified=False,
			)
		return
	if row.docstatus != 2:
		return

	undone = _cancelled_ledger_rows(original)
	if undone:
		print(
			f"reversed_not_cancelled: {doctype} {original} left Cancelled - {undone} of its ledger rows "
			f"are cancelled, so it was undone in the ledger, not only reversed by {counter.name}"
		)
		return

	frappe.db.set_value(
		doctype, original,
		{"docstatus": 1, link_field: counter.name, "reversed_on": reversed_on},
		update_modified=False,
	)
	for df in frappe.get_meta(doctype).get_table_fields():
		frappe.db.sql(
			f"""update `tab{df.options}` set docstatus = 1
			where parent = %s and parenttype = %s and parentfield = %s and docstatus = 2""",
			(original, doctype, df.fieldname),
		)
	frappe.get_doc(doctype, original).add_comment(
		"Info",
		f"Restored to Submitted, marked Reversed by {counter.name} on {reversed_on}: it had been left "
		"Cancelled by the earlier reversal flow, although none of its ledger entries were cancelled "
		"(GAP-030 step 4).",
	)
	print(f"reversed_not_cancelled: {doctype} {original} restored - reversed by {counter.name}")


def _cancelled_ledger_rows(voucher_no):
	"""GL and stock ledger rows of the original that were cancelled, and its
	cancelled Serial and Batch Bundles (the old repair cancel ran
	cancel_sabb), each an undo the restore must not contradict."""
	return (
		frappe.db.count("GL Entry", {"voucher_no": voucher_no, "is_cancelled": 1})
		+ frappe.db.count("Stock Ledger Entry", {"voucher_no": voucher_no, "is_cancelled": 1})
		+ frappe.db.count("Serial and Batch Bundle", {"voucher_no": voucher_no, "docstatus": 2})
	)
