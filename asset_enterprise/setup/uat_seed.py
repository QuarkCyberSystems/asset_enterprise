"""UAT dataset seeder — creates PERSISTENT, clickable test data on the
UAT site (no savepoints, no rollback). Every document is prefixed
"UAT-FA" so the dataset is recognizable and removable.

Run:    bench --site <site> execute asset_enterprise.setup.uat_seed.run
Wipe:   bench --site <site> execute asset_enterprise.setup.uat_seed.wipe

Each scenario commits independently; a failure in one scenario is
reported and does not block the others. The final manifest maps every
created document to the UAT script scenario it demonstrates.
"""

import traceback

import frappe
from frappe import _
from frappe.utils import add_days, add_months, flt, get_first_day, getdate, nowdate

PREFIX = "UAT-FA"
MANIFEST = []


def _note(scenario, doctype, name, look_for):
	MANIFEST.append((scenario, doctype, name, look_for))
	print(f"  [{scenario}] {doctype} {name} — {look_for}")


def _fail(scenario, exc):
	print(f"  [{scenario}] FAILED: {exc}")
	MANIFEST.append((scenario, "ERROR", str(exc)[:120], "scenario incomplete"))
	frappe.db.rollback()


# ------------------------------------------------------------------ masters


def _account(company, root_type=None, account_type=None, name_like=None):
	if account_type:
		return frappe.db.get_value(
			"Account", {"company": company, "is_group": 0, "account_type": account_type}, "name"
		)
	from asset_enterprise.setup.test_fixtures import pick_plain_account

	return pick_plain_account(company, root_type)


def _ensure_masters(company):
	fixed_asset = _account(company, account_type="Fixed Asset")
	accum = _account(company, account_type="Accumulated Depreciation")
	depr_exp = _account(company, account_type="Depreciation") or _account(company, root_type="Expense")
	liability = _account(company, root_type="Liability")
	expense = _account(company, root_type="Expense")

	for cat in (f"{PREFIX} Category A", f"{PREFIX} Category B"):
		if not frappe.db.exists("Asset Category", cat):
			frappe.get_doc(
				{
					"doctype": "Asset Category",
					"asset_category_name": cat,
					"accounts": [
						{
							"company_name": company,
							"fixed_asset_account": fixed_asset,
							"accumulated_depreciation_account": accum,
							"depreciation_expense_account": depr_exp,
						}
					],
				}
			).insert(ignore_permissions=True)

	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	for item_code, cat in (
		(f"{PREFIX}-ITEM-A", f"{PREFIX} Category A"),
		(f"{PREFIX}-ITEM-B", f"{PREFIX} Category B"),
	):
		if not frappe.db.exists("Item", item_code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"item_group": item_group,
					"is_fixed_asset": 1,
					"is_stock_item": 0,
					"asset_category": cat,
					"auto_create_assets": 1,
					"asset_naming_series": frappe.get_meta("Asset")
					.get_field("naming_series")
					.options.split("\n")[0],
				}
			).insert(ignore_permissions=True)

	if not frappe.db.exists("Supplier", {"supplier_name": f"{PREFIX} Supplier"}):
		frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": f"{PREFIX} Supplier"}
		).insert(ignore_permissions=True)

	location = frappe.db.get_value("Location", {}, "name")
	if not location:
		location = (
			frappe.get_doc({"doctype": "Location", "location_name": f"{PREFIX} Location"})
			.insert(ignore_permissions=True)
			.name
		)

	# Settings + account defaults (fill only what is empty — these are
	# the go-live configuration anyway).
	frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	if flt(frappe.db.get_single_value("Accounts Settings", "over_billing_allowance")) < 100:
		frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 100)

	company_defaults = {
		"default_asset_suspense_account": liability,
		"default_asset_invoice_difference_account": liability,
		"default_capitalization_clearing_account": liability,
		"default_post_disposal_invoice_diff_account": expense,
		"default_pya_expense_account": expense,
		"disposal_account": expense,
		"exchange_gain_loss_account": expense,
	}
	for field, value in company_defaults.items():
		if not frappe.db.get_value("Company", company, field):
			frappe.db.set_value("Company", company, field, value, update_modified=False)

	if not frappe.db.get_value(
		"Scrapping Type Account", {"parent": "Damage", "company": company}, "name"
	):
		frappe.get_doc(
			{
				"doctype": "Scrapping Type Account",
				"parenttype": "Scrapping Type",
				"parent": "Damage",
				"parentfield": "accounts",
				"company": company,
				"gl_account": expense,
			}
		).db_insert()

	if not frappe.db.get_value(
		"Asset Settings Authority Role", {"parent": "Asset Settings"}, "name"
	):
		frappe.get_doc(
			{
				"doctype": "Asset Settings Authority Role",
				"parenttype": "Asset Settings",
				"parent": "Asset Settings",
				"parentfield": "mass_depreciation_authority_roles",
				"role": "System Manager",
			}
		).db_insert()

	tol = frappe.db.get_value(
		"Asset Settings Tolerance", {"parent": "Asset Settings", "company": company}, "name"
	)
	if tol:
		frappe.db.set_value(
			"Asset Settings Tolerance", tol, "tolerance_approver", "System Manager",
			update_modified=False,
		)
	else:
		frappe.get_doc(
			{
				"doctype": "Asset Settings Tolerance",
				"parenttype": "Asset Settings",
				"parent": "Asset Settings",
				"parentfield": "tolerance_settings",
				"company": company,
				"default_last_period_tolerance_amount": 1,
				"tolerance_approver": "System Manager",
			}
		).db_insert()

	return frappe._dict(location=location, liability=liability, expense=expense)


def _make_asset(
	company,
	name,
	gross,
	location,
	item=None,
	category=None,
	submit=True,
	with_depreciation=False,
	months=24,
	start=None,
	salvage=0,
	opening_accum=0,
):
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"item_code": item or f"{PREFIX}-ITEM-A",
			"asset_name": name,
			"asset_category": category or f"{PREFIX} Category A",
			"location": location,
			"purchase_amount": gross,
			"net_purchase_amount": gross,
			"opening_accumulated_depreciation": opening_accum,
			"purchase_date": add_months(nowdate(), -4),
			"available_for_use_date": start or add_months(nowdate(), -3),
			"calculate_depreciation": 1 if with_depreciation else 0,
		}
	)
	if with_depreciation:
		asset.append(
			"finance_books",
			{
				"depreciation_method": "Straight Line",
				"total_number_of_depreciations": months,
				"frequency_of_depreciation": 1,
				"depreciation_start_date": start or get_first_day(nowdate()),
				"expected_value_after_useful_life": salvage,
				"daily_prorata_based": 1,
			},
		)
	asset.flags.ignore_permissions = True
	asset.insert()
	if submit:
		asset.submit()
	return asset


# ---------------------------------------------------------------- scenarios


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()


def _run():
	from asset_enterprise.setup.test_fixtures import pick_company
	company = pick_company()
	print(f"Seeding persistent UAT dataset on company: {company}\n")
	m = _ensure_masters(company)
	frappe.db.commit()
	supplier = frappe.db.get_value("Supplier", {"supplier_name": f"{PREFIX} Supplier"}, "name")

	# --- A: existing asset with suspense opening JE (TC-001) -------------
	try:
		a1 = _make_asset(
			company, f"{PREFIX} A1 Existing Asset (Suspense Opening)", 1_000_000,
			m.location, opening_accum=400_000,
		)
		ft = frappe.db.get_value(
			"Financial Treatment",
			{"asset": a1.name, "transaction_type": "Existing-Asset Opening"},
			["name", "journal_entry"], as_dict=True,
		)
		_note("A", "Asset", a1.name, "Enterprise tab values; opening JE "
			f"{ft and ft.journal_entry}: DR FA 1,000,000 / CR Accum 400,000 / CR Suspense 600,000")
		frappe.db.commit()
	except Exception as e:
		_fail("A", e)

	# --- B: PR -> asset -> PI +delta -> transfer JE + AVA (TC-023) -------
	try:
		pr = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{"item_code": f"{PREFIX}-ITEM-A", "qty": 1, "rate": 50_000,
					 "asset_location": m.location}
				],
			}
		)
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		pr_asset_name = frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
		pr_asset = frappe.get_doc("Asset", pr_asset_name)
		pr_asset.asset_name = f"{PREFIX} B1 Purchased Asset (PR->PI Delta)"
		pr_asset.available_for_use_date = nowdate()
		pr_asset.flags.ignore_permissions = True
		pr_asset.save()
		pr_asset.submit()

		pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{"item_code": f"{PREFIX}-ITEM-A", "qty": 1, "rate": 55_000,
					 "purchase_receipt": pr.name, "pr_detail": pr.items[0].name}
				],
				# NO pi_asset_allocation — TC-023's steps are "submit a PI
				# for a different amount against the same asset", nothing
				# more. Pre-filling the table here is what hid GAP-012.
			}
		)
		pi.flags.ignore_permissions = True
		pi.insert()
		pi.submit()
		transfer_je = frappe.db.get_value(
			"Journal Entry",
			{"user_remark": ("like", f"Invoice delta transfer for {pi.name}%")}, "name")
		_note("B", "Purchase Receipt", pr.name, "asset auto-created at delivery, Asset Linked flag")
		_note("B", "Purchase Invoice", pi.name,
			f"+5,000 delta: transfer JE {transfer_je} (ARBNB→clearing) + auto Invoice-Adjustment AVA; asset HAV = 55,000")
		_note("B", "Asset", pr_asset.name, "HAV 55,000 after invoice adjustment")
		frappe.db.commit()
	except Exception as e:
		_fail("B", e)

	# --- C: depreciating asset, mass run posts 3 months (TC-008/015) ----
	try:
		c1 = _make_asset(
			company, f"{PREFIX} C1 Depreciating Asset (3 Months Posted)", 73_000,
			m.location, with_depreciation=True, months=24,
			start=get_first_day(add_months(nowdate(), -3)),
		)
		mad = frappe.get_doc(
			{
				"doctype": "Mass Asset Depreciation",
				"company": company,
				"posting_date": nowdate(),
				"mode": "Selected Assets",
				"selected_assets": [{"asset": c1.name}],
			}
		)
		mad.flags.ignore_permissions = True
		mad.insert()
		mad.submit()
		_note("C/E", "Mass Asset Depreciation", mad.name,
			"result rows with clickable JE links; 3 monthly rows posted")
		_note("C/E", "Asset", c1.name,
			"Active schedule: 3 posted rows w/ JEs, future rows pending; Enterprise tab Accum/NBV")
		frappe.db.commit()
	except Exception as e:
		_fail("C/E", e)

	# --- C2: UL adjustment +12 months (TC-025) --------------------------
	try:
		c2 = _make_asset(
			company, f"{PREFIX} C2 UL Adjustment +12mo", 36_000, m.location,
			with_depreciation=True, months=24, start=get_first_day(nowdate()),
		)
		ava = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment",
				"asset": c2.name, "company": company, "date": nowdate(),
				"transaction_type": "Useful Life Adjustment",
				"current_asset_value": 36_000, "new_asset_value": 36_000,
				"adjusted_life_months": 12,
				"difference_account": m.liability,
			}
		)
		ava.flags.ignore_permissions = True
		ava.insert()
		ava.submit()
		_note("G", "Asset Value Adjustment", ava.name,
			"UL +12: old schedule Superseded, new Active schedule ends 12 months later; finance book 36 periods")
		_note("G", "Asset", c2.name, "two schedules — Superseded + Active (supersedes link)")
		frappe.db.commit()
	except Exception as e:
		_fail("G-UL", e)

	# --- G: upward AVA then cancel -> Reversal AVA pair (TC-044) --------
	try:
		g1 = _make_asset(company, f"{PREFIX} G1 AVA Reversal Pair", 100_000, m.location)
		ava_up = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment",
				"asset": g1.name, "company": company, "date": nowdate(),
				"transaction_type": "Upward Revaluation",
				"current_asset_value": 100_000, "new_asset_value": 120_000,
				"difference_account": m.liability,
			}
		)
		ava_up.flags.ignore_permissions = True
		ava_up.insert()
		ava_up.submit()
		ava_up.reload()
		ava_up.cancel()
		reversal = frappe.db.get_value(
			"Asset Value Adjustment", {"reversal_of_ava": ava_up.name}, "name")
		_note("G", "Asset Value Adjustment", ava_up.name,
			f"cancelled → Reversal AVA {reversal} posted mirror JE; both FTs paired; HAV back to 100,000")
		frappe.db.commit()
	except Exception as e:
		_fail("G", e)

	# --- I: composite merge (TC-029/049) --------------------------------
	try:
		src1 = _make_asset(company, f"{PREFIX} I1 Merge Source 1", 30_000, m.location)
		src2 = _make_asset(company, f"{PREFIX} I2 Merge Source 2", 20_000, m.location)
		tgt = _make_asset(company, f"{PREFIX} I3 Composite Target", 50_000, m.location)
		cap = frappe.get_doc(
			{
				"doctype": "Asset Capitalization",
				"transaction_type": "Capitalized Maintenance",
				"transaction_sub_type": "Standard Maintenance",
				"target_asset": tgt.name, "company": company,
				"posting_date": nowdate(), "posting_time": frappe.utils.nowtime(),
				"entry_type": "Capitalization",
				"asset_items": [{"asset": src1.name}, {"asset": src2.name}],
			}
		)
		cap.flags.ignore_permissions = True
		cap.flags.ignore_mandatory = True
		cap.insert()
		cap.submit()
		_note("I", "Asset Capitalization", cap.name,
			"two-leg merge JE via Capitalization Clearing (nets zero)")
		_note("I", "Asset", tgt.name,
			"HAV 100,000; Merge Log: 2 rows w/ HAV/Accum/NBV + RUL snapshot; sources cancelled w/ merged-into link")
		frappe.db.commit()
	except Exception as e:
		_fail("I", e)

	# --- I2: reclassification A -> B (TC-028) ---------------------------
	try:
		r_src = _make_asset(company, f"{PREFIX} I4 Reclass Source (Cat A)", 40_000, m.location)
		r_tgt = frappe.get_doc(
			{
				"doctype": "Asset", "company": company,
				"item_code": f"{PREFIX}-ITEM-B",
				"asset_name": f"{PREFIX} I5 Reclass Target (Cat B)",
				"asset_category": f"{PREFIX} Category B",
				"location": m.location,
				"purchase_amount": 1, "net_purchase_amount": 1,
				"purchase_date": nowdate(), "available_for_use_date": nowdate(),
				"calculate_depreciation": 0,
			}
		)
		r_tgt.flags.ignore_permissions = True
		r_tgt.insert()  # draft target in the new category
		cap2 = frappe.get_doc(
			{
				"doctype": "Asset Capitalization",
				"transaction_type": "Capitalized Maintenance",
				"transaction_sub_type": "Reclassification / Asset Category Transfer",
				"target_asset": r_tgt.name, "company": company,
				"posting_date": nowdate(), "posting_time": frappe.utils.nowtime(),
				"entry_type": "Capitalization",
				"asset_items": [{"asset": r_src.name}],
			}
		)
		cap2.flags.ignore_permissions = True
		cap2.flags.ignore_mandatory = True
		cap2.insert()
		cap2.submit()
		_note("I", "Asset Capitalization", cap2.name,
			"reclassification JE: DR B-FA gross / CR A-FA gross (accum legs when present), NO clearing")
		_note("I", "Asset", r_tgt.name,
			"took over gross 40,000 under Category B; Reclassified From link; NO suspense JE")
		frappe.db.commit()
	except Exception as e:
		_fail("I-Reclass", e)

	# --- J: scrap family -------------------------------------------------
	try:
		j1 = _make_asset(company, f"{PREFIX} J1 Full Scrap", 25_000, m.location)
		tx = frappe.get_doc(
			{
				"doctype": "Scrap Transaction", "asset": j1.name, "company": company,
				"transaction_date": nowdate(), "scrap_type": "Full Scrap",
				"scrapping_type": "Damage",
			}
		)
		tx.flags.ignore_permissions = True
		tx.insert()
		tx.submit()
		_note("J", "Scrap Transaction", tx.name,
			"full scrap document w/ linked JE (DR loss 25,000 / CR FA 25,000)")
		frappe.db.commit()
	except Exception as e:
		_fail("J1", e)

	try:
		j2 = _make_asset(company, f"{PREFIX} J2 Partial Scrap 20%", 50_000, m.location)
		tx2 = frappe.get_doc(
			{
				"doctype": "Scrap Transaction", "asset": j2.name, "company": company,
				"transaction_date": nowdate(), "scrap_type": "Partial Scrap",
				"scrapping_type": "Damage", "mode": "By Value", "scrap_value": 10_000,
			}
		)
		tx2.flags.ignore_permissions = True
		tx2.insert()
		tx2.submit()
		_note("J", "Scrap Transaction", tx2.name,
			"partial scrap 10,000; asset stays active, HAV 40,000, schedule superseded")
		frappe.db.commit()
	except Exception as e:
		_fail("J2", e)

	try:
		j3 = _make_asset(
			company, f"{PREFIX} J3 Cross-Period Restore (Path 3)", 36_500, m.location,
			with_depreciation=True, months=24, start=get_first_day(add_months(nowdate(), 2)),
		)
		from asset_enterprise import disposal as _disposal
		from asset_enterprise.restore import cross_period_restore

		_disposal.scrap_asset(
			j3.name, scrap_date=add_days(get_first_day(nowdate()), -10), scrapping_type="Damage"
		)
		mirror = cross_period_restore(j3.name)
		_note("J", "Asset", j3.name,
			f"scrapped last month, restored via {mirror}; Active schedule's first unposted row is the CATCH-UP row (>31 days)")
		frappe.db.commit()
	except Exception as e:
		_fail("J3", e)

	try:
		j4 = _make_asset(company, f"{PREFIX} J4 Scrapped + Replacement", 15_000, m.location)
		tx4 = frappe.get_doc(
			{
				"doctype": "Scrap Transaction", "asset": j4.name, "company": company,
				"transaction_date": nowdate(), "scrap_type": "Full Scrap",
				"scrapping_type": "Damage",
			}
		)
		tx4.flags.ignore_permissions = True
		tx4.insert()
		tx4.submit()
		from asset_enterprise.restore import create_replacement_asset

		repl = create_replacement_asset(j4.name)
		_note("J", "Asset", j4.name, f"scrapped; replacement draft {repl} w/ two-way link")
		frappe.db.commit()
	except Exception as e:
		_fail("J4", e)

	# --- K: movement with CC split (TC-034/035/037) ---------------------
	try:
		k1 = _make_asset(
			company, f"{PREFIX} K1 Movement + CC Split", 24_000, m.location,
			with_depreciation=True, months=24, start=get_first_day(nowdate()),
		)
		cc2 = frappe.db.get_value(
			"Cost Center",
			{"company": company, "is_group": 0,
			 "name": ["!=", frappe.db.get_value("Asset", k1.name, "cost_center") or ""]},
			"name",
		)
		loc2 = frappe.db.get_value("Location", {"name": ["!=", m.location]}, "name")
		if not loc2:
			loc2 = (
				frappe.get_doc({"doctype": "Location", "location_name": f"{PREFIX} Location 2"})
				.insert(ignore_permissions=True)
				.name
			)
		emp = frappe.db.get_value("Employee", {"company": company}, "name")
		mv = frappe.get_doc(
			{
				"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
				"transaction_date": frappe.utils.now(),
				"assets": [
					{"asset": k1.name, "target_location": loc2,
					 **({"to_employee": emp} if emp else {}),
					 "target_cost_center": cc2}
				],
			}
		)
		mv.flags.ignore_permissions = True
		mv.insert()
		mv.submit()
		_note("K", "Asset Movement", mv.name,
			"location + custodian + cost center in ONE movement; schedule row split by CC mid-period; combined history row")
		frappe.db.commit()
	except Exception as e:
		_fail("K", e)

	# --- M1: tolerance manual-post flow ---------------------------------
	try:
		from asset_enterprise.depreciation import enable_depreciation, post_final_row

		t1 = _make_asset(company, f"{PREFIX} M1 Tolerance Manual Post", 20_000, m.location)
		enable_depreciation(
			t1.name, total_number_of_depreciations=2, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(add_months(nowdate(), -2)),
		)
		# Simulate accumulated drift REALISTICALLY: shift 50 from the
		# first row into the final absorbing row — the schedule still
		# sums to the base (NBV lands on salvage), but the final row
		# deviates from nominal rate x days beyond the tolerance.
		rows_t1 = frappe.db.sql(
			"""select ds.name, ds.depreciation_amount from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status = 'Active'
			order by ds.schedule_date""",
			t1.name, as_dict=True,
		)
		frappe.db.set_value(
			"Depreciation Schedule", rows_t1[0].name, "depreciation_amount",
			flt(rows_t1[0].depreciation_amount) - 50, update_modified=False,
		)
		frappe.db.set_value(
			"Depreciation Schedule", rows_t1[-1].name, "depreciation_amount",
			flt(rows_t1[-1].depreciation_amount) + 50, update_modified=False,
		)
		mad2 = frappe.get_doc(
			{
				"doctype": "Mass Asset Depreciation", "company": company,
				"posting_date": nowdate(), "mode": "Selected Assets",
				"selected_assets": [{"asset": t1.name}],
			}
		)
		mad2.flags.ignore_permissions = True
		mad2.insert()
		mad2.submit()
		je = post_final_row(t1.name, override_tolerance=1)
		_note("M", "Mass Asset Depreciation", mad2.name,
			"first row Posted w/ JE link; FINAL row 'Manual Posting Required' (beyond tolerance)")
		_note("M", "Asset", t1.name,
			f"final row posted manually with Tolerance Approver override → JE {je}")
		frappe.db.commit()
	except Exception as e:
		_fail("M", e)

	# --- F: tree ---------------------------------------------------------
	try:
		p1 = _make_asset(company, f"{PREFIX} F1 Tree Parent", 60_000, m.location)
		ch1 = _make_asset(company, f"{PREFIX} F2 Tree Child 1", 20_000, m.location)
		ch2 = _make_asset(company, f"{PREFIX} F3 Tree Child 2", 10_000, m.location)
		for ch in (ch1, ch2):
			frappe.db.set_value("Asset", ch.name, "parent_asset", p1.name, update_modified=False)
		_note("F", "Asset", p1.name,
			"Manage → Tree Summary: 3 assets, HAV 90,000 aggregated")
		frappe.db.commit()
	except Exception as e:
		_fail("F", e)

	# ---------------------------------------------------------- manifest
	print("\n================ UAT DATASET MANIFEST ================")
	for scenario, doctype, name, look in MANIFEST:
		print(f"{scenario:8} | {doctype:26} | {name:28} | {look}")
	print("======================================================")
	print("All documents are PERSISTENT (committed). Wipe with uat_seed.wipe.")


# -------------------------------------------------------------------- wipe


def wipe():
	"""Best-effort removal of a previous UAT-FA dataset (test systems
	only). Deletes in reverse dependency order with force."""
	from asset_enterprise.setup.test_fixtures import pick_company
	company = pick_company()
	assets = frappe.get_all(
		"Asset", filters={"asset_name": ("like", f"{PREFIX}%")}, pluck="name"
	)
	if not assets:
		print("nothing to wipe")
		return
	print(f"wiping {len(assets)} UAT-FA assets and linked documents ...")

	def _force_delete(doctype, names):
		has_docstatus = frappe.get_meta(doctype).is_submittable
		for n in names:
			try:
				if has_docstatus and frappe.db.get_value(doctype, n, "docstatus") == 1:
					# test-system tool: drop to cancelled at DB level so
					# force-delete is allowed (no reversal side effects).
					frappe.db.set_value(doctype, n, "docstatus", 2, update_modified=False)
				frappe.delete_doc(doctype, n, force=1, ignore_permissions=True)
			except Exception as e:
				print(f"  keep {doctype} {n}: {str(e)[:80]}")

	linked = lambda dt, field="asset": frappe.get_all(  # noqa: E731
		dt, filters={field: ("in", assets)}, pluck="name"
	)

	_force_delete("Asset Activity", linked("Asset Activity"))
	_force_delete("Financial Treatment", linked("Financial Treatment"))
	_force_delete("Scrap Transaction", linked("Scrap Transaction"))
	_force_delete(
		"Mass Asset Depreciation",
		frappe.get_all("Mass Asset Depreciation Asset", filters={"asset": ("in", assets)},
			pluck="parent"),
	)
	_force_delete(
		"Asset Capitalization",
		frappe.get_all("Asset Capitalization", filters={"target_asset": ("in", assets)},
			pluck="name"),
	)
	_force_delete(
		"Asset Movement",
		frappe.get_all("Asset Movement Item", filters={"asset": ("in", assets)}, pluck="parent"),
	)
	_force_delete(
		"Asset Value Adjustment",
		frappe.get_all("Asset Value Adjustment", filters={"asset": ("in", assets)}, pluck="name"),
	)
	_force_delete(
		"Asset Depreciation Schedule",
		frappe.get_all("Asset Depreciation Schedule", filters={"asset": ("in", assets)},
			pluck="name"),
	)
	je_rows = frappe.get_all(
		"Journal Entry Account",
		filters={"reference_type": "Asset", "reference_name": ("in", assets)},
		pluck="parent",
	)
	_force_delete("Journal Entry", sorted(set(je_rows)))
	pis = frappe.get_all(
		"PI Asset Allocation", filters={"asset": ("in", assets)}, pluck="parent")
	_force_delete("Purchase Invoice", sorted(set(pis)))
	prs = frappe.get_all(
		"Asset", filters={"name": ("in", assets), "purchase_receipt": ("!=", "")},
		pluck="purchase_receipt")
	_force_delete("Asset", assets)
	_force_delete("Purchase Receipt", sorted(set(prs)))
	frappe.db.commit()
	print("wipe done (masters/settings kept)")
