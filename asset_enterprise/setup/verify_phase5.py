"""Phase 5 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase5.run

Composite-merge round-trip inside one rolled-back savepoint.
"""

import traceback

import frappe
from frappe.utils import flt, nowdate


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()


def _run():
	ok = True
	company = frappe.db.get_value("Company", {}, "name")

	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.setup.test_fixtures import make_test_asset

	frappe.db.savepoint("phase5_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)

		# Clearing account default on Company (any non-group BS account works
		# for the smoke; production seeds a dedicated clearing account).
		clearing = frappe.db.get_value(
			"Account",
			{"company": company, "root_type": "Asset", "is_group": 0, "account_type": ""},
			"name",
		) or frappe.db.get_value(
			"Account", {"company": company, "root_type": "Asset", "is_group": 0}, "name"
		)
		frappe.db.set_value(
			"Company", company, "default_capitalization_clearing_account", clearing,
			update_modified=False,
		)
		print(f"setup  clearing account = {clearing}")

		# Target: a submitted Asset. (In production, composites arrive
		# submitted via core Standard Capitalization with
		# asset_type="Composite Asset"; core couples that type to its
		# own pipeline, so the smoke uses a plain submitted target —
		# the merge machinery is asset_type-agnostic.)
		composite = make_test_asset(company, gross=120_000, submit=False, with_depreciation=True)
		composite.submit()

		# Second asset needs its own item to avoid unique clash — reuse
		# category/item from fixture helper by name suffix.
		source = frappe.copy_doc(composite)
		source.asset_type = "Existing Asset"
		source.asset_name = "AE Smoke Source"
		source.gross_purchase_amount = 50_000
		source.purchase_amount = 50_000
		source.net_purchase_amount = 50_000
		source.flags.ignore_permissions = True
		source.insert()
		source.submit()

		# ------------------------------------------------ CM merge (GAP-014)
		cap = frappe.get_doc(
			{
				"doctype": "Asset Capitalization",
				"transaction_type": "Capitalized Maintenance",
				"transaction_sub_type": "Standard Maintenance",
				"target_asset": composite.name,
				"target_item_code": composite.item_code,
				"company": company,
				"posting_date": nowdate(),
				"asset_items": [{"asset": source.name}],
			}
		)
		cap.flags.ignore_permissions = True
		cap.flags.ignore_mandatory = True
		cap.insert()
		cap.submit()

		# Assertions.
		src_docstatus = frappe.db.get_value("Asset", source.name, "docstatus")
		src_merged_into = frappe.db.get_value("Asset", source.name, "merged_into_asset")
		log = frappe.get_all(
			"Composite Merge Log Entry",
			filters={"parent": composite.name},
			fields=[
				"merged_source_asset",
				"historical_value_at_merge",
				"accumulated_depreciation_at_merge",
				"net_book_value_at_merge",
				"remaining_useful_life_in_months",
				"remaining_useful_life_in_years",
				"status",
			],
		)
		hav = recalculate_asset_values(composite.name, save=False)["historical_asset_value"]
		je_name = frappe.db.get_value(
			"Journal Entry", {"user_remark": ("like", f"%{cap.name}%"), "docstatus": 1}, "name"
		)
		# Clearing nets to zero on the merge JE.
		net = frappe.db.sql(
			"""select coalesce(sum(debit) - sum(credit), 0) from `tabGL Entry`
			   where voucher_no = %s and account = %s and is_cancelled = 0""",
			(je_name, clearing),
		)[0][0]

		# Mid-period proration (GAP-015) may post catch-up depreciation on
		# the source before the merge — derive expectations from the
		# snapshot itself and assert internal consistency.
		snap_hav = flt(log[0].historical_value_at_merge) if log else 0
		snap_accum = flt(log[0].accumulated_depreciation_at_merge) if log else 0
		snap_nbv = flt(log[0].net_book_value_at_merge) if log else 0
		m_ok = (
			src_docstatus == 2
			and src_merged_into == composite.name
			and len(log) == 1
			and snap_hav == 50_000
			and flt(snap_hav - snap_accum, 2) == snap_nbv
			and log[0].status == "Active"
			and flt(hav, 2) == flt(120_000 + snap_nbv, 2)
			and flt(net) == 0
		)
		print(
			f"merge  src docstatus={src_docstatus} merged_into={src_merged_into == composite.name} "
			f"log_rows={len(log)} snapshot(HAV/Accum/NBV)=({snap_hav}/{snap_accum}/{snap_nbv}) "
			f"composite HAV={hav} (want 120000+NBV) clearing_net={net} {'OK' if m_ok else 'FAIL'}"
		)
		ok = ok and bool(m_ok)

		fts = frappe.get_all(
			"Financial Treatment",
			filters={"source_doctype": "Asset Capitalization", "source_name": cap.name},
			fields=["transaction_category", "asset", "hav_delta"],
			order_by="name",
		)
		ft_ok = (
			len(fts) == 2
			and {f.transaction_category for f in fts} == {"Disposal", "Addition"}
			and any(f.asset == source.name and flt(f.hav_delta) == -50_000 for f in fts)
			and any(f.asset == composite.name and flt(f.hav_delta) == snap_nbv for f in fts)
		)
		print(f"merge  FT pair: {[(f.transaction_category, f.asset, f.hav_delta) for f in fts]} {'OK' if ft_ok else 'FAIL'}")
		ok = ok and ft_ok

		# ------------------------------------- Reversal of CM (GAP-026/B7)
		cap.reload()
		cap.cancel()

		reversal = frappe.db.get_value(
			"Asset Capitalization",
			{"reversal_of_capitalization": cap.name, "docstatus": 1},
			"name",
		)
		log_status = frappe.db.get_value(
			"Composite Merge Log Entry",
			{"parent": composite.name, "merged_via_capitalization": cap.name},
			"status",
		)
		hav_after = recalculate_asset_values(composite.name, save=False)["historical_asset_value"]
		src_after = frappe.db.get_value("Asset", source.name, "docstatus")
		reversed_by = frappe.db.get_value("Asset Capitalization", cap.name, "reversed_by_capitalization")

		r_ok = (
			reversal
			and log_status == "Reversed"
			and hav_after == 120_000
			and src_after == 2  # stays cancelled — manual re-creation per design
			and reversed_by == reversal
		)
		print(
			f"revcm  reversal={reversal} log={log_status} HAV back={hav_after} "
			f"src stays cancelled={src_after == 2} backref={'OK' if reversed_by == reversal else reversed_by} "
			f"{'OK' if r_ok else 'FAIL'}"
		)
		ok = ok and bool(r_ok)

		# Loop guard.
		if reversal:
			try:
				frappe.get_doc("Asset Capitalization", reversal).cancel()
				print("revcm  loop-guard: FAIL")
				ok = False
			except frappe.ValidationError as e:
				guard = "cannot be" in str(e)
				print(f"revcm  loop-guard throw: {'OK' if guard else 'FAIL'}")
				ok = ok and guard

	finally:
		frappe.db.rollback(save_point="phase5_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and not switch else 'FAIL'}")
		ok = ok and left == 0 and not switch

	print("PHASE 5:", "PASS" if ok else "FAIL")
