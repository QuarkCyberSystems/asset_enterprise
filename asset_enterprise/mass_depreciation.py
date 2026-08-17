"""Mass Asset Depreciation — GA-0005-01 v2.14 GAP-005 / VR-006 / VR-007.

Controlled bulk depreciation posting through the enterprise engine.
Restricted modes (Selected Asset Category / Selected Assets) require a
role from the Asset Settings multi-select authority table (per
2026-07-14 meeting). Rows whose schedule entry already carries a
Journal Entry are skipped (VR-007 double-post gate).
"""

import frappe
from frappe import _
from frappe.utils import getdate


def execute_mass_depreciation(doc):
	_check_authority(doc)

	filters = {
		"status": "Active",
		"docstatus": 1,
	}
	rows = frappe.db.sql(
		"""
		select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
		       ds.depreciation_amount, ds.cost_center, ads.asset,
		       ds.daily_rate, ds.days_in_period
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		join `tabAsset` a on a.name = ads.asset
		where ads.status = 'Active' and ads.docstatus = 1
		  -- a reversed or disposed asset keeps its schedule for audit;
		  -- nothing may post against it
		  and a.docstatus = 1
		  and a.status not in ('Disposed', 'Sold', 'Scrapped', 'Capitalized')
		  and a.company = %(company)s
		  and ifnull(ds.journal_entry, '') = ''
		  and ds.schedule_date <= %(posting_date)s
		order by ads.asset, ds.schedule_date
		""",
		{"company": doc.company, "posting_date": getdate(doc.posting_date)},
		as_dict=True,
	)

	rows = _filter_by_mode(doc, rows)
	if not rows:
		doc.append(
			"result_summary",
			{"asset": None, "outcome": "Skipped", "reason": _("No due unposted rows found.")},
		)

	from asset_enterprise.depreciation import (
		_post_one,
		enterprise_enabled,
		final_row_requires_manual_post,
	)

	if not enterprise_enabled():
		frappe.throw(_("Mass Asset Depreciation requires Enterprise Assets to be enabled."))

	for row in rows:
		if final_row_requires_manual_post(row):
			# §4.10 point 4 (v2.16): beyond-tolerance final rows are never
			# auto-posted — surfaced here for the manual/approved flow.
			doc.append(
				"result_summary",
				{
					"asset": row.asset,
					"outcome": "Manual Posting Required",
					"reason": _(
						"Final-row drift exceeds tolerance — post via 'Post Final Row' "
						"with Tolerance Approver approval (§4.10)."
					),
				},
			)
			continue
		already = frappe.db.get_value("Depreciation Schedule", row.row_name, "journal_entry")
		if already:  # VR-007 — re-check at execution time
			doc.append(
				"result_summary",
				{"asset": row.asset, "outcome": "Skipped", "reason": _("Already posted: {0}").format(already)},
			)
			continue
		try:
			_post_one(row, getdate(doc.posting_date))
			je = frappe.db.get_value("Depreciation Schedule", row.row_name, "journal_entry")
			# TC-008 (v2.16 CH-11): visible JE link per posted row.
			doc.append(
				"result_summary",
				{"asset": row.asset, "outcome": "Posted", "reason": je, "journal_entry": je},
			)
		except Exception as e:
			doc.append(
				"result_summary",
				{"asset": row.asset, "outcome": "Failed", "reason": str(e)[:140]},
			)

	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)


def _filter_by_mode(doc, rows):
	if doc.mode == "Selected Asset Category":
		allowed = set(
			frappe.get_all(
				"Asset", filters={"asset_category": doc.asset_category}, pluck="name"
			)
		)
		return [r for r in rows if r.asset in allowed]
	if doc.mode == "Selected Assets":
		allowed = {r.asset for r in doc.selected_assets}
		return [r for r in rows if r.asset in allowed]
	return rows


def _check_authority(doc):
	"""VR-006: restricted modes need a role from the Asset Settings
	multi-select table (per 2026-07-14 meeting)."""
	if doc.mode == "All Eligible":
		return
	allowed_roles = frappe.get_all(
		"Asset Settings Authority Role", filters={"parent": "Asset Settings"}, pluck="role"
	)
	if not allowed_roles:
		frappe.throw(
			_(
				"No Mass Depreciation Authority Roles configured in Asset Settings — "
				"restricted modes are unavailable."
			)
		)
	user_roles = set(frappe.get_roles())
	if not user_roles.intersection(allowed_roles):
		frappe.throw(
			_(
				"Mode '{0}' requires one of the Mass Depreciation Authority Roles: {1}."
			).format(doc.mode, ", ".join(allowed_roles))
		)
