"""Phase 7 verification — run: bench --site <site> execute asset_enterprise.setup.verify_phase7.run"""

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

	frappe.db.savepoint("phase7_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		# Invoice Adjustment requires PI rate != PR rate — a Badia go-live
		# config item (imp runbook): Buying Settings maintain_same_rate off.
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		frappe.db.set_single_value(
			"Accounts Settings", "over_billing_allowance", 100
		)  # PI > PR headroom for the Invoice Adjustment smoke
		diff_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Asset", "is_group": 0}, "name"
		)
		frappe.db.set_value(
			"Company", company, "default_asset_invoice_difference_account", diff_account,
			update_modified=False,
		)

		# ------------------------------------------------ fixtures: PR chain
		from asset_enterprise.setup.test_fixtures import make_test_asset

		# Base masters via the existing helper (category/item/location).
		seed = make_test_asset(company, gross=1, submit=False)  # creates category+item
		item = frappe.get_doc("Item", "AE-SMOKE-ITEM")
		item.auto_create_assets = 1
		item.asset_naming_series = frappe.get_meta("Asset").get_field("naming_series").options.split(
			"\n"
		)[0]
		item.flags.ignore_permissions = True
		item.save()

		supplier = frappe.db.get_value("Supplier", {}, "name")
		if not supplier:
			supplier = (
				frappe.get_doc({"doctype": "Supplier", "supplier_name": "AE Smoke Supplier"})
				.insert(ignore_permissions=True)
				.name
			)

		pr = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": "AE-SMOKE-ITEM",
						"qty": 2,
						"rate": 1000,
						"asset_location": seed.location,
					}
				],
			}
		)
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()

		assets = frappe.get_all(
			"Asset", filters={"purchase_receipt": pr.name}, pluck="name", order_by="name"
		)
		linked_flag = frappe.db.get_value(
			"Purchase Receipt Item", pr.items[0].name, "asset_linked"
		)
		pr_ok = len(assets) == 2 and linked_flag == 1
		print(
			f"pr     auto-created assets={len(assets)} (want 2) asset_linked={linked_flag} "
			f"{'OK' if pr_ok else 'FAIL'}"
		)
		ok = ok and pr_ok

		# PR cancel guard: submit one asset, PR cancel must block.
		a1 = frappe.get_doc("Asset", assets[0])
		a1.available_for_use_date = nowdate()
		a1.flags.ignore_permissions = True
		a1.save()
		a1.submit()
		try:
			pr.reload()
			pr.cancel()
			print("prgate FAIL (PR cancelled with submitted asset)")
			ok = False
		except frappe.ValidationError as e:
			g = "Reverse those assets" in str(e)
			print(f"prgate PR cancel blocked while asset submitted: {'OK' if g else 'FAIL'}")
			ok = ok and g

		# ------------------------------------------------ PI allocation + AVA
		pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": "AE-SMOKE-ITEM",
						"qty": 2,
						"rate": 1100,  # +100/unit over PR
						"purchase_receipt": pr.name,
						"pr_detail": pr.items[0].name,
					}
				],
				"pi_asset_allocation": [{"asset": assets[0]}],  # assets[1] stays free for the Option C probe
			}
		)
		pi.flags.ignore_permissions = True
		pi.insert()
		pi.submit()

		rows = frappe.get_all(
			"PI Asset Allocation",
			filters={"parent": pi.name},
			fields=["asset", "pi_delta_amount", "fx_delta_amount"],
			order_by="idx",
		)
		ava = frappe.db.get_value(
			"Asset Value Adjustment",
			{"asset": assets[0], "transaction_type": "Invoice Adjustment", "docstatus": 1},
			["name", "difference_amount"],
			as_dict=True,
		)
		from asset_enterprise.asset_values import recalculate_asset_values

		hav1 = recalculate_asset_values(assets[0], save=False)["historical_asset_value"]
		d_ok = (
			len(rows) == 1
			and all(flt(r.pi_delta_amount) == 100 for r in rows)
			and all(flt(r.fx_delta_amount) == 0 for r in rows)
			and ava
			and flt(ava.difference_amount) == 100
			and flt(hav1) == 1100
		)
		print(
			f"pidelta deltas={[(r.asset, r.pi_delta_amount, r.fx_delta_amount) for r in rows]} "
			f"AVA={ava and ava.name} diff={ava and ava.difference_amount} HAV(a1)={hav1} (want 1100) "
			f"{'OK' if d_ok else 'FAIL'}"
		)
		ok = ok and bool(d_ok)

		# Re-selection block: second PI covering the same asset.
		pi2 = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": "AE-SMOKE-ITEM",
						"qty": 1,
						"rate": 1000,
						"purchase_receipt": pr.name,
						"pr_detail": pr.items[0].name,
					}
				],
				"pi_asset_allocation": [{"asset": assets[0]}],
			}
		)
		pi2.flags.ignore_permissions = True
		try:
			pi2.insert()
			print("reselect FAIL (fully-invoiced asset re-selected)")
			ok = False
		except frappe.ValidationError as e:
			g = "fully-invoiced" in str(e).lower() or "already covered" in str(e)
			print(f"reselect blocked: {'OK' if g else 'FAIL'}")
			ok = ok and g

		# --------------------- Option B (v2.16 CH-05): warn, never block
		frappe.db.set_single_value("Asset Settings", "warn_invoice_below_receipt", 1)
		frappe.clear_messages()
		pi3 = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": "AE-SMOKE-ITEM",
						"qty": 1,
						"rate": 900,  # below PR
						"purchase_receipt": pr.name,
						"pr_detail": pr.items[0].name,
					}
				],
				"pi_asset_allocation": [{"asset": assets[1]}],
			}
		)
		pi3.flags.ignore_permissions = True
		try:
			pi3.insert()
			warned = any(
				"Below Receipt" in str(m.get("message", "")) or "below its receipt" in str(m.get("message", ""))
				for m in frappe.get_message_log()
			)
			print(f"optionb below-receipt PI saved with warning: {'OK' if warned else 'FAIL (no warning)'}")
			ok = ok and warned
		except frappe.ValidationError as e:
			print(f"optionb FAIL (below-receipt PI blocked under Option B): {e}")
			ok = False

		# ------------------------------------------- PI cancel unwinds via Reversal AVA
		pi.reload()
		pi.cancel()
		hav_after = recalculate_asset_values(assets[0], save=False)["historical_asset_value"]
		reversal_exists = frappe.db.exists(
			"Asset Value Adjustment", {"reversal_of_ava": ava.name, "docstatus": 1}
		)
		c_ok = flt(hav_after) == 1000 and bool(reversal_exists)
		print(
			f"picancel HAV back={hav_after} (want 1000) reversal AVA={'yes' if reversal_exists else 'no'} "
			f"{'OK' if c_ok else 'FAIL'}"
		)
		ok = ok and c_ok

	finally:
		frappe.db.rollback(save_point="phase7_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and not switch else 'FAIL'}")
		ok = ok and left == 0 and not switch

	print("PHASE 7:", "PASS" if ok else "FAIL")
