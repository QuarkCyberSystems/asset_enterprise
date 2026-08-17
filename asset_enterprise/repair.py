"""Data repair utilities (explicit, opt-in — never run automatically).

`repair_missing_opening_entries` posts the GAP-001 opening JE for
submitted opening-balance assets that have none. Needed when assets
were submitted while the enterprise switch was off, before the app
was deployed, or against stale application workers.

    bench --site <site> execute \\
        asset_enterprise.repair.repair_missing_opening_entries \\
        --kwargs "{'dry_run': 1}"
    bench --site <site> execute \\
        asset_enterprise.repair.repair_missing_opening_entries \\
        --kwargs "{'asset': 'ACC-ASS-2026-00033', 'dry_run': 0}"
"""

import frappe
from frappe import _
from frappe.utils import cint, flt


def find_missing_opening_entries(company=None, asset=None):
	"""Submitted assets that SHOULD carry an opening JE but have no GL
	and no Existing-Asset Opening treatment."""
	filters = {"docstatus": 1}
	if company:
		filters["company"] = company
	if asset:
		filters["name"] = asset

	missing = []
	for row in frappe.get_all(
		"Asset", filters=filters, fields=["name", "asset_name", "company", "net_purchase_amount"]
	):
		doc = frappe.get_doc("Asset", row.name)
		if not doc._is_opening_balance_asset() or flt(doc.net_purchase_amount) <= 0:
			continue
		has_ft = frappe.db.exists(
			"Financial Treatment",
			{"asset": row.name, "transaction_type": "Existing-Asset Opening", "status": "Posted"},
		)
		has_gl = frappe.db.sql(
			"select count(*) from `tabGL Entry` where against_voucher = %s and is_cancelled = 0",
			row.name,
		)[0][0]
		if not has_ft and not has_gl:
			missing.append(row)
	return missing


def repair_missing_opening_entries(company=None, asset=None, dry_run=1):
	"""Report (dry_run=1) or post (dry_run=0) the missing opening JEs."""
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		frappe.throw(_("Enterprise Assets is not enabled."))

	missing = find_missing_opening_entries(company=company, asset=asset)
	if not missing:
		print("no assets are missing their opening entry")
		return []

	print(f"{len(missing)} asset(s) missing the GAP-001 opening entry:")
	for row in missing:
		print(f"  {row.name} | {row.asset_name} | gross {row.net_purchase_amount}")

	if cint(dry_run):
		print("dry run — nothing posted. Re-run with dry_run=0 to post.")
		return [r.name for r in missing]

	posted = []
	for row in missing:
		doc = frappe.get_doc("Asset", row.name)
		try:
			doc._post_existing_asset_opening()
			frappe.db.commit()
			ft = frappe.db.get_value(
				"Financial Treatment",
				{"asset": row.name, "transaction_type": "Existing-Asset Opening"},
				["name", "journal_entry"], as_dict=True,
			)
			print(f"  posted {row.name}: JE {ft and ft.journal_entry} (FT {ft and ft.name})")
			posted.append(row.name)
		except Exception as e:
			frappe.db.rollback()
			print(f"  FAILED {row.name}: {str(e)[:140]}")
	return posted


def remove_empty_superseded_schedules(company=None, asset=None, dry_run=1):
	"""Delete Superseded depreciation schedules that never posted a
	single entry — working copies left behind before the engine started
	dropping them (core builds one at Asset submit that the §4.3 rebuild
	replaces milliseconds later). Anything with a posted or reversed row
	is history and is never touched.

	    bench --site <site> execute \\
	        asset_enterprise.repair.remove_empty_superseded_schedules \\
	        --kwargs "{'dry_run': 0}"
	"""
	filters = {"status": "Superseded", "docstatus": 1}
	if company:
		filters["company"] = company
	if asset:
		filters["asset"] = asset

	targets = []
	for row in frappe.get_all(
		"Asset Depreciation Schedule", filters=filters, fields=["name", "asset"]
	):
		booked = frappe.db.sql(
			"""select count(*) from `tabDepreciation Schedule`
			   where parent = %s and (ifnull(journal_entry, '') != ''
			                          or ifnull(reversal_journal_entry, '') != '')""",
			row.name,
		)[0][0]
		if not booked:
			targets.append(row)

	if not targets:
		print("no empty superseded schedules")
		return []

	print(f"{len(targets)} superseded schedule(s) that never booked anything:")
	for row in targets:
		print(f"  {row.name}  ({row.asset})")
	if cint(dry_run):
		print("dry run — nothing deleted. Re-run with dry_run=0 to remove.")
		return [r.name for r in targets]

	removed = []
	for row in targets:
		try:
			# Re-point anything that chains through this schedule.
			for child in frappe.get_all(
				"Asset Depreciation Schedule", filters={"supersedes": row.name}, pluck="name"
			):
				frappe.db.set_value(
					"Asset Depreciation Schedule",
					child,
					"supersedes",
					frappe.db.get_value("Asset Depreciation Schedule", row.name, "supersedes"),
					update_modified=False,
				)
			from asset_enterprise.depreciation import delete_unposted_schedule

			delete_unposted_schedule(row.name)
			frappe.db.commit()
			print(f"  removed {row.name}")
			removed.append(row.name)
		except Exception as e:
			frappe.db.rollback()
			print(f"  FAILED {row.name}: {str(e)[:140]}")
	return removed


def find_unrouted_invoice_differences(company=None, purchase_invoice=None):
	"""Submitted Purchase Invoices whose asset rows carry a PI−PR delta
	that never routed, because the invoice named no assets under Asset
	Allocation (the pre-2026-08-16 behaviour silently skipped GAP-012)."""
	from asset_enterprise.invoice_diff import _uncovered_assets_for_pr_row

	conditions, values = ["pi.docstatus = 1"], {}
	if company:
		conditions.append("pi.company = %(company)s")
		values["company"] = company
	if purchase_invoice:
		conditions.append("pi.name = %(pi)s")
		values["pi"] = purchase_invoice

	stuck = []
	rows = frappe.db.sql(
		f"""
		select pi.name pi, pi.posting_date, pi.company, pii.name item, pii.idx,
		       pii.pr_detail, pii.net_rate pi_rate, pii.qty
		from `tabPurchase Invoice` pi
		join `tabPurchase Invoice Item` pii on pii.parent = pi.name
		where {" and ".join(conditions)}
		  and pii.is_fixed_asset = 1 and ifnull(pii.pr_detail, '') != ''
		order by pi.posting_date
		""",
		values,
		as_dict=True,
	)
	for row in rows:
		if frappe.db.count("PI Asset Allocation", {"parent": row.pi}):
			continue  # allocation exists — GAP-012 already ran
		pr_rate = frappe.db.get_value("Purchase Receipt Item", row.pr_detail, "net_rate")
		delta = flt(row.pi_rate) - flt(pr_rate or 0)
		if not delta:
			continue
		candidates = _uncovered_assets_for_pr_row(row.pr_detail, exclude_pi=row.pi)
		if not candidates:
			continue
		row.update({"delta": delta, "assets": candidates})
		stuck.append(row)
	return stuck


def repair_invoice_differences(company=None, purchase_invoice=None, dry_run=1):
	"""Replay GAP-012 for invoices that submitted before the allocation
	auto-resolve fix. The original invoice is never touched — the delta
	routes through the same transfer JE + Invoice-Adjustment AVA a fresh
	submit would produce.

	    bench --site <site> execute \\
	        asset_enterprise.repair.repair_invoice_differences \\
	        --kwargs "{'purchase_invoice': 'ACC-PINV-2026-00107', 'dry_run': 0}"
	"""
	from asset_enterprise.depreciation import enterprise_enabled
	from asset_enterprise.invoice_diff import pi_on_submit

	if not enterprise_enabled():
		frappe.throw(_("Enterprise Assets is not enabled."))

	stuck = find_unrouted_invoice_differences(company=company, purchase_invoice=purchase_invoice)
	if not stuck:
		print("no invoice differences are stuck")
		return []

	print(f"{len(stuck)} invoice row(s) with an unrouted difference:")
	for row in stuck:
		print(
			f"  {row.pi} row {row.idx} | delta {row.delta} | "
			f"asset(s) {', '.join(row.assets[: max(1, int(row.qty or 1))])}"
		)
	if cint(dry_run):
		print("dry run — nothing posted. Re-run with dry_run=0 to route.")
		return [r.pi for r in stuck]

	by_invoice = {}
	for row in stuck:
		by_invoice.setdefault(row.pi, []).extend(row.assets[: max(1, int(row.qty or 1))])

	done = []
	for pi_name, assets in by_invoice.items():
		try:
			for idx, asset_name in enumerate(assets, start=1):
				frappe.get_doc(
					{
						"doctype": "PI Asset Allocation",
						"parent": pi_name,
						"parenttype": "Purchase Invoice",
						"parentfield": "pi_asset_allocation",
						"idx": idx,
						"asset": asset_name,
						"purchase_receipt": frappe.db.get_value(
							"Asset", asset_name, "purchase_receipt"
						),
					}
				).db_insert()
			doc = frappe.get_doc("Purchase Invoice", pi_name)
			pi_on_submit(doc)
			frappe.db.commit()
			print(f"  routed {pi_name}: assets {', '.join(assets)}")
			done.append(pi_name)
		except Exception as e:
			frappe.db.rollback()
			print(f"  FAILED {pi_name}: {str(e)[:160]}")
	return done


def rebuild_schedules_under_daycount_rule(company=None, asset=None, dry_run=1):
	"""Re-apply the §4.3 day-count rule to schedules that core built
	(non-uniform daily rate across a leap year). Posted rows are kept.

	    bench --site <site> execute \
	        asset_enterprise.repair.rebuild_schedules_under_daycount_rule \
	        --kwargs "{'dry_run': 0}"
	"""
	from asset_enterprise.depreciation import (
		apply_daycount_rule,
		enterprise_enabled,
		is_rule_built_schedule,
	)

	if not enterprise_enabled():
		frappe.throw(_("Enterprise Assets is not enabled."))

	filters = {"docstatus": 1, "calculate_depreciation": 1}
	if company:
		filters["company"] = company
	if asset:
		filters["name"] = asset

	targets = [
		row.name
		for row in frappe.get_all("Asset", filters=filters, fields=["name"])
		if frappe.db.exists(
			"Asset Depreciation Schedule",
			{"asset": row.name, "status": "Active", "docstatus": 1},
		)
		and not is_rule_built_schedule(row.name)
	]
	if not targets:
		print("all Active schedules already follow the §4.3 rule")
		return []

	print(f"{len(targets)} schedule(s) built under core's day-count:")
	for name in targets:
		print(f"  {name}")
	if cint(dry_run):
		print("dry run — nothing changed. Re-run with dry_run=0 to rebuild.")
		return targets

	done = []
	for name in targets:
		try:
			ads = apply_daycount_rule(name, reason=_("§4.3 day-count rule — schedule rebuild"))
			frappe.db.commit()
			print(f"  rebuilt {name} -> {ads and ads.name}")
			done.append(name)
		except Exception as e:
			frappe.db.rollback()
			print(f"  FAILED {name}: {str(e)[:140]}")
	return done


def refresh_stored_asset_values(company=None, dry_run=1):
	"""Write the derived HAV / Accumulated / NBV onto assets that never
	had them stored. The values are snapshots taken when a treatment
	posts, so an asset created before that step — or one whose only
	events predate the app — shows zeros on the form and in the tree.

	    bench --site <site> execute \\
	        asset_enterprise.repair.refresh_stored_asset_values \\
	        --kwargs "{'dry_run': 0}"
	"""
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import enterprise_enabled

	if not enterprise_enabled():
		frappe.throw(_("Enterprise Assets is not enabled."))

	filters = {"docstatus": ("<", 2)}
	if company:
		filters["company"] = company

	stale = []
	for row in frappe.get_all("Asset", filters=filters, fields=["name", "historical_asset_value"]):
		derived = recalculate_asset_values(row.name, save=False)
		if flt(row.historical_asset_value) != flt(derived["historical_asset_value"]):
			stale.append((row.name, flt(row.historical_asset_value), derived))

	if not stale:
		print("every asset already carries its derived values")
		return []

	print(f"{len(stale)} asset(s) with stale or missing stored values:")
	for name, stored, derived in stale[:20]:
		print(f"  {name}: stored {stored:,.2f} -> {derived['historical_asset_value']:,.2f}")
	if len(stale) > 20:
		print(f"  ... and {len(stale) - 20} more")
	if cint(dry_run):
		print("dry run — nothing written. Re-run with dry_run=0 to refresh.")
		return [s[0] for s in stale]

	for name, _stored, _derived in stale:
		recalculate_asset_values(name, save=True)
	frappe.db.commit()
	print(f"refreshed {len(stale)} asset(s)")
	return [s[0] for s in stale]
