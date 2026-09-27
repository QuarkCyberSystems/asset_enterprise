"""Reversing a posted depreciation entry (§4.9 applied to depreciation itself).

A depreciation entry is reversed with a Reversal Journal Entry (GA-0001-01),
one at a time or through a Mass Depreciation Reversal. The entry stays
posted; the schedule has to learn about the reversal, or the period reads
as booked for ever:

- the schedule row keeps its journal entry and gains
  `reversal_journal_entry` (the same mark a merge's straddle reversal
  leaves), and the row's Financial Treatment pairs off;
- the schedule is superseded and its unposted rows regenerated from the
  last period still booked, so the reversed period is due again and posts
  on the next run — mass, per asset or the scheduler (client, 27/09, FA-005:
  after reversing March on ACC-ASS-2026-00040 neither run would re-post it);
- depreciation is reversed latest period first: a period may not be
  reversed while a later one is still booked on the same schedule
  (client, 27/09, FA-006). Reversing from the middle would leave a booked
  period priced from a carrying amount that no longer exists.

Reversals the application raises itself (a merge's straddle rows, a
restore, an adjustment reversal) do their own bookkeeping; they are marked
by `restore._mirror_je` and pass through untouched.
"""

import frappe
from frappe import _
from frappe.utils import formatdate, getdate

SYSTEM_REVERSAL_FLAG = "ae_system_reversal"


def _is_user_reversal(doc):
	return bool(doc.get("reversal_of")) and not doc.flags.get(SYSTEM_REVERSAL_FLAG)


def live_row_for(journal_entry):
	"""The Active-generation schedule row this depreciation entry booked,
	while it is not yet reversed — or None."""
	rows = frappe.db.sql(
		"""
		select ds.name, ds.parent as schedule, ds.schedule_date, ads.asset,
		       ads.finance_book
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ds.journal_entry = %s
		  and ifnull(ds.reversal_journal_entry, '') = ''
		  and ads.status = 'Active' and ads.docstatus = 1
		limit 1
		""",
		journal_entry,
		as_dict=True,
	)
	return rows[0] if rows else None


def later_booked_row(row):
	"""The latest period still booked after `row` on the same schedule."""
	later = frappe.db.sql(
		"""
		select schedule_date, journal_entry from `tabDepreciation Schedule`
		where parent = %s and schedule_date > %s
		  and ifnull(journal_entry, '') != ''
		  and ifnull(reversal_journal_entry, '') = ''
		order by schedule_date desc
		limit 1
		""",
		(row.schedule, row.schedule_date),
		as_dict=True,
	)
	return later[0] if later else None


def reversal_refusal(row):
	"""Why the depreciation booked on `row` may not be reversed now, or
	None. Shared by the Journal Entry guard and Mass Depreciation Reversal
	so both say the same thing."""
	from asset_enterprise.status import off_register

	status = frappe.db.get_value("Asset", row.asset, "status")
	if off_register(status):
		return _(
			"{0} is {1} — its depreciation is reversed only by restoring the asset."
		).format(row.asset, _(status))
	later = later_booked_row(row)
	if later:
		return _(
			"Depreciation on {0} is reversed latest period first: reverse {1} ({2}) "
			"before {3}."
		).format(
			row.asset, formatdate(later.schedule_date), later.journal_entry,
			formatdate(row.schedule_date),
		)
	return None


def validate(doc, method=None):
	"""Journal Entry validate: refuse an out-of-order reversal before it is
	saved, so the user never holds a draft that cannot be submitted."""
	if doc.docstatus == 2 or not _is_user_reversal(doc):
		return
	row = live_row_for(doc.reversal_of)
	if not row:
		return
	refusal = reversal_refusal(row)
	if refusal:
		frappe.throw(refusal, title=_("Depreciation Reversal Refused"))


def on_submit(doc, method=None):
	"""Journal Entry on_submit: mark the row, pair its Financial Treatment,
	and regenerate the schedule so the reversed period is due again."""
	if not _is_user_reversal(doc):
		return
	row = live_row_for(doc.reversal_of)
	if not row:
		return
	# re-checked here: validate ran against the state at save time, and a
	# later period may have posted between save and submit
	refusal = reversal_refusal(row)
	if refusal:
		frappe.throw(refusal, title=_("Depreciation Reversal Refused"))

	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import supersede_and_regenerate

	posting_date = getdate(doc.posting_date)
	frappe.db.set_value(
		"Depreciation Schedule", row.name, "reversal_journal_entry", doc.name,
		update_modified=False,
	)
	ft = frappe.db.get_value(
		"Financial Treatment",
		{
			"asset": row.asset,
			"journal_entry": doc.reversal_of,
			"transaction_category": "Depreciation",
			"status": "Posted",
		},
		"name",
	)
	if ft:
		tcc.reverse(ft, doc, posting_date=posting_date, journal_entry=doc.name)
	else:
		recalculate_asset_values(row.asset)

	supersede_and_regenerate(
		row.asset,
		finance_book=row.finance_book,
		reason=_(
			"depreciation for {0} ({1}) reversed by {2}; the period is due again"
		).format(formatdate(row.schedule_date), doc.reversal_of, doc.name),
		triggered_by=doc,
	)
	recalculate_asset_values(row.asset)
	frappe.get_doc("Asset", row.asset).add_comment(
		"Comment",
		_(
			"Depreciation for {0} ({1}) reversed by {2}. The period is due again and "
			"posts on the next depreciation run."
		).format(formatdate(row.schedule_date), doc.reversal_of, doc.name),
	)
