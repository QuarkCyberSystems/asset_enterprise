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


# --------------------------------------------------------------------------
# Mass Depreciation Reversal (client, 27/09, FA-003)
# --------------------------------------------------------------------------


def execute_mass_reversal(doc):
	"""Reverse the depreciation booked for one period across a scope of
	assets — the counterpart of Mass Asset Depreciation.

	Each entry is reversed by an ordinary Reversal Journal Entry, so every
	rule above applies unchanged: latest period first, the row marked, the
	period due again. An asset that cannot be reversed for the period (a
	later period still booked, the asset off the register) is listed as
	Skipped with the reason; nothing is reversed for it. Any other failure
	stops the whole run, so a submitted run never stands half-posted.
	"""
	from asset_enterprise.api import _assert_reversal_date
	from asset_enterprise.depreciation import enterprise_enabled
	from asset_enterprise.mass_depreciation import assert_authority
	from qcs_platform.core.journal_entry import make_reverse_journal_entry

	if not enterprise_enabled():
		frappe.throw(_("Mass Depreciation Reversal requires Enterprise Assets to be enabled."))
	# reversing depreciation is never routine, whatever the scope
	assert_authority(_("Mass Depreciation Reversal"))

	doc.set("result_summary", [])
	rows = _rows_in_scope(doc)
	if not rows:
		frappe.throw(
			_("No booked depreciation for {0} {1} in this scope — nothing to reverse.").format(
				_(doc.period_month), doc.period_year
			),
			title=_("Nothing to Reverse"),
		)
	latest_source = max(getdate(r.je_posting_date) for r in rows)
	posting_date = _assert_reversal_date(
		doc.company, latest_source, doc.posting_date, _("Depreciation")
	)

	reversed_any = False
	for row in rows:
		# re-read: an earlier reversal in this run superseded the schedule
		live = live_row_for(row.journal_entry)
		refusal = reversal_refusal(live) if live else _("Already reversed.")
		if refusal:
			doc.append(
				"result_summary",
				{
					"asset": row.asset,
					"schedule_date": row.schedule_date,
					"journal_entry": row.journal_entry,
					"outcome": "Skipped",
					"reason": refusal,
				},
			)
			continue
		reversal = make_reverse_journal_entry(row.journal_entry)
		reversal.posting_date = posting_date
		reversal.user_remark = _("Mass Depreciation Reversal {0}: {1}").format(doc.name, doc.reason)
		reversal.flags.ignore_permissions = True
		reversal.submit()
		reversed_any = True
		doc.append(
			"result_summary",
			{
				"asset": row.asset,
				"schedule_date": row.schedule_date,
				"journal_entry": row.journal_entry,
				"reversal_journal_entry": reversal.name,
				"outcome": "Reversed",
			},
		)

	if not reversed_any:
		frappe.throw(
			_("Nothing was reversed for {0} {1}:<br>{2}").format(
				_(doc.period_month), doc.period_year,
				"<br>".join(f"{r.asset}: {r.reason}" for r in doc.result_summary),
			),
			title=_("Nothing to Reverse"),
		)

	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)


def _rows_in_scope(doc):
	"""Booked, unreversed Active-schedule rows dated in the period, latest
	first within each asset so a period holding two rows reverses cleanly."""
	from asset_enterprise.status import OFF_REGISTER

	rows = frappe.db.sql(
		"""
		select ds.name, ds.schedule_date, ds.journal_entry, ads.asset,
		       a.asset_category, je.posting_date as je_posting_date
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		join `tabAsset` a on a.name = ads.asset
		join `tabJournal Entry` je on je.name = ds.journal_entry
		where ads.status = 'Active' and ads.docstatus = 1
		  and a.docstatus = 1 and a.company = %(company)s
		  and a.status not in %(off_register)s
		  and je.docstatus = 1
		  and ifnull(ds.reversal_journal_entry, '') = ''
		  and ds.schedule_date between %(start)s and %(end)s
		order by ads.asset, ds.schedule_date desc, ds.idx desc
		""",
		{
			"company": doc.company,
			"off_register": OFF_REGISTER,
			"start": doc.period_start,
			"end": doc.period_end,
		},
		as_dict=True,
	)
	if doc.mode == "Selected Asset Category":
		rows = [r for r in rows if r.asset_category == doc.asset_category]
	elif doc.mode == "Selected Assets":
		allowed = {r.asset for r in doc.selected_assets}
		rows = [r for r in rows if r.asset in allowed]
	elif doc.mode == "Mass Depreciation Run":
		posted = set(
			frappe.get_all(
				"Mass Asset Depreciation Result",
				filters={"parent": doc.mass_asset_depreciation, "outcome": "Posted"},
				pluck="journal_entry",
			)
		)
		rows = [r for r in rows if r.journal_entry in posted]
	return rows
