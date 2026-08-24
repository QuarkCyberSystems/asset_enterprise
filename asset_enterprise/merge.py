"""Composite merge machinery — GA-0005-01 v2.14 GAP-014/015/017/035.

Capitalized Maintenance (Standard Maintenance): each source Asset is
DISPOSED INTO the composite through the Capitalization Clearing
account (per 2026-07-14 meeting — no direct FA-to-FA transfer):

    Component disposal leg:
        DR Accumulated Depreciation (source)   [Accum at merge]
        DR Capitalization Clearing             [NBV]
           CR Fixed Asset Cost (source)        [HAV / gross]
    Composite addition leg:
        DR Fixed Asset (composite)             [NBV]
           CR Capitalization Clearing          [NBV]

One balanced JE carries both legs; the clearing account nets to zero
per merge. The source Asset leaves the active register (status
"Disposed", document stays submitted
via db_set — its booking GL is NOT re-reversed; the disposal leg above
is the ledger truth), gets `merged_into_asset`, and the composite's
Merge Log snapshots HAV / Accum / NBV at merge (values tracked for
later scrap sizing; NO per-component schedules).

Reversal of Capitalized Maintenance mirrors both legs in a new JE,
pairs the FTs, marks Merge Log rows Reversed, and leaves source
Assets cancelled — finance re-creates records manually (seeded from
the snapshot) per the signed design.
"""

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, date_diff, flt, getdate, nowdate

from asset_enterprise.accounts import get_enterprise_account
from asset_enterprise.rounding import fa_module_round


def _category_accounts(asset):
	aca = frappe.db.get_value(
		"Asset Category Account",
		{"parent": asset.asset_category, "company_name": asset.company},
		["fixed_asset_account", "accumulated_depreciation_account"],
		as_dict=True,
	)
	if not aca or not aca.fixed_asset_account:
		frappe.throw(
			_("Asset Category Account missing for {0} / {1}.").format(
				asset.asset_category, asset.company
			)
		)
	return aca


def merge_sources_into_composite(cap_doc):
	"""Execute the two-leg merge for every asset_items row of a
	Capitalized Maintenance capitalization. Returns the merge JE name."""
	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values
	from erpnext.assets.doctype.asset.depreciation import depreciate_asset

	target = frappe.get_doc("Asset", cap_doc.target_asset)
	company = target.company
	clearing = get_enterprise_account(
		"capitalization_clearing_account", company, target.asset_category
	)
	target_accounts = _category_accounts(target)
	posting_date = getdate(cap_doc.posting_date or nowdate())

	je_accounts = []
	merged = []  # (source_doc, snapshot)

	for row in cap_doc.asset_items:
		source = frappe.get_doc("Asset", row.asset)
		if source.docstatus != 1:
			frappe.throw(_("Source Asset {0} must be submitted.").format(source.name))
		if source.name == target.name:
			frappe.throw(_("An asset cannot be merged into itself."))

		# GAP-015 step 1 (Phase 11 F6): a full-period depreciation JE
		# already posted PAST the merge date is reversed via mirror JE
		# (original stays posted); the row drops out of the accum fold.
		_reverse_straddling_depreciation(source.name, posting_date, cap_doc)

		# Mid-period proration (GAP-015) via existing engine.
		if source.calculate_depreciation:
			try:
				depreciate_asset(source, posting_date, _("Merge into {0}").format(target.name))
				source.reload()
				# core only RESHAPES the schedule to the merge date; the
				# prorated row still has to be posted, or the composite
				# absorbs the source at its un-depreciated value and the
				# last days of use are never expensed (TC-029 / TC-049).
				from asset_enterprise.depreciation import post_schedule_entries

				active = frappe.db.get_value(
					"Asset Depreciation Schedule",
					{"asset": source.name, "status": "Active", "docstatus": 1},
					"name",
				)
				if active:
					post_schedule_entries(active, posting_date)
					source.reload()
			except Exception:
				frappe.log_error(
					title=f"asset_enterprise: merge proration failed for {source.name}",
					message=frappe.get_traceback(),
				)

		values = recalculate_asset_values(source.name, save=False)
		hav = flt(values["historical_asset_value"])
		accum = flt(values["accumulated_depreciation_value"])
		nbv = fa_module_round(hav - accum, company)
		# CH-06: the Merge Log snapshots CALENDAR remaining life — what
		# the component still brings to the composite. The displayed RUL
		# is posting-based and reads 0 here (the source's schedule is
		# already terminated at the merge date).
		from asset_enterprise.asset_values import calendar_remaining_life_months

		rul_months = calendar_remaining_life_months(source)
		if nbv < 0:
			frappe.throw(_("Source Asset {0} has negative NBV.").format(source.name))

		src_accounts = _category_accounts(source)
		cc = row.get("cost_center") or source.get("cost_center")

		# Disposal leg (source).
		if accum:
			je_accounts.append(
				{
					"account": src_accounts.accumulated_depreciation_account,
					"debit_in_account_currency": accum,
					"cost_center": cc,
					"reference_type": "Asset",
					"reference_name": source.name,
				}
			)
		if nbv:
			je_accounts.append(
				{
					"account": clearing,
					"debit_in_account_currency": nbv,
					"cost_center": cc,
				}
			)
		je_accounts.append(
			{
				"account": src_accounts.fixed_asset_account,
				"credit_in_account_currency": hav,
				"cost_center": cc,
				"reference_type": "Asset",
				"reference_name": source.name,
			}
		)
		# Addition leg (composite).
		if nbv:
			je_accounts.append(
				{
					"account": target_accounts.fixed_asset_account,
					"debit_in_account_currency": nbv,
					"cost_center": target.get("cost_center"),
					"reference_type": "Asset",
					"reference_name": target.name,
				}
			)
			je_accounts.append(
				{
					"account": clearing,
					"credit_in_account_currency": nbv,
					"cost_center": target.get("cost_center"),
				}
			)

		merged.append((source, {"hav": hav, "accum": accum, "nbv": nbv, "rul_months": rul_months}))

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": company,
			"posting_date": posting_date,
			"user_remark": _("Capitalized Maintenance merge via {0}").format(cap_doc.name),
			"accounts": je_accounts,
		}
	)
	je.flags.ignore_permissions = True
	je.submit()

	total_nbv = 0.0
	for source, snap in merged:
		# Source leaves the active register; booking GL untouched
		# (the disposal leg above is the ledger truth).
		# A merge DISPOSES of the source; it does not cancel the document
		# that created it. The asset stays submitted — as a scrapped or
		# sold asset does — and leaves the active register by status
		# (client, sheet item 24).
		source.db_set("status", "Disposed", update_modified=False)
		source.db_set("merged_into_asset", target.name, update_modified=False)

		target.append(
			"merge_log",
			{
				"merged_source_asset": source.name,
				"merged_source_asset_name": source.asset_name,
				"merged_date": posting_date,
				"merged_via_capitalization": cap_doc.name,
				"historical_value_at_merge": snap["hav"],
				"accumulated_depreciation_at_merge": snap["accum"],
				"net_book_value_at_merge": snap["nbv"],
				# v2.16 CH-06: RUL snapshot for later scrap sizing.
				"remaining_useful_life_in_months": int(round(snap["rul_months"])),
				"remaining_useful_life_in_years": flt(snap["rul_months"] / 12, 2),
				"status": "Active",
			},
		)

		tcc.apply(
			source_doc=cap_doc,
			category="Disposal",
			transaction_type="Merged into Composite",
			asset=source.name,
			posting_date=posting_date,
			amount=snap["nbv"],
			hav_delta=-snap["hav"],
			accum_delta=-snap["accum"],
			journal_entry=je.name,
		)
		tcc.apply(
			source_doc=cap_doc,
			category="Addition",
			# GAP-017 (Phase 11b): the composite's history entry names the
			# merged source directly.
			transaction_type=_("Capitalized Maintenance ({0})").format(source.name),
			asset=target.name,
			posting_date=posting_date,
			amount=snap["nbv"],
			hav_delta=snap["nbv"],
			journal_entry=je.name,
		)
		total_nbv += snap["nbv"]

	# Persist merge log rows without re-running Asset validations. The
	# rows deliberately link cancelled sources (that IS the audit trail).
	target.flags.ignore_validate_update_after_submit = True
	target.flags.ignore_permissions = True
	target.flags.ignore_links = True
	target.flags.via_capitalization = True  # sanctioned Merge Log writer (VR-039)
	target.save()

	_apply_fully_depreciated_treatment(cap_doc, target, total_nbv, posting_date)
	_resupersede(target.name, posting_date, cap_doc)
	# The composite's stored Historical / Accumulated / Net Book values are
	# what the form shows. Without this the merge left them at their
	# pre-merge figures until someone pressed Recalculate — reported by
	# the client 16/08/2026.
	recalculate_asset_values(target.name)
	for row in cap_doc.asset_items:
		recalculate_asset_values(row.asset)
	return je.name


def _reverse_straddling_depreciation(asset_name, merge_date, cap_doc):
	"""Mirror-reverse posted depreciation rows dated after the merge
	date. The row keeps its JE (immutable) and gains
	`reversal_journal_entry`; its row-backed FT pairs off."""
	from asset_enterprise import tcc
	from asset_enterprise.restore import _mirror_je

	rows = frappe.db.sql(
		"""
		select ds.name, ds.journal_entry, ds.schedule_date
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') != ''
		  and ifnull(ds.reversal_journal_entry, '') = ''
		  and ds.schedule_date > %s
		""",
		(asset_name, merge_date),
		as_dict=True,
	)
	for row in rows:
		mirror = _mirror_je(
			row.journal_entry,
			_("Reversal of straddling depreciation ({0}) on merge via {1}").format(
				row.schedule_date, cap_doc.name
			),
		)
		frappe.db.set_value(
			"Depreciation Schedule", row.name, "reversal_journal_entry", mirror,
			update_modified=False,
		)
		ft = frappe.db.get_value(
			"Financial Treatment",
			{
				"asset": asset_name,
				"journal_entry": row.journal_entry,
				"transaction_category": "Depreciation",
				"status": "Posted",
			},
			"name",
		)
		if ft:
			tcc.reverse(ft, cap_doc, posting_date=getdate(cap_doc.posting_date or nowdate()),
				journal_entry=mirror)


def _apply_fully_depreciated_treatment(cap_doc, target, total_nbv, posting_date):
	"""GAP-014 both-options support (per 2026-07-14 meeting)."""
	treatment = cap_doc.get("fully_depreciated_treatment")
	if not treatment or not total_nbv:
		return
	from asset_enterprise import tcc

	if treatment == "Expense Immediately":
		accounts = _category_accounts(target)
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Depreciation Entry",
				"company": target.company,
				"posting_date": posting_date,
				"user_remark": _("Immediate depreciation of merged value per {0}").format(
					cap_doc.name
				),
				"accounts": [
					{
						"account": frappe.db.get_value(
							"Asset Category Account",
							{"parent": target.asset_category, "company_name": target.company},
							"depreciation_expense_account",
						),
						"debit_in_account_currency": total_nbv,
						"cost_center": target.get("cost_center"),
					},
					{
						"account": accounts.accumulated_depreciation_account,
						"credit_in_account_currency": total_nbv,
					},
				],
			}
		)
		je.flags.ignore_permissions = True
		je.submit()
		tcc.apply(
			source_doc=cap_doc,
			category="Depreciation",
			transaction_type="Fully-Depreciated Merge — Expense Immediately",
			asset=target.name,
			posting_date=posting_date,
			amount=total_nbv,
			accum_delta=total_nbv,
			journal_entry=je.name,
		)


def _resupersede(target_name, posting_date, cap_doc):
	# Rows resume from where POSTING stopped, not from the capitalization
	# date — a mid-month merge otherwise left the days before it with no
	# depreciation at all (same defect the client reported on the invoice
	# adjustment, sheet items 4 and 5).
	from asset_enterprise.depreciation import regenerate_after_value_change

	from asset_enterprise.depreciation import (
		active_schedule_horizon,
		bump_useful_life_periods,
	)

	end_override = None
	# Extend the horizon ONLY when life was actually granted. With
	# "Add Value and Extend Life" and zero months, this collapsed the
	# asset's life to the capitalization date: the whole remaining value
	# was crammed into the last two periods and every future row vanished
	# (client, ACC-ASS-2026-00102 — Extended Life 0).
	extra = flt(cap_doc.get("extended_life_months") or 0)
	# Days combine with months and shift the end of life without touching
	# the period count — the AVA's rule (client, 21/08).
	extra_days = cint(cap_doc.get("extended_life_days") or 0)

	# A reversal RETRACTS whatever the original granted. Its own fields
	# are empty (the reversal document only carries the target), so the
	# grant is read back from the source — otherwise the value came off
	# but the life stayed extended.
	if cap_doc.get("transaction_type") == "Reversal of Capitalized Maintenance" and cap_doc.get(
		"reversal_of_capitalization"
	):
		source = frappe.db.get_value(
			"Asset Capitalization",
			cap_doc.reversal_of_capitalization,
			["extended_life_months", "extended_life_days", "fully_depreciated_treatment"],
			as_dict=True,
		) or frappe._dict()
		src_months = flt(source.get("extended_life_months") or 0)
		src_days = cint(source.get("extended_life_days") or 0)
		if src_months or src_days:
			horizon = active_schedule_horizon(target_name)
			if source.get("fully_depreciated_treatment") == "Add Value and Extend Life":
				# The target had no life left before the merge — hand it
				# back exhausted rather than to an arithmetic remainder.
				from asset_enterprise.depreciation import last_posted_schedule_date

				end_override = getdate(last_posted_schedule_date(target_name) or posting_date)
			elif horizon:
				end_override = add_days(
					add_months(getdate(horizon), -int(round(src_months))), -src_days
				)
				bump_useful_life_periods(target_name, -src_months, -src_days)
	elif cap_doc.get("fully_depreciated_treatment") == "Add Value and Extend Life" and (
		extra > 0 or extra_days > 0
	):
		# fully-depreciated target: no life remains — the added value
		# gets a fresh life anchored at the merge date.
		end_override = add_days(add_months(posting_date, extra), extra_days)
	elif extra > 0 or extra_days > 0:
		# LIVING target (client, 19/08 — an overhaul that prolongs
		# service life): the months extend the CURRENT end of life,
		# exactly like a Useful Life Adjustment folded into the merge.
		# Anchoring at the posting date here is what collapsed
		# ACC-ASS-2026-00127's remaining 4.5 years into 12 months.
		horizon = active_schedule_horizon(target_name)
		if horizon:
			end_override = add_days(
				add_months(getdate(horizon), int(round(extra))), extra_days
			)
			bump_useful_life_periods(target_name, extra, extra_days)
	try:
		regenerate_after_value_change(
			target_name,
			posting_date,
			_("Capitalized Maintenance {0}").format(cap_doc.name),
			end_of_life_override=end_override,
			triggered_by=cap_doc,
		)
	except frappe.ValidationError:
		pass  # composite without an Active schedule — value-only


def capitalize_service_costs(cap_doc):
	"""§12.3 / TC-027 / TC-046: capitalize SERVICE costs onto a SUBMITTED
	target asset (Capitalized Maintenance, no asset merge involved).

	    DR Fixed Asset Cost (target)   [capitalized amount]
	       CR Service Expense Account  [per service row]

	The target's HAV rises by the capitalized amount and its schedule is
	recalculated prospectively via the centralized rule. Stock rows are
	not handled here — they consume inventory and route through Asset
	Repair (Capitalized Repair).
	"""
	from asset_enterprise import tcc

	target = frappe.get_doc("Asset", cap_doc.target_asset)
	posting_date = getdate(cap_doc.posting_date or nowdate())
	rows = [r for r in (cap_doc.get("service_items") or []) if flt(r.amount)]
	if not rows:
		if cap_doc.get("service_items"):
			frappe.throw(
				_(
					"Every service row on {0} has a zero amount — nothing would be "
					"capitalized onto {1}. Set the quantity and rate."
				).format(cap_doc.name, cap_doc.target_asset)
			)
		return None

	total = 0.0
	accounts = []
	for row in rows:
		if not row.get("expense_account"):
			frappe.throw(
				_("Service row {0} ({1}): set an Expense Account — it is the credit "
				  "side of the capitalization (§12.3).").format(row.idx, row.item_code)
			)
		amount = fa_module_round(flt(row.amount), target.company)
		accounts.append(
			{
				"account": row.expense_account,
				"credit_in_account_currency": amount,
				"cost_center": row.get("cost_center") or target.get("cost_center"),
			}
		)
		total = fa_module_round(total + amount, target.company)

	tgt_accounts = _category_accounts(target)
	accounts.insert(
		0,
		{
			"account": tgt_accounts.fixed_asset_account,
			"debit_in_account_currency": total,
			"cost_center": target.get("cost_center"),
			"reference_type": "Asset",
			"reference_name": target.name,
		},
	)

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": target.company,
			"posting_date": posting_date,
			"user_remark": _("Capitalized Maintenance (service) via {0}").format(cap_doc.name),
			"accounts": accounts,
		}
	)
	je.flags.ignore_permissions = True
	je.submit()

	tcc.apply(
		source_doc=cap_doc,
		category="Addition",
		transaction_type="Capitalized Maintenance — Service",
		asset=target.name,
		posting_date=posting_date,
		amount=total,
		hav_delta=total,
		journal_entry=je.name,
	)
	# Prospective recalculation over the increased base (TC-027/TC-046).
	_resupersede(target.name, posting_date, cap_doc)
	from asset_enterprise.asset_values import recalculate_asset_values as _recalc

	_recalc(target.name)
	return je.name


def reclassify(cap_doc):
	"""GAP-014 Reclassification sub-type (Phase 11 F5) — §3.6/§12.13
	corrected model: standard Disposal (old category) + Addition (new
	category) in ONE JE, NO clearing account, gross AND accumulated
	depreciation re-established under the new category. The source is
	Cancelled under the old category; the pre-created new-category
	asset takes over with a two-way `reclassified_from` trace. No
	Merge Log rows (reclassification is a category move, not a merge).
	"""
	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values
	from erpnext.assets.doctype.asset.depreciation import depreciate_asset

	if len(cap_doc.asset_items or []) != 1:
		frappe.throw(_("Reclassification moves exactly one source Asset."))
	source = frappe.get_doc("Asset", cap_doc.asset_items[0].asset)
	if source.docstatus != 1:
		frappe.throw(_("Source Asset {0} must be submitted.").format(source.name))
	posting_date = getdate(cap_doc.posting_date or nowdate())

	if cap_doc.get("target_asset"):
		# Legacy path: a draft asset in the new category, pre-created by
		# the user, is taken over (v2.14 §3.6).
		target = frappe.get_doc("Asset", cap_doc.target_asset)
		cap_doc.db_set("created_asset", target.name, update_modified=False)
	else:
		# Target named as a MATERIAL (client, 19/08): the transaction
		# creates the new-category asset itself, like purchasing. The
		# machine has not moved — location and cost centre carry over;
		# available-for-use is the transaction date.
		item = frappe.get_doc("Item", cap_doc.target_item)
		target = frappe.get_doc(
			{
				"doctype": "Asset",
				"company": source.company,
				"asset_name": item.item_name or item.name,
				"item_code": item.name,
				"asset_category": item.asset_category,
				"location": source.location,
				"cost_center": source.get("cost_center"),
				"asset_quantity": source.get("asset_quantity") or 1,
				"purchase_date": posting_date,
				"available_for_use_date": posting_date,
				"calculate_depreciation": 0,
				"purchase_amount": flt(source.net_purchase_amount),
				"net_purchase_amount": flt(source.net_purchase_amount),
				"reclassified_from": source.name,
			}
		)
		target.flags.ignore_permissions = True
		target.insert()
		cap_doc.db_set("target_asset", target.name, update_modified=False)
		cap_doc.db_set("target_asset_category", item.asset_category, update_modified=False)
		# The Target Asset picker is hidden on this sub-type, so the asset
		# this transaction created is surfaced as an OUTPUT (client, 24/08).
		cap_doc.db_set("created_asset", target.name, update_modified=False)

	# Standard-disposal proration up to the transfer date.
	if source.calculate_depreciation:
		try:
			depreciate_asset(source, posting_date, _("Reclassified via {0}").format(cap_doc.name))
			source.reload()
		except Exception:
			pass

	values = recalculate_asset_values(source.name, save=False)
	gross = flt(values["historical_asset_value"])
	accum = flt(values["accumulated_depreciation_value"])
	source_fb = frappe.db.get_value(
		"Asset Finance Book",
		{"parent": source.name},
		[
			"total_number_of_depreciations", "frequency_of_depreciation",
			"depreciation_start_date", "expected_value_after_useful_life",
		],
		as_dict=True,
	)
	salvage = flt(source_fb.expected_value_after_useful_life if source_fb else 0)

	src_accounts = _category_accounts(source)
	tgt_accounts = _category_accounts(target)
	cc = source.get("cost_center")

	accounts = []
	if accum:
		accounts.append(
			{
				"account": src_accounts.accumulated_depreciation_account,
				"debit_in_account_currency": accum,
				"cost_center": cc,
				"reference_type": "Asset",
				"reference_name": source.name,
			}
		)
	accounts.append(
		{
			"account": src_accounts.fixed_asset_account,
			"credit_in_account_currency": gross,
			"cost_center": cc,
			"reference_type": "Asset",
			"reference_name": source.name,
		}
	)
	accounts.append(
		{
			"account": tgt_accounts.fixed_asset_account,
			"debit_in_account_currency": gross,
			"cost_center": target.get("cost_center") or cc,
			"reference_type": "Asset",
			"reference_name": target.name,
		}
	)
	if accum:
		accounts.append(
			{
				"account": tgt_accounts.accumulated_depreciation_account,
				"credit_in_account_currency": accum,
				"reference_type": "Asset",
				"reference_name": target.name,
			}
		)

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			# opening-style booking under the new category — not a
			# revaluation for the Fixed Asset Register's adjustment map
			"is_opening": "Yes",
			"company": source.company,
			"posting_date": posting_date,
			"user_remark": _("Reclassification of {0} to category {1} via {2}").format(
				source.name, target.asset_category, cap_doc.name
			),
			"accounts": accounts,
		}
	)
	je.flags.ignore_permissions = True
	je.flags.ignore_links = True
	je.submit()

	# Source leaves the register under the old category. §3.6: "The source
	# Asset gets CANCELLED under the old category account structure" — so
	# the status has to say Cancelled too. It said "Capitalized", which is
	# core's word for an asset consumed into a COMPOSITE, and the same
	# section is explicit that a reclassification "is a category move, not
	# a merge": the document header read Cancelled while the Status field
	# claimed a merge that never happened (client, 24/08).
	source.db_set("status", "Cancelled", update_modified=False)
	source.db_set("docstatus", 2, update_modified=False)

	# Target carries gross + opening accum; the reclassification JE is
	# its booking — GAP-001's suspense path is suppressed by the trace.
	target.db_set("reclassified_from", source.name, update_modified=False)
	if target.docstatus == 0:
		target.purchase_amount = gross
		target.net_purchase_amount = gross
		target.opening_accumulated_depreciation = accum
		target.reclassified_from = source.name
		# available-for-use = the transaction date (client, 19/08) —
		# holds for auto-created targets and pre-created drafts alike.
		target.available_for_use_date = posting_date
		target.flags.ignore_permissions = True
		target.flags.ignore_links = True
		target.save()
		target.submit()

	tcc.apply(
		source_doc=cap_doc,
		category="Disposal",
		transaction_type="Reclassification — Out",
		asset=source.name,
		posting_date=posting_date,
		amount=gross,
		hav_delta=-gross,
		accum_delta=-accum,
		journal_entry=je.name,
	)
	# Target values derive from net_purchase_amount + opening accum —
	# the FT is the audit record + JE link (zero deltas, GAP-001 style).
	tcc.apply(
		source_doc=cap_doc,
		category="Addition",
		transaction_type="Reclassification — In",
		asset=target.name,
		posting_date=posting_date,
		amount=gross,
		journal_entry=je.name,
	)

	# The new asset's period count describes its WHOLE useful life under
	# the NEW category (client, 24/08: "the total number of dep should be
	# 36 as the asset category ... describe the asset whole life"). The
	# life already used up on the predecessor is not netted off that
	# count — it is recorded as Life Consumed Before Transfer (Days) and
	# comes off the horizon instead, so the asset still ends where it
	# always would have. Before this, the count silently read 32.
	if source.calculate_depreciation and not target.calculate_depreciation:
		from asset_enterprise.depreciation import enable_depreciation

		whole_life = _category_life_months(target) or (
			cint(source_fb.total_number_of_depreciations)
			* (cint(source_fb.frequency_of_depreciation) or 1)
			if source_fb
			else 0
		)
		# Days already depreciated, counted from when the asset first went
		# into service to the day it moves category.
		source_start = getdate(
			source.available_for_use_date
			or (source_fb.depreciation_start_date if source_fb else posting_date)
		)
		consumed_days = max(0, date_diff(posting_date, source_start))
		if whole_life < 1:
			frappe.log_error(
				title=f"Reclassification schedule reset skipped: {target.name}",
				message=(
					"Neither the target category nor the source finance book states a "
					"useful life, so no schedule could be built."
				),
			)
			return je.name
		try:
			enable_depreciation(
				target.name,
				total_number_of_depreciations=whole_life,
				frequency_of_depreciation=1,
				depreciation_start_date=add_days_safe(posting_date),
				expected_value_after_useful_life=salvage,
				prior_life_days=consumed_days,
				# The predecessor charged through the transfer date itself
				# (its final prorated row), so charging resumes the day
				# after — otherwise that day is billed on both assets.
				depreciation_basis_date=add_days_safe(posting_date),
			)
		except Exception:
			frappe.log_error(
				title=f"Reclassification schedule reset failed: {target.name}",
				message=frappe.get_traceback(),
			)
		else:
			_note_consumed_life(target.name, source, consumed_days, whole_life, accum)
	return je.name


def _category_life_months(asset):
	"""Total useful life the asset's CATEGORY prescribes, in months."""
	row = frappe.db.get_value(
		"Asset Finance Book",
		{"parent": asset.asset_category, "parenttype": "Asset Category"},
		["total_number_of_depreciations", "frequency_of_depreciation"],
		as_dict=True,
	)
	if not row or not row.total_number_of_depreciations:
		return 0
	return cint(row.total_number_of_depreciations) * (cint(row.frequency_of_depreciation) or 1)


def _note_consumed_life(target_name, source, consumed_days, whole_life, accum):
	"""Say at the top of the new schedule what was already used up
	(client, 24/08: "you can mention them at the start of the new dep")."""
	ads = frappe.db.get_value(
		"Asset Depreciation Schedule",
		{"asset": target_name, "status": "Active", "docstatus": 1},
		"name",
	)
	if not ads:
		return
	note = _(
		"Reclassified from {0}. Total useful life {1} months under this category, "
		"of which {2} days were already depreciated on the previous asset "
		"(from {3}); that charge, {4}, is carried here as Opening Accumulated "
		"Depreciation. This schedule covers the remaining life only."
	).format(
		source.name,
		whole_life,
		cint(consumed_days),
		frappe.format_value(getdate(source.available_for_use_date), {"fieldtype": "Date"})
		if source.available_for_use_date
		else "-",
		frappe.format_value(flt(accum), {"fieldtype": "Currency"}),
	)
	existing = frappe.db.get_value("Asset Depreciation Schedule", ads, "notes") or ""
	frappe.db.set_value(
		"Asset Depreciation Schedule", ads, "notes",
		(note + ("\n\n" + existing if existing else "")),
		update_modified=False,
	)


def add_days_safe(posting_date):
	from frappe.utils import add_days

	return add_days(posting_date, 1)


def reverse_merge(reversal_doc):
	"""Reversal of Capitalized Maintenance: mirror both legs, pair FTs,
	flag Merge Log rows Reversed. Sources stay cancelled (manual
	re-creation per design)."""
	from asset_enterprise import tcc

	source_cap = frappe.get_doc("Asset Capitalization", reversal_doc.reversal_of_capitalization)
	posting_date = getdate(reversal_doc.posting_date or nowdate())

	# Mirror EVERY JE the capitalization posted — a Capitalized
	# Maintenance may carry a merge JE and/or a service-capitalization
	# JE (§12.3), and both must be reversed.
	orig_jes = frappe.get_all(
		"Journal Entry",
		filters={"user_remark": ("like", f"%{source_cap.name}%"), "docstatus": 1},
		pluck="name",
		order_by="creation",
	)
	if not orig_jes:
		frappe.throw(_("Journal Entry for capitalization {0} not found.").format(source_cap.name))

	mirror = None
	for je_name in orig_jes:
		orig_je = frappe.get_doc("Journal Entry", je_name)
		m = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": orig_je.voucher_type,
				"company": orig_je.company,
				"posting_date": posting_date,
				"user_remark": _("Reversal of Capitalized Maintenance {0} via {1} (mirrors {2})").format(
					source_cap.name, reversal_doc.name, je_name
				),
				"accounts": [
					{
						"account": a.account,
						"debit_in_account_currency": a.credit_in_account_currency,
						"credit_in_account_currency": a.debit_in_account_currency,
						"cost_center": a.cost_center,
						"reference_type": a.reference_type,
						"reference_name": a.reference_name,
					}
					for a in orig_je.accounts
				],
			}
		)
		m.flags.ignore_permissions = True
		m.flags.ignore_links = True
		m.submit()
		mirror = mirror or m

	# Pair every FT the merge created.
	for ft_name in frappe.get_all(
		"Financial Treatment",
		filters={
			"source_doctype": "Asset Capitalization",
			"source_name": source_cap.name,
			"status": "Posted",
		},
		pluck="name",
	):
		tcc.reverse(ft_name, reversal_doc, posting_date=posting_date, journal_entry=mirror.name)

	# Merge Log rows -> Reversed (never deleted).
	target = frappe.get_doc("Asset", source_cap.target_asset)
	changed = False
	for row in target.get("merge_log") or []:
		if row.merged_via_capitalization == source_cap.name and row.status == "Active":
			row.db_set("status", "Reversed", update_modified=False)
			changed = True
	if changed:
		frappe.db.set_value(
			"Asset Capitalization", source_cap.name,
			"reversed_by_capitalization", reversal_doc.name, update_modified=False,
		)

	_resupersede(target.name, posting_date, reversal_doc)
	return mirror.name
