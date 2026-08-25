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
from frappe.utils import add_days, cint, date_diff, flt, getdate


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


# --------------------------------------------------------------------------
# Fallout of the reschedule monkeypatch never reaching core (§4.8 / GAP-031)
# --------------------------------------------------------------------------


def _cancelled_schedules(company=None, asset=None):
	conditions, params = "", []
	if company:
		conditions += " and ads.company = %s"
		params.append(company)
	if asset:
		conditions += " and ads.asset = %s"
		params.append(asset)
	return frappe.db.sql(
		f"""
		select ads.name, ads.asset
		from `tabAsset Depreciation Schedule` ads
		where (ads.docstatus = 2 or ads.status = 'Cancelled'){conditions}
		order by ads.asset, ads.creation
		""",
		params,
		as_dict=True,
	)


def resupersede_cancelled_schedules(company=None, asset=None, dry_run=1):
	"""Cancelled depreciation schedules -> Superseded (GAP-031).

	Until the reschedule wrapper was rebound onto every module that had
	imported it, core's reschedule_depreciation ran on disposal and ended
	in current_schedule.cancel(). The supersede-never-cancel rule says
	that generation stays SUBMITTED with status "Superseded" so its
	posted rows and JE links remain readable history.

	Asset values are unaffected: _posted_depreciation_total sums the
	ACTIVE generation only, and Superseded is excluded exactly as
	Cancelled was. Any posted JE that exists ONLY on the cancelled
	generation is reported and left alone — that is an accumulated-
	depreciation shortfall, a separate question from this flip.

	    bench --site <site> execute \\
	        asset_enterprise.repair.resupersede_cancelled_schedules \\
	        --kwargs "{'dry_run': 0}"
	"""
	targets = _cancelled_schedules(company, asset)
	if not targets:
		print("no cancelled depreciation schedules")
		return []

	print(f"{len(targets)} cancelled schedule(s) to re-supersede:")
	orphans = []
	for row in targets:
		missing = frappe.db.sql(
			"""
			select ds.journal_entry
			from `tabDepreciation Schedule` ds
			where ds.parent = %s and ifnull(ds.journal_entry, '') != ''
			  and ds.journal_entry not in (
				select ifnull(a.journal_entry, '')
				from `tabDepreciation Schedule` a
				join `tabAsset Depreciation Schedule` s on a.parent = s.name
				where s.asset = %s and s.status = 'Active' and s.docstatus = 1
			  )
			""",
			(row.name, row.asset),
			pluck=True,
		)
		flag = f"  !! {len(missing)} posted JE(s) not on the Active generation: {missing}" if missing else ""
		print(f"  {row.name}  ({row.asset}){flag}")
		if missing:
			orphans.append((row.asset, row.name, missing))

	if orphans:
		print(
			f"\n  {len(orphans)} schedule(s) carry posted depreciation the Active generation "
			f"does not — their assets under-state accumulated depreciation. Reported only; "
			f"the flip below does not change any value."
		)

	if cint(dry_run):
		print("dry run — nothing changed. Re-run with dry_run=0 to apply.")
		return [r.name for r in targets]

	changed = []
	for row in targets:
		frappe.db.set_value(
			"Asset Depreciation Schedule",
			row.name,
			{"docstatus": 1, "status": "Superseded"},
			update_modified=False,
		)
		changed.append(row.name)
	frappe.db.commit()
	print(f"re-superseded {len(changed)} schedule(s)")
	return changed


def find_collapsed_horizons(company=None, asset=None):
	"""Active schedules carrying less future depreciation than the asset
	still has to depreciate — value with no row to charge it through.

	Each regeneration took its horizon from the PREVIOUS generation's
	last row, so once core truncated a generation at a disposal date
	every later generation inherited the short horizon. The test is the
	shortfall itself, not the horizon: a schedule that merely ends a few
	weeks early still spreads the whole NBV across the rows it has, and
	strands nothing.
	"""
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import schedule_horizon_from_life

	conditions, params = "", []
	if company:
		conditions += " and a.company = %s"
		params.append(company)
	if asset:
		conditions += " and a.name = %s"
		params.append(asset)

	rows = frappe.db.sql(
		f"""
		select ads.asset, ads.name sched, max(ds.schedule_date) last_row,
		       sum(case when ifnull(ds.journal_entry,'') = '' then 1 else 0 end) unposted,
		       coalesce(sum(case when ifnull(ds.journal_entry,'') = ''
		                         then ds.depreciation_amount else 0 end), 0) future_amount
		from `tabAsset Depreciation Schedule` ads
		join `tabAsset` a on a.name = ads.asset
		join `tabDepreciation Schedule` ds on ds.parent = ads.name
		where ads.status = 'Active' and ads.docstatus = 1 and a.docstatus = 1
		  and a.calculate_depreciation = 1
		  and a.status not in ('Scrapped', 'Sold', 'Disposed', 'Capitalized', 'Cancelled')
		  {conditions}
		group by ads.name
		order by ads.asset
		""",
		params,
		as_dict=True,
	)

	found = []
	for row in rows:
		nbv = flt(recalculate_asset_values(row.asset, save=False)["net_book_value"])
		salvage = flt(
			frappe.db.get_value(
				"Asset Finance Book", {"parent": row.asset}, "expected_value_after_useful_life"
			)
			or 0
		)
		# What still has to depreciate, minus what the schedule can carry.
		stranded = flt(nbv - salvage - flt(row.future_amount), 2)
		if stranded <= 0.005:
			continue
		horizon = schedule_horizon_from_life(row.asset)
		found.append(
			{
				"asset": row.asset,
				"schedule": row.sched,
				"last_row": getdate(row.last_row),
				"horizon": getdate(horizon) if horizon else None,
				"unposted": cint(row.unposted),
				"stranded": stranded,
				"repairable": bool(horizon and getdate(horizon) > getdate(row.last_row)),
			}
		)
	return found


def restore_collapsed_horizons(company=None, asset=None, dry_run=1):
	"""Regenerate Active schedules back out to the finance-book end of
	life, so stranded NBV has rows to depreciate through. Posted rows are
	copied verbatim as always; only the future is rebuilt.

	    bench --site <site> execute \\
	        asset_enterprise.repair.restore_collapsed_horizons \\
	        --kwargs "{'asset': 'ACC-ASS-2026-00102', 'dry_run': 0}"
	"""
	from asset_enterprise.depreciation import last_posted_schedule_date, supersede_and_regenerate

	found = find_collapsed_horizons(company, asset)
	if not found:
		print("no collapsed schedule horizons")
		return []

	print(f"{len(found)} asset(s) with depreciation no schedule row can carry:")
	for row in found:
		note = "" if row["repairable"] else "  !! life already ended — needs a finance-book decision"
		print(
			f"  {row['asset']}  {row['schedule']} ends {row['last_row']} ({row['unposted']} "
			f"unposted row(s)), life runs to {row['horizon']} — {row['stranded']:,.2f} "
			f"cannot depreciate{note}"
		)
	if cint(dry_run):
		print("dry run — nothing changed. Re-run with dry_run=0 to apply.")
		return [r["asset"] for r in found]

	repaired = []
	for row in found:
		if not row["repairable"]:
			print(f"  SKIPPED {row['asset']}: end of life {row['horizon']} is not past {row['last_row']}")
			continue
		try:
			as_of = last_posted_schedule_date(row["asset"]) or row["last_row"]
			new = supersede_and_regenerate(
				row["asset"],
				as_of_date=as_of,
				end_of_life_override=row["horizon"],
				reason=_("Horizon restored to end of life ({0})").format(row["horizon"]),
			)
			frappe.db.commit()
			print(f"  {row['asset']}: {new.name} now runs to {row['horizon']}")
			repaired.append(row["asset"])
		except Exception as e:
			frappe.db.rollback()
			print(f"  FAILED {row['asset']}: {str(e)[:160]}")
	return repaired


def _reference_daily_rate(rows, candidates):
	"""What a day costs on this schedule, from rows long enough to say:
	any row that already carries a rate, plus reconstructions spanning a
	full period. Returns the median, or None when nothing qualifies."""
	rates = [flt(r.daily_rate) for r in rows if flt(r.daily_rate) > 0]
	rates += [rate for _n, _d, days, rate in candidates if days >= 25]
	if not rates:
		return None
	rates.sort()
	mid = len(rates) // 2
	return rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2


def backfill_row_daycount_metadata(company=None, asset=None, dry_run=1):
	"""Fill days_in_period / daily_rate on rows that core created.

	Core writes neither field, so rows it built — every schedule the
	cancel-and-recreate path produced — display as "0 days @ 0.000000"
	once they are carried forward into our generations. The amounts are
	posted and are NOT touched: days come from the gap to the previous
	row (or the in-service date for the first), and the rate is derived
	from the amount actually booked, so rate x days reproduces it.

	    bench --site <site> execute \\
	        asset_enterprise.repair.backfill_row_daycount_metadata \\
	        --kwargs "{'dry_run': 0}"
	"""
	conditions, params = "", []
	if company:
		conditions += " and ads.company = %s"
		params.append(company)
	if asset:
		conditions += " and ads.asset = %s"
		params.append(asset)

	schedules = frappe.db.sql(
		f"""
		select distinct ads.name, ads.asset
		from `tabAsset Depreciation Schedule` ads
		join `tabDepreciation Schedule` ds on ds.parent = ads.name
		where ads.docstatus = 1 and ads.status = 'Active'
		  and ifnull(ds.days_in_period, 0) = 0 and ifnull(ds.daily_rate, 0) = 0
		  {conditions}
		order by ads.asset
		""",
		params,
		as_dict=True,
	)
	if not schedules:
		print("no rows missing day-count metadata")
		return []

	updates, incoherent = [], []
	for sched in schedules:
		basis = frappe.db.get_value("Asset", sched.asset, "available_for_use_date")
		rows = frappe.get_all(
			"Depreciation Schedule",
			filters={"parent": sched.name},
			fields=["name", "schedule_date", "depreciation_amount", "days_in_period", "daily_rate"],
			order_by="schedule_date, idx",
		)
		# Candidates first, then judge each against what the schedule's
		# own full periods say a day costs.
		candidates = []
		prev = add_days(getdate(basis), -1) if basis else None
		for row in rows:
			this_date = getdate(row.schedule_date)
			if flt(row.days_in_period) or flt(row.daily_rate):
				prev = this_date
				continue
			days = date_diff(this_date, prev) if prev else 0
			if days > 0:
				candidates.append((row.name, this_date, days, flt(row.depreciation_amount) / days))
			prev = this_date

		reference = _reference_daily_rate(rows, candidates)
		for row_name, this_date, days, rate in candidates:
			# A stub period whose amount is really a whole period's charge
			# would be recorded as "1 day @ a month's money". Core wrote
			# these rows monthly; where the arithmetic does not agree with
			# the rest of the schedule, say so instead of inventing a rate.
			if reference:
				coherent = reference / 3 <= rate <= reference * 3
			else:
				coherent = days >= 5
			if coherent:
				updates.append((sched.asset, sched.name, row_name, this_date, days, rate))
			else:
				incoherent.append((sched.asset, sched.name, this_date, days, rate, reference))

	if incoherent:
		print(f"{len(incoherent)} row(s) NOT reconstructable — reported, not written:")
		for asset_name, sched_name, date, days, rate, ref in incoherent:
			ref_txt = f"schedule averages {ref:.6f}/day" if ref else "no full period to compare against"
			print(f"  {asset_name} {sched_name} {date}: would be {days} day(s) @ {rate:.6f} — {ref_txt}")

	if not updates:
		print("no rows could be reconstructed")
		return []

	print(f"{len(updates)} row(s) across {len(schedules)} schedule(s) to backfill:")
	shown = updates if len(updates) <= 120 else updates[:120]
	for asset_name, sched_name, _row, date, days, rate in shown:
		print(f"  {asset_name} {sched_name} {date}: {days} days @ {rate:.6f}")
	if len(shown) < len(updates):
		print(f"  ... and {len(updates) - len(shown)} more")
	if cint(dry_run):
		print("dry run — nothing changed. Re-run with dry_run=0 to apply.")
		return [u[2] for u in updates]

	for _asset, _sched, row_name, _date, days, rate in updates:
		frappe.db.set_value(
			"Depreciation Schedule",
			row_name,
			{"days_in_period": days, "daily_rate": rate},
			update_modified=False,
		)
	frappe.db.commit()
	print(f"backfilled {len(updates)} row(s)")
	return [u[2] for u in updates]


def find_unlinked_repair_treatments(company=None, asset=None):
	"""Posted Capitalized Repair Addition FTs whose GL voucher is not
	linked.

	The repair posts its GL under its own voucher; a treatment that does
	not carry voucher_type/voucher_no is invisible to the values fold's
	counted-voucher set, so the manual-GL sweep re-adds the repair's GL
	as an "unrepresented" manual posting and the derived HAV
	double-counts the capitalized cost (client, 2026-08-25: HAV 33,000
	instead of 18,000)."""
	filters = {
		"source_doctype": "Asset Repair",
		"transaction_type": "Capitalized Repair",
		"status": "Posted",
		"voucher_no": ("is", "not set"),
	}
	if company:
		filters["company"] = company
	if asset:
		filters["asset"] = asset
	return frappe.get_all(
		"Financial Treatment",
		filters=filters,
		fields=["name", "asset", "source_name", "amount"],
	)


def _schedule_rebuild_needed(asset_name):
	"""Read-only: does the Active schedule still spread the double-counted
	NBV over its future rows?

	MEASURE THIS ONLY AFTER THE VOUCHER LINK IS SET. Before the link, the
	schedule and the derived NBV are BOTH inflated by the same repair GL,
	so they agree with each other and the divergence is invisible — the
	first cut of this backfill asked the question too early, always got
	False, and left the corrupted schedule in place while reporting that
	nothing needed rebuilding (2026-08-25 review).
	"""
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import last_posted_schedule_date

	if not frappe.db.exists(
		"Asset Depreciation Schedule",
		{"asset": asset_name, "status": "Active", "docstatus": 1},
	):
		return False
	values = recalculate_asset_values(asset_name, save=False)
	salvage = flt(
		frappe.db.get_value("Asset Finance Book", {"parent": asset_name},
			"expected_value_after_useful_life") or 0
	)
	expected = flt(values["net_book_value"]) - salvage
	unposted = flt(
		frappe.db.sql(
			"""select coalesce(sum(ds.depreciation_amount), 0)
			   from `tabDepreciation Schedule` ds
			   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			   where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			     and ifnull(ds.journal_entry, '') = ''""",
			asset_name,
		)[0][0]
	)
	if expected <= 0 or abs(unposted - expected) < 0.01:
		return False
	# A rebuild respreads from the last posted row; with nothing posted
	# there is no anchor, so report what can actually be done.
	return bool(last_posted_schedule_date(asset_name))


def _rebuild_future_schedule(asset_name):
	"""Respread the future rows from the corrected derived base. Posted
	rows are preserved and the corrupted generation is Superseded, never
	cancelled (GAP-031/032, IA-05)."""
	from asset_enterprise.depreciation import last_posted_schedule_date, supersede_and_regenerate

	supersede_and_regenerate(
		asset_name,
		as_of_date=getdate(last_posted_schedule_date(asset_name)),
		reason=_("Rebuilt after repair-voucher backfill — future rows were "
			"spread over a double-counted NBV (client 2026-08-25)"),
	)


def _apply_voucher_link(row):
	"""Set the missing link, re-derive the asset's values, and report
	whether the future schedule still needs rebuilding. Returns True when
	a rebuild is required."""
	from asset_enterprise.asset_values import recalculate_asset_values

	frappe.db.set_value(
		"Financial Treatment", row.name,
		{"voucher_type": "Asset Repair", "voucher_no": row.source_name},
		update_modified=False,
	)
	recalculate_asset_values(row.asset, save=True)
	return _schedule_rebuild_needed(row.asset)


def link_repair_voucher_references(company=None, asset=None, dry_run=1):
	"""Report (dry_run=1) or set (dry_run=0) the missing voucher link on
	Capitalized Repair treatments, then recalculate the affected assets
	so their ledger-derived HAV drops the double-count, and rebuild the
	future schedule rows that were spread over the corrupted NBV.

	    bench --site <site> execute \\
	        asset_enterprise.repair.link_repair_voucher_references \\
	        --kwargs "{'dry_run': 1}"
	    bench --site <site> execute \\
	        asset_enterprise.repair.link_repair_voucher_references \\
	        --kwargs "{'dry_run': 0}"
	"""
	stuck = find_unlinked_repair_treatments(company=company, asset=asset)
	print(f"{len(stuck)} Capitalized Repair treatment(s) without a voucher link:")
	for row in stuck:
		if dry_run:
			# The divergence only becomes visible once the link is set,
			# so the dry run applies the fix inside a savepoint, reads
			# the answer, and rolls back — the report is then exactly
			# what the live run will do, and nothing is written.
			frappe.db.savepoint("ae_repair_voucher_backfill")
			try:
				needs = _apply_voucher_link(row)
			finally:
				frappe.db.rollback(save_point="ae_repair_voucher_backfill")
		else:
			needs = _apply_voucher_link(row)
			if needs:
				_rebuild_future_schedule(row.asset)
		print(
			f"  FT {row.name}  asset {row.asset}  repair {row.source_name}  "
			f"amount {flt(row.amount):,.2f}  schedule-rebuild-needed={needs}"
		)
	if not dry_run:
		frappe.db.commit()
	return stuck


def find_orphaned_posted_rows(company=None, asset=None):
	"""Depreciation that is POSTED in the ledger but sits only on a
	SUPERSEDED generation — invisible to the Active schedule.

	GAP-031/032 make the Active generation the complete record: posted
	rows are copied verbatim into every generation, and asset_values sums
	posted rows from the Active one alone. A journal entry stamped on a
	generation after it was superseded breaks that — the charge is in the
	GL, the Active schedule still shows the period as due, and the
	scheduler will charge it again (UAT ACC-ASS-2026-00191, 2026-08-25:
	three 2026 entries landed on ACC-ADS-2026-00401 78 seconds after a
	capitalization superseded it).
	"""
	filters = ""
	values = {}
	if company:
		filters += " and a.company = %(company)s"
		values["company"] = company
	if asset:
		filters += " and a.name = %(asset)s"
		values["asset"] = asset
	return frappe.db.sql(
		f"""
		select old.asset, old.name as superseded_schedule, ds.name as row_name,
		       ds.schedule_date, ds.depreciation_amount, ds.journal_entry,
		       ds.days_in_period, ds.daily_rate, ds.cost_center, ds.is_pya_entry,
		       live.name as active_schedule
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` old on ds.parent = old.name
		join `tabAsset` a on a.name = old.asset
		join `tabAsset Depreciation Schedule` live
		     on live.asset = old.asset and live.status = 'Active' and live.docstatus = 1
		where old.status <> 'Active' and old.docstatus = 1
		  and ifnull(ds.journal_entry, '') <> ''
		  and ifnull(ds.reversal_journal_entry, '') = ''
		  and not exists (
		      select 1 from `tabDepreciation Schedule` cur
		      where cur.parent = live.name and cur.journal_entry = ds.journal_entry
		  )
		  {filters}
		order by old.asset, ds.schedule_date
		""",
		values,
		as_dict=True,
	)


def repair_orphaned_posted_rows(company=None, asset=None, dry_run=1):
	"""Carry orphaned posted rows onto the Active generation verbatim,
	then respread what is left from the corrected base.

	    bench --site <site> execute \\
	        asset_enterprise.repair.repair_orphaned_posted_rows \\
	        --kwargs "{'dry_run': 1}"
	"""
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import last_posted_schedule_date, supersede_and_regenerate

	rows = find_orphaned_posted_rows(company=company, asset=asset)
	by_asset = {}
	for row in rows:
		by_asset.setdefault(row.asset, []).append(row)
	print(f"{len(rows)} posted row(s) stranded on superseded generations, {len(by_asset)} asset(s):")

	for asset_name, orphans in by_asset.items():
		print(f"  {asset_name}  Active {orphans[0].active_schedule}")
		unmatched = []
		for row in orphans:
			target = frappe.db.get_value(
				"Depreciation Schedule",
				{
					"parent": orphans[0].active_schedule,
					"schedule_date": row.schedule_date,
					"journal_entry": ("in", ("", None)),
				},
				"name",
			)
			print(
				f"    {row.schedule_date}  {flt(row.depreciation_amount):,.2f}  "
				f"{row.journal_entry}  (from {row.superseded_schedule})  "
				f"-> {target or 'NO MATCHING ROW'}"
			)
			if not target:
				# Never invent a row: the Active generation may legitimately
				# have re-priced the period away. Report it for a human.
				unmatched.append(row)
				continue
			if dry_run:
				continue
			frappe.db.set_value(
				"Depreciation Schedule", target,
				{
					"depreciation_amount": row.depreciation_amount,
					"days_in_period": row.days_in_period,
					"daily_rate": row.daily_rate,
					"cost_center": row.cost_center,
					"is_pya_entry": row.is_pya_entry,
					"journal_entry": row.journal_entry,
				},
				update_modified=False,
			)
		if unmatched:
			print(f"    ! {len(unmatched)} row(s) have no matching period on the Active "
			      f"generation — reported, not written.")
		if dry_run:
			continue
		# The base moved: respread the remaining rows so the Active
		# generation's unposted total lands on the derived NBV again.
		recalculate_asset_values(asset_name, save=True)
		last_posted = last_posted_schedule_date(asset_name)
		if last_posted:
			supersede_and_regenerate(
				asset_name,
				as_of_date=getdate(last_posted),
				reason=_("Rebuilt after recovering depreciation stranded on a "
					"superseded generation (GAP-031/032)"),
			)
			recalculate_asset_values(asset_name, save=True)
	if not dry_run:
		frappe.db.commit()
	return rows
