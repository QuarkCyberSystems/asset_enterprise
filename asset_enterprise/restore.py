"""Two-path disposal recovery — GA-0005-01 v2.14 GAP-016 / GAP-029 / VR-033.

Path 1 — same-period accidental-disposal restore (per 2026-07-14
meeting): allowed ONLY when the restore falls in the same depreciation
period (calendar month under the EOM model) as the disposal AND no
depreciation has been posted since. A mirror JE reverses the disposal
(the original stays posted); `scrap_reversal_journal_entry` stores the
mirror; the FT pair nets; future depreciation resumes prospectively.
Applies to full scrap AND partial scrap.

Path 2 — beyond the window: Create Replacement Asset — a new Asset
with a two-way link to the disposed source. No GL is re-posted on the
disposed Asset.

Path 3 (v2.16 CH-08, 2026-07-23 review) — cross-period restore with
catch-up: a Scrapped asset may be restored directly after the window.
The disposal JE is mirrored (value as of the disposal date), the
disposal FT pairs off, and the schedule regenerates from the disposal
date with first_posting_date = restore date, so the FIRST post-restore
depreciation entry accumulates the disposed periods in one posting
(§4.5 mechanics). Finance chooses between Path 2 and Path 3 per case.
"""

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, get_last_day, getdate, nowdate


@frappe.whitelist()
def restore_asset(asset_name):
	"""Whitelisted replacement for core restore_asset (build plan §2.2)."""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		from erpnext.assets.doctype.asset.depreciation import restore_asset as core_restore

		return core_restore(asset_name)

	asset = frappe.get_doc("Asset", asset_name)
	if asset.status != "Scrapped" or not asset.get("journal_entry_for_scrap"):
		frappe.throw(_("Asset {0} is not scrapped — nothing to restore.").format(asset_name))

	_same_period_gate(asset, asset.disposal_date)

	mirror = _mirror_je(
		asset.journal_entry_for_scrap,
		_("Same-period restore of Asset {0}").format(asset.name),
	)

	asset.db_set("scrap_reversal_journal_entry", mirror)
	asset.db_set("status", "Partially Depreciated" if asset.calculate_depreciation else "Submitted")
	asset.db_set("disposal_date", None)

	# Pair the disposal FT; values re-derive to pre-disposal.
	from asset_enterprise import tcc

	disposal_ft = frappe.db.get_value(
		"Financial Treatment",
		{
			"asset": asset.name,
			"transaction_category": "Disposal",
			"status": "Posted",
			"journal_entry": asset.journal_entry_for_scrap,
		},
		"name",
	)
	if disposal_ft:
		tcc.reverse(disposal_ft, ("Asset", asset.name), journal_entry=mirror)

	# Future depreciation resumes prospectively.
	from asset_enterprise.depreciation import supersede_and_regenerate

	try:
		supersede_and_regenerate(
			asset.name, as_of_date=nowdate(), reason=_("Restored via {0}").format(mirror)
		)
	except frappe.ValidationError:
		pass

	from erpnext.assets.doctype.asset_activity.asset_activity import add_asset_activity

	add_asset_activity(
		asset.name,
		_("Same-period restore: disposal reversed via {0}; original scrap JE remains posted.").format(
			mirror
		),
	)
	return mirror


@frappe.whitelist()
def restore_partial_scrap(asset_name, financial_treatment):
	"""Path 1 for a partial scrap: mirror one Partial Disposal FT/JE
	proportionally, under the same same-period / zero-subsequent gate."""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		frappe.throw(_("Partial scrap restore requires Enterprise Assets."))

	ft = frappe.get_doc("Financial Treatment", financial_treatment)
	if ft.asset != asset_name or ft.transaction_category != "Disposal":
		frappe.throw(_("{0} is not a disposal treatment of {1}.").format(ft.name, asset_name))
	if ft.status != "Posted":
		frappe.throw(_("{0} is {1} — only Posted treatments can be restored.").format(ft.name, ft.status))

	asset = frappe.get_doc("Asset", asset_name)
	_same_period_gate(asset, ft.posting_date)

	mirror = _mirror_je(
		ft.journal_entry, _("Partial scrap restore of Asset {0}").format(asset_name)
	)

	from asset_enterprise import tcc

	tcc.reverse(ft.name, ("Asset", asset_name), journal_entry=mirror)

	from asset_enterprise.depreciation import supersede_and_regenerate

	try:
		supersede_and_regenerate(
			asset_name, as_of_date=nowdate(), reason=_("Partial scrap restored via {0}").format(mirror)
		)
	except frappe.ValidationError:
		pass
	return mirror


@frappe.whitelist()
def cross_period_restore(asset_name, restore_date=None):
	"""GAP-016 Path 3 (v2.16): restore a Scrapped asset after the
	same-period window, with catch-up depreciation on the first
	post-restore posting."""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		frappe.throw(_("Cross-period restore requires Enterprise Assets to be enabled."))
	frappe.has_permission("Asset", "write", asset_name, throw=True)

	asset = frappe.get_doc("Asset", asset_name)
	if asset.status != "Scrapped" or not asset.get("journal_entry_for_scrap"):
		frappe.throw(_("Asset {0} is not scrapped — nothing to restore.").format(asset_name))

	restore_date = getdate(restore_date or nowdate())
	disposal_date = getdate(asset.disposal_date)

	mirror = _mirror_je(
		asset.journal_entry_for_scrap,
		_("Cross-period restore (Path 3) of Asset {0} — value as of disposal {1}").format(
			asset.name, disposal_date
		),
	)

	asset.db_set("scrap_reversal_journal_entry", mirror)
	asset.db_set(
		"status", "Partially Depreciated" if asset.calculate_depreciation else "Submitted"
	)
	asset.db_set("disposal_date", None)

	from asset_enterprise import tcc

	disposal_ft = frappe.db.get_value(
		"Financial Treatment",
		{
			"asset": asset.name,
			"transaction_category": "Disposal",
			"status": "Posted",
			"journal_entry": asset.journal_entry_for_scrap,
		},
		"name",
	)
	if disposal_ft:
		tcc.reverse(disposal_ft, ("Asset", asset.name), journal_entry=mirror)

	# Schedule resumes FROM THE DISPOSAL DATE; the first regenerated row
	# catches up every disposed period in one entry (posted on the next
	# depreciation run at/after the restore date). A full scrap left an
	# EMPTY Active schedule, so the pre-disposal horizon is re-derived
	# from the superseded generations.
	from asset_enterprise.depreciation import supersede_and_regenerate

	# All generations count — core's mid-flow proration can leave the
	# original full-horizon schedule Cancelled while the surviving
	# docstatus-1 generations only carry the prorated row.
	horizon = frappe.db.sql(
		"""
		select max(ds.schedule_date)
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s
		""",
		asset.name,
	)[0][0]
	try:
		supersede_and_regenerate(
			asset.name,
			as_of_date=disposal_date,
			first_posting_date=restore_date,
			end_of_life_override=horizon,
			reason=_("Cross-period restore (Path 3) via {0}").format(mirror),
		)
	except frappe.ValidationError:
		pass

	from erpnext.assets.doctype.asset_activity.asset_activity import add_asset_activity

	add_asset_activity(
		asset.name,
		_(
			"Cross-period restore (Path 3): disposal reversed via {0}; first "
			"depreciation after restore catches up periods since {1}."
		).format(mirror, disposal_date),
	)
	return mirror


@frappe.whitelist()
def create_replacement_asset(source_asset):
	"""Path 2: draft a new Asset pre-filled from the disposed source with
	the two-way replacement link. User adjusts values and submits."""
	source = frappe.get_doc("Asset", source_asset)
	if source.status not in ("Scrapped", "Sold", "Capitalized"):
		frappe.throw(
			_("Create Replacement Asset applies to disposed assets; {0} is {1}.").format(
				source.name, source.status
			)
		)

	replacement = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": source.company,
			"item_code": source.item_code,
			"asset_name": _("{0} (Replacement)").format(source.asset_name),
			"asset_category": source.asset_category,
			"location": source.location,
			"cost_center": source.get("cost_center"),
			"is_existing_asset": 1,
			"purchase_date": nowdate(),
			"available_for_use_date": nowdate(),
			# Seeded from the source (core validate rejects zero net
			# purchase); finance adjusts to the recovered value before
			# submitting — per the signed design, values are theirs to set.
			"purchase_amount": source.purchase_amount or source.net_purchase_amount,
			"net_purchase_amount": source.net_purchase_amount or source.purchase_amount,
			"replacement_of_asset": source.name,
		}
	)
	replacement.flags.ignore_permissions = True
	replacement.flags.ignore_links = True  # source may be status-disposed
	replacement.flags.ignore_mandatory = True  # user fills values before submit
	replacement.insert()
	return replacement.name


def _same_period_gate(asset, disposal_date):
	"""VR-033: same calendar month (EOM period model) + zero depreciation
	posted since the disposal."""
	disposal_date = getdate(disposal_date)
	today = getdate(nowdate())
	in_period = get_first_day(today) <= disposal_date <= get_last_day(today)

	posted_since = frappe.db.sql(
		"""
		select count(*) from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		join `tabJournal Entry` je on je.name = ds.journal_entry
		where ads.asset = %s and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') != ''
		  and je.posting_date > %s
		""",
		(asset.name, disposal_date),
	)[0][0]

	if not in_period or posted_since:
		frappe.throw(
			_(
				"Restore window has passed (disposal {0}; same-period restores only, "
				"with no depreciation posted since). Use 'Create Replacement Asset' "
				"(Path 2) or 'Cross-Period Restore' with catch-up depreciation "
				"(Path 3). See GAP-016."
			).format(disposal_date)
		)


def _mirror_je(source_je_name, remark):
	source_je = frappe.get_doc("Journal Entry", source_je_name)
	mirror = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": source_je.company,
			"posting_date": nowdate(),
			"user_remark": remark,
			"accounts": [
				{
					"account": a.account,
					"debit_in_account_currency": flt(a.credit_in_account_currency),
					"credit_in_account_currency": flt(a.debit_in_account_currency),
					"cost_center": a.cost_center,
					"reference_type": a.reference_type,
					"reference_name": a.reference_name,
				}
				for a in source_je.accounts
			],
		}
	)
	mirror.flags.ignore_permissions = True
	mirror.flags.ignore_links = True
	mirror.submit()
	return mirror.name
