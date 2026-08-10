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
per merge. The source Asset leaves the active register (docstatus=2
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
from frappe.utils import add_months, flt, getdate, nowdate

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
			except Exception:
				pass  # no elapsed period to prorate

		values = recalculate_asset_values(source.name, save=False)
		hav = flt(values["historical_asset_value"])
		accum = flt(values["accumulated_depreciation_value"])
		nbv = fa_module_round(hav - accum, company)
		rul_months = flt(values["remaining_useful_life_months"])
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
		source.db_set("status", "Capitalized", update_modified=False)
		source.db_set("docstatus", 2, update_modified=False)
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
	from asset_enterprise.depreciation import supersede_and_regenerate

	end_override = None
	if cap_doc.get("fully_depreciated_treatment") == "Add Value and Extend Life":
		end_override = add_months(posting_date, flt(cap_doc.get("extended_life_months") or 0))
	try:
		supersede_and_regenerate(
			target_name,
			as_of_date=posting_date,
			reason=_("Capitalized Maintenance {0}").format(cap_doc.name),
			end_of_life_override=end_override,
		)
	except frappe.ValidationError:
		pass  # composite without an Active schedule — value-only


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
	target = frappe.get_doc("Asset", cap_doc.target_asset)
	posting_date = getdate(cap_doc.posting_date or nowdate())

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
	rul_months = flt(values["remaining_useful_life_months"])
	salvage = flt(
		frappe.db.get_value(
			"Asset Finance Book", {"parent": source.name}, "expected_value_after_useful_life"
		)
		or 0
	)

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

	# Source leaves the register under the old category.
	source.db_set("status", "Capitalized", update_modified=False)
	source.db_set("docstatus", 2, update_modified=False)

	# Target carries gross + opening accum; the reclassification JE is
	# its booking — GAP-001's suspense path is suppressed by the trace.
	target.db_set("reclassified_from", source.name, update_modified=False)
	if target.docstatus == 0:
		target.purchase_amount = gross
		target.net_purchase_amount = gross
		target.opening_accumulated_depreciation = accum
		target.is_existing_asset = 1
		target.reclassified_from = source.name
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

	# Schedule resets under the new category over the remaining life.
	if source.calculate_depreciation and rul_months >= 1 and not target.calculate_depreciation:
		from asset_enterprise.depreciation import enable_depreciation

		try:
			enable_depreciation(
				target.name,
				total_number_of_depreciations=int(round(rul_months)),
				frequency_of_depreciation=1,
				depreciation_start_date=add_days_safe(posting_date),
				expected_value_after_useful_life=salvage,
			)
		except Exception:
			frappe.log_error(
				title=f"Reclassification schedule reset failed: {target.name}",
				message=frappe.get_traceback(),
			)
	return je.name


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

	# Mirror JE: swap the original merge JE's debits and credits.
	orig_je_name = frappe.db.get_value(
		"Journal Entry",
		{"user_remark": ("like", f"%{source_cap.name}%"), "docstatus": 1},
		"name",
	)
	if not orig_je_name:
		frappe.throw(_("Merge Journal Entry for {0} not found.").format(source_cap.name))
	orig_je = frappe.get_doc("Journal Entry", orig_je_name)
	mirror = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": orig_je.company,
			"posting_date": posting_date,
			"user_remark": _("Reversal of Capitalized Maintenance {0} via {1}").format(
				source_cap.name, reversal_doc.name
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
	mirror.flags.ignore_permissions = True
	mirror.flags.ignore_links = True
	mirror.submit()

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
