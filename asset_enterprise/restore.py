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
from frappe.utils import cint, flt, get_first_day, get_last_day, getdate, nowdate

from asset_enterprise.api import assert_reversal_not_before_source



def _charge_if_life_exhausted(asset_name, mirror):
	"""A restore puts value back; if the asset's life is already over the
	regeneration has nothing to charge it through, so VR-018 applies —
	expense the remainder on the restore date instead of leaving it as
	net book value for ever (client, 25/08, ACC-ASS-2026-00217).
	"""
	from asset_enterprise.depreciation import charge_stranded_value

	return charge_stranded_value(
		asset_name,
		getdate(nowdate()),
		("Asset", asset_name),
		transaction_type=_("Immediate Depreciation — Value Restored After End of Life"),
	)


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
	# The same-period gate keeps this inside the disposal's own period,
	# but a disposal dated LATER in that period would still be reversed
	# before it happened — the mirror posts today (VR-022).
	assert_reversal_not_before_source(asset.disposal_date, nowdate(), _("disposal"))

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

	# Future depreciation resumes prospectively. Two traps found in the
	# 19/08 caller audit: (a) as_of must be the last POSTED row, not
	# today, or the gap days get no row; (b) the schedule being
	# superseded was TERMINATED at the disposal date, so the default
	# horizon (its last row) generated NO future rows at all — the asset
	# came back with NBV but a dead schedule. The horizon is re-derived
	# from the finance book, as Path 3 does.
	from asset_enterprise.depreciation import (
		last_posted_schedule_date,
		schedule_horizon_from_life,
		supersede_and_regenerate,
	)

	last_posted = last_posted_schedule_date(asset.name)
	try:
		supersede_and_regenerate(
			asset.name,
			as_of_date=getdate(last_posted) if last_posted else nowdate(),
			end_of_life_override=schedule_horizon_from_life(asset.name),
			reason=_("Restored via {0}").format(mirror),
		)
	except frappe.ValidationError:
		pass
	_charge_if_life_exhausted(asset.name, mirror)

	from asset_enterprise.tcc import add_snapshot_activity

	add_snapshot_activity(
		asset.name,
		_("Same-period restore: disposal reversed via {0}; original scrap JE remains posted.").format(
			mirror
		),
		transaction_type="Restore (Same Period)",
		journal_entry=mirror,
	)
	return mirror


@frappe.whitelist()
def restore_partial_scrap(asset_name, financial_treatment, cross_period=0):
	"""Reverse ONE partial scrap by mirroring its disposal JE
	proportionally. The original entry stays posted.

	A partial scrap gets the same pair of routes a full scrap has
	(client ruling, 25/08 — "treat it same like full scrap reversal"):

	  * inside the window — same period, nothing posted since (VR-033);
	  * `cross_period=1` — after the window, the Path 3 treatment. The
	    mirror posts TODAY and the schedule re-prices from the restored
	    value; periods already posted at the reduced base stay as they
	    are, which is what the immutable ledger requires.

	Until now only the first existed, and the design's stated fallback
	("use Create Replacement Asset") was written for a full scrap: after
	a partial scrap nothing was disposed, so there is no disposed record
	to replace and a new asset would not put the value back.
	"""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		frappe.throw(_("Partial scrap restore requires Enterprise Assets."))
	frappe.has_permission("Asset", "write", asset_name, throw=True)

	ft = frappe.get_doc("Financial Treatment", financial_treatment)
	if ft.asset != asset_name or ft.transaction_category != "Disposal":
		frappe.throw(_("{0} is not a disposal treatment of {1}.").format(ft.name, asset_name))
	if ft.status != "Posted":
		frappe.throw(_("{0} is {1} — only Posted treatments can be restored.").format(ft.name, ft.status))

	asset = frappe.get_doc("Asset", asset_name)
	if not cint(cross_period):
		_same_period_gate(asset, ft.posting_date, partial=True)
	# VR-022 applies to the partial path too: the mirror posts today, and
	# a scrap dated ahead of today would be reversed before it happened.
	assert_reversal_not_before_source(ft.posting_date, nowdate(), _("partial scrap"))

	mirror = _mirror_je(
		ft.journal_entry,
		_("Partial scrap restore of Asset {0}{1}").format(
			asset_name, _(" (cross-period)") if cint(cross_period) else ""
		),
	)

	from asset_enterprise import tcc

	tcc.reverse(ft.name, ("Asset", asset_name), journal_entry=mirror)

	# Resume from the last posted row; the restore date is the
	# rate-change boundary (value comes back today — days before it stay
	# at the post-scrap rate that was in force).
	from asset_enterprise.depreciation import (
		last_posted_schedule_date,
		supersede_and_regenerate,
	)

	last_posted = last_posted_schedule_date(asset_name)
	try:
		supersede_and_regenerate(
			asset_name,
			as_of_date=getdate(last_posted) if last_posted else nowdate(),
			rate_change_date=getdate(nowdate()) if last_posted else None,
			reason=_("Partial scrap restored via {0}").format(mirror),
		)
	except frappe.ValidationError:
		pass
	_charge_if_life_exhausted(asset_name, mirror)

	# Say so on the scrap record itself, or it keeps reading as live —
	# the same record-versus-ledger mismatch that bit the movement fix.
	scrap = frappe.db.get_value(
		"Scrap Transaction",
		{"journal_entry": ft.journal_entry, "docstatus": 1},
		["name", "composite_component"],
		as_dict=True,
	)
	component_note = ""
	if scrap:
		# A COMPONENT scrap also consumed a Merge Log row (CH-09,
		# disposal.py marks it "Scrapped"). Reversing the money without
		# reversing that leaves the composite showing a component it still
		# owns as gone — and blocks scrapping it again, because the picker
		# only offers Active rows (client, 25/08: "should take the
		# component scrap in consideration").
		if scrap.composite_component:
			row = frappe.db.get_value(
				"Composite Merge Log Entry",
				{
					"parent": asset_name,
					"parenttype": "Asset",
					"merged_source_asset": scrap.composite_component,
					"status": "Scrapped",
				},
				"name",
			)
			if row:
				frappe.db.set_value(
					"Composite Merge Log Entry", row, "status", "Active",
					update_modified=False,
				)
				component_note = _(" Component {0} is back on the Merge Log as Active.").format(
					scrap.composite_component
				)
		frappe.db.set_value(
			"Scrap Transaction", scrap.name, "reversal_journal_entry", mirror,
			update_modified=False,
		)
		frappe.get_doc("Scrap Transaction", scrap.name).add_comment(
			"Comment",
			_("Reversed via {0}{1}. The original entry stays posted.{2}").format(
				mirror, _(" (cross-period)") if cint(cross_period) else "", component_note
			),
		)

	from asset_enterprise.tcc import add_snapshot_activity

	add_snapshot_activity(
		asset_name,
		_("Partial scrap reversed via {0}{1}; the original entry stays posted.{2}").format(
			mirror, _(" — cross-period") if cint(cross_period) else "", component_note
		),
		transaction_type="Restore (Partial Scrap)",
		journal_entry=mirror,
	)
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
	# VR-022: the restore may be in a LATER period — that is what Path 3
	# is for — but never in an earlier one than the disposal it reverses.
	assert_reversal_not_before_source(disposal_date, restore_date, _("disposal"))

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
	from asset_enterprise.depreciation import schedule_horizon_from_life, supersede_and_regenerate

	# End of life comes from the FINANCE BOOK, not from leftover rows.
	# Reading it back off the schedules only ever worked because core's
	# cancel-and-recreate left the full-horizon generation behind as a
	# cancelled document; under supersession a scrap that had booked
	# nothing legitimately drops that generation, and the max() went
	# NULL — the restore then regenerated no rows at all.
	horizon = schedule_horizon_from_life(asset.name) or frappe.db.sql(
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
	_charge_if_life_exhausted(asset.name, mirror)

	from asset_enterprise.tcc import add_snapshot_activity

	add_snapshot_activity(
		asset.name,
		_(
			"Cross-period restore (Path 3): disposal reversed via {0}; first "
			"depreciation after restore catches up periods since {1}."
		).format(mirror, disposal_date),
		transaction_type="Restore (Cross-Period)",
		journal_entry=mirror,
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
	from asset_enterprise.tcc import add_snapshot_activity

	add_snapshot_activity(
		source.name,
		_("Replacement asset {0} drafted (GAP-016 Path 2).").format(replacement.name),
		transaction_type="Replacement Drafted",
	)
	return replacement.name


def _same_period_gate(asset, disposal_date, partial=False):
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
		if partial:
			# Create Replacement Asset is meaningless here: nothing was
			# disposed, so there is no record to replace.
			frappe.throw(
				_(
					"Restore window has passed (partial scrap {0}; same-period reversals "
					"only, with no depreciation posted since). Use 'Cross-Period Reverse "
					"Partial Scrap' — the value returns today and the schedule re-prices "
					"from there. See GAP-016."
				).format(disposal_date)
			)
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
			# Keep the source's voucher type — core requires Depreciation
			# Entry for JEs carrying asset depreciation references.
			"voucher_type": source_je.voucher_type,
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
	# §12.22 / GA-0001-01 (Phase 11b T10): carry the two-way JE
	# back-references when the site has the reversal fields.
	je_meta = frappe.get_meta("Journal Entry")
	if je_meta.has_field("reversal_of"):
		mirror.reversal_of = source_je_name
	mirror.flags.ignore_permissions = True
	mirror.flags.ignore_links = True
	mirror.submit()
	if je_meta.has_field("reversed_by"):
		frappe.db.set_value(
			"Journal Entry", source_je_name, "reversed_by", mirror.name, update_modified=False
		)
	return mirror.name
