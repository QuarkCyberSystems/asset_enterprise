"""Disposal engine — GA-0005-01 v2.14 GAP-018 / GAP-019 / VR-041.

Full scrap (replaces the whitelisted core scrap_asset when the master
switch is on) and partial scrap (new endpoint), both routing the loss
leg through the Scrapping Type account chain (§3.5, per 2026-07-14
meeting):

    Scrapping Type Account (per reason, per company)
      -> Asset Category `disposal_account_override`
      -> Company disposal account

Full scrap GL (per xlsx ASSET_MVT Scrapping):
    DR Accumulated Depreciation   [Accum at scrap, post-proration]
    DR <Scrapping Type account>   [NBV loss]
       CR Fixed Asset Cost        [HAV / gross]

Partial scrap GL (per xlsx Partial_Disposal_Simulation, labels per
2026-07-14 meeting):
    DR Accumulated Depreciation   [Accumulated Reverse = ratio x Accum]
    DR <Scrapping Type account>   [Loss = Scrap Value − Accumulated Reverse]
       CR Fixed Asset Cost        [Scrap Value]

VR-041: an asset already in a disposal state cannot be disposed again
(core validate_asset_for_scrap covers full scrap; we mirror it here).
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from asset_enterprise.accounts import get_disposal_account, get_disposal_cost_center
from asset_enterprise.rounding import fa_module_round

DISPOSED_STATUSES = ("Cancelled", "Sold", "Scrapped", "Capitalized")


def assert_fully_invoiced(asset):
	"""GAP-010 / VR-011: with the Asset Settings flag on, an asset created
	from a PR cannot be disposed of (or merged away) until a submitted
	Purchase Invoice allocation covers it. Callers gate on the master
	switch; `asset` is a doc or anything with .get()."""
	if not frappe.db.get_single_value(
		"Asset Settings", "prevent_disposal_before_full_invoicing", cache=False
	):
		return
	if not asset.get("purchase_receipt") or asset.get("purchase_invoice"):
		return
	covered = frappe.db.sql(
		"""
		select paa.parent from `tabPI Asset Allocation` paa
		join `tabPurchase Invoice` pi on pi.name = paa.parent
		where paa.asset = %s and pi.docstatus = 1
		limit 1
		""",
		asset.name,
	)
	if not covered:
		frappe.throw(
			_(
				"Asset {0} is received on {1} but not yet fully invoiced — disposal and "
				"capitalization are blocked until a submitted Purchase Invoice covers it "
				"(VR-011, per Asset Settings)."
			).format(asset.name, asset.get("purchase_receipt"))
		)


@frappe.whitelist()
def scrap_asset(asset_name, scrap_date=None, scrapping_type=None, cost_center=None):
	"""Whitelisted replacement for core scrap_asset (build plan §2.2)."""
	from asset_enterprise.depreciation import enterprise_enabled

	if frappe.db.get_value("Asset", asset_name, "is_group_node"):
		frappe.throw(
			_(
				"{0} is a Grouping Asset and holds no value — dispose of the physical "
				"assets grouped under it instead (GAP-036)."
			).format(asset_name)
		)

	if not enterprise_enabled():
		from erpnext.assets.doctype.asset.depreciation import scrap_asset as core_scrap

		return core_scrap(asset_name, scrap_date=scrap_date)

	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values
	from erpnext.assets.doctype.asset.depreciation import (
		depreciate_asset,
		validate_asset_for_scrap,
	)

	asset = frappe.get_doc("Asset", asset_name)
	scrap_date = getdate(scrap_date or today())
	validate_asset_for_scrap(asset, scrap_date)  # incl. VR-041 status gate
	assert_fully_invoiced(asset)  # GAP-010 / VR-011

	# Mid-period proration up to the scrap date (existing engine). A
	# proration failure FAILS the scrap — the blanket except here
	# swallowed a broken posting for weeks and the usage days silently
	# became disposal loss (client, 19/08, ACC-ASS-2026-00139).
	if asset.calculate_depreciation:
		depreciate_asset(asset, scrap_date, _("Scrapped on {0}").format(scrap_date))
		asset.reload()

	values = recalculate_asset_values(asset.name, save=False)
	hav = flt(values["historical_asset_value"])
	accum = flt(values["accumulated_depreciation_value"])
	nbv = fa_module_round(hav - accum, asset.company)

	je = _post_disposal_je(
		asset, scrap_date, scrapping_type, accum, nbv, hav, cost_center=cost_center
	)

	asset.db_set("disposal_date", scrap_date)
	asset.db_set("journal_entry_for_scrap", je)
	asset.db_set("status", "Scrapped")

	tcc.apply(
		source_doc=("Asset", asset.name),
		category="Disposal",
		transaction_type=f"Scrapping — {scrapping_type}" if scrapping_type else "Scrapping",
		asset=asset.name,
		posting_date=scrap_date,
		amount=nbv,
		hav_delta=-hav,
		accum_delta=-accum,
		journal_entry=je,
	)
	_freeze_schedule(asset.name, scrap_date, _("Asset scrapped via {0}").format(je))
	_record_scrap_transaction(
		asset.name, "Full Scrap", scrapping_type, scrap_date, je, cost_center=cost_center
	)
	return je


@frappe.whitelist()
def partial_scrap_asset(
	asset_name,
	scrap_value=None,
	percentage=None,
	scrapping_type=None,
	scrap_date=None,
	composite_component=None,
	cost_center=None,
):
	"""GAP-018: partial scrap by value or percentage. Asset stays active.

	composite_component (v2.16 CH-09): scrap a specific Active merged
	component of a composite — its NBV-at-merge snapshot defaults the
	scrap value and the Merge Log row is marked Scrapped."""
	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		frappe.throw(_("Partial scrap requires Enterprise Assets to be enabled."))

	asset = frappe.get_doc("Asset", asset_name)
	if asset.get("is_group_node"):
		frappe.throw(
			_(
				"{0} is a Grouping Asset and holds no value — dispose of the physical "
				"assets grouped under it instead (GAP-036)."
			).format(asset_name)
		)
	if asset.docstatus != 1:
		frappe.throw(_("Asset {0} must be submitted.").format(asset_name))
	if asset.status in DISPOSED_STATUSES:
		frappe.throw(
			_("Asset {0} is {1} — no further disposal is permitted (VR-041).").format(
				asset_name, asset.status
			)
		)
	assert_fully_invoiced(asset)  # GAP-010 / VR-011

	if composite_component:
		# Child tables must be queried with their parent doctype named —
		# without it the row is simply not found, and scrapping a merged
		# component failed with "not an Active merged component" even
		# though the Merge Log listed it (client sheet item 8).
		component_row = frappe.db.sql(
			"""select name, net_book_value_at_merge
			   from `tabComposite Merge Log Entry`
			   where parent = %s and parenttype = 'Asset'
			     and merged_source_asset = %s and status = 'Active'
			   limit 1""",
			(asset_name, composite_component),
			as_dict=True,
		)
		component_row = component_row[0] if component_row else None
		if not component_row:
			frappe.throw(
				_(
					"{0} is not an Active merged component of {1} — pick a component "
					"from the composite's Merge Log."
				).format(composite_component, asset_name)
			)
		if not flt(scrap_value) and not flt(percentage):
			scrap_value = flt(component_row.net_book_value_at_merge)

	scrap_date = getdate(scrap_date or today())
	values = recalculate_asset_values(asset.name, save=False)
	hav = flt(values["historical_asset_value"])
	accum = flt(values["accumulated_depreciation_value"])

	scrap_value = flt(scrap_value) or fa_module_round(hav * flt(percentage) / 100, asset.company)
	if not (0 < scrap_value < hav):
		frappe.throw(_("Partial scrap value must be greater than 0 and less than HAV (VR-023)."))

	ratio = scrap_value / hav
	accumulated_reverse = fa_module_round(ratio * accum, asset.company)
	loss = fa_module_round(scrap_value - accumulated_reverse, asset.company)

	je = _post_disposal_je(
		asset, scrap_date, scrapping_type, accumulated_reverse, loss, scrap_value,
		cost_center=cost_center,
	)

	tcc.apply(
		source_doc=("Asset", asset.name),
		category="Disposal",
		transaction_type=f"Partial Disposal — {scrapping_type}" if scrapping_type else "Partial Disposal",
		asset=asset.name,
		posting_date=scrap_date,
		amount=scrap_value,
		hav_delta=-scrap_value,
		accum_delta=-accumulated_reverse,
		journal_entry=je,
	)

	# Prospective schedule over the reduced base; asset remains active.
	# Resume from the last POSTED row with the scrap date as the
	# rate-change boundary — regenerating from the scrap date left the
	# days before it with no row of their own (same short-row defect as
	# the UL adjustment, caught in the 19/08 caller audit).
	from asset_enterprise.depreciation import (
		last_posted_schedule_date,
		supersede_and_regenerate,
	)

	last_posted = last_posted_schedule_date(asset.name)
	try:
		supersede_and_regenerate(
			asset.name,
			as_of_date=getdate(last_posted) if last_posted else scrap_date,
			rate_change_date=getdate(scrap_date) if last_posted else None,
			reason=_("Partial scrap via {0}").format(je),
		)
	except frappe.ValidationError:
		pass
	# Order matters: the Scrap Transaction is the RECORD of this scrap and
	# validates that the component is still an Active Merge Log row. Flip
	# the row to Scrapped only once that record exists, or the document
	# refuses the very scrap it is documenting.
	_record_scrap_transaction(
		asset.name, "Partial Scrap", scrapping_type, scrap_date, je,
		scrap_value=scrap_value, percentage=percentage,
		composite_component=composite_component, cost_center=cost_center,
	)
	if composite_component:
		# The component row records that this scrap consumed it.
		frappe.db.set_value(
			"Composite Merge Log Entry", component_row.name, "status", "Scrapped",
			update_modified=False,
		)
	return je


def _record_scrap_transaction(
	asset_name,
	scrap_type,
	scrapping_type,
	scrap_date,
	je,
	scrap_value=None,
	percentage=None,
	composite_component=None,
	cost_center=None,
):
	"""v2.16 CH-09: every scrap is a first-class Scrap Transaction. When
	the posting was triggered from a Scrap Transaction itself, the doc
	already exists; otherwise (core Scrap button / API) record one."""
	if frappe.flags.get("in_scrap_transaction"):
		return
	doc = frappe.get_doc(
		{
			"doctype": "Scrap Transaction",
			"asset": asset_name,
			"company": frappe.db.get_value("Asset", asset_name, "company"),
			"transaction_date": scrap_date,
			"scrap_type": scrap_type,
			"scrapping_type": scrapping_type or frappe.db.get_value("Scrapping Type", {}, "name"),
			"mode": "By Value" if scrap_value else "By Percentage",
			"scrap_value": flt(scrap_value) if scrap_value else None,
			"percentage": flt(percentage) if percentage else None,
			"composite_component": composite_component,
			"cost_center": cost_center,
			"journal_entry": je,
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.auto_recorded = True  # posting already done — record only
	doc.insert()
	doc.submit()


def _post_disposal_je(
	asset, posting_date, scrapping_type, accum_debit, loss_debit, fa_credit, cost_center=None
):
	aca = frappe.db.get_value(
		"Asset Category Account",
		{"parent": asset.asset_category, "company_name": asset.company},
		["fixed_asset_account", "accumulated_depreciation_account"],
		as_dict=True,
	)
	loss_account = get_disposal_account(
		asset.company, scrapping_type=scrapping_type, asset_category=asset.asset_category
	)
	# An explicit cost centre wins: the Scrapping Type may allow the
	# transaction to redirect the charge (client, 20/08).
	cost_center = (
		cost_center
		or get_disposal_cost_center(asset.company, scrapping_type)
		or asset.get("cost_center")
	)

	accounts = []
	if flt(accum_debit):
		accounts.append(
			{
				"account": aca.accumulated_depreciation_account,
				"debit_in_account_currency": flt(accum_debit),
				"cost_center": cost_center,
				"reference_type": "Asset",
				"reference_name": asset.name,
			}
		)
	if flt(loss_debit):
		accounts.append(
			{
				"account": loss_account,
				"debit_in_account_currency": flt(loss_debit),
				"cost_center": cost_center,
				"reference_type": "Asset",
				"reference_name": asset.name,
			}
		)
	accounts.append(
		{
			"account": aca.fixed_asset_account,
			"credit_in_account_currency": flt(fa_credit),
			"cost_center": cost_center,
			"reference_type": "Asset",
			"reference_name": asset.name,
		}
	)

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": asset.company,
			"posting_date": posting_date,
			"user_remark": _("Disposal ({0}) of Asset {1}").format(
				scrapping_type or "Scrap", asset.name
			),
			"accounts": accounts,
		}
	)
	je.flags.ignore_permissions = True
	je.submit()
	return je.name


def _freeze_schedule(asset_name, as_of_date, reason):
	"""After full disposal NBV is zero — supersession leaves only the
	posted rows (no future rows regenerate from a zero base).

	Skipped when the Active generation is ALREADY terminated at the
	disposal date (the reschedule wrapper truncated it): a second
	supersession there was pure churn — and it silently DROPPED any
	still-unposted proration row (client, 19/08)."""
	from asset_enterprise.depreciation import supersede_and_regenerate

	last_row = frappe.db.sql(
		"""select max(ds.schedule_date) from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1""",
		asset_name,
	)[0][0]
	if last_row and getdate(last_row) <= getdate(as_of_date):
		return  # already frozen by the disposal truncation
	try:
		supersede_and_regenerate(asset_name, as_of_date=as_of_date, reason=reason)
	except frappe.ValidationError:
		pass


def get_gl_entries_on_asset_disposal_wrapper(core_fn):
	"""Patch #3 (build plan §2.3): swap the loss account in core disposal
	GL (e.g. sale via Sales Invoice) with the §3.5 chain result."""

	def wrapped(asset, *args, **kwargs):
		from asset_enterprise.depreciation import enterprise_enabled

		gl = core_fn(asset, *args, **kwargs)
		if not enterprise_enabled():
			return gl
		try:
			override = get_disposal_account(
				asset.company, scrapping_type=None, asset_category=asset.asset_category
			)
			from erpnext.assets.doctype.asset.depreciation import (
				get_disposal_account_and_cost_center,
			)

			core_account = get_disposal_account_and_cost_center(asset.company)[0]
			if override and override != core_account:
				for row in gl:
					if row.get("account") == core_account:
						row["account"] = override
		except Exception:
			pass
		return gl

	wrapped._asset_enterprise_wrapper = True
	return wrapped
