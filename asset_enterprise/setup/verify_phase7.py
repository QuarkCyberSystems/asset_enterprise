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
	from asset_enterprise.setup.test_fixtures import pick_company, pick_plain_account
	company = pick_company()

	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	frappe.db.savepoint("phase7_verify")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		# Invoice Adjustment requires PI rate != PR rate — a Badia go-live
		# config item (imp runbook): Buying Settings maintain_same_rate off.
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		frappe.db.set_single_value(
			"Accounts Settings", "over_billing_allowance", 100
		)  # PI > PR headroom for the Invoice Adjustment smoke
		diff_account = frappe.db.sql(
			"""select name from tabAccount where company = %s and root_type = 'Asset'
			   and is_group = 0 and ifnull(account_type, '') not in
			   ('Receivable', 'Payable', 'Stock', 'Bank', 'Cash') limit 1""",
			company,
		)[0][0]
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

		# dedicated supplier — existing suppliers on live sites may carry
		# foreign-currency payable accounts (currency-mismatch throws).
		supplier = frappe.db.get_value("Supplier", {"supplier_name": "AE Smoke Supplier"}, "name")
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

		# --------------- Phase 11c D1: ARBNB/clearing reconcile via transfer JE
		transfer_je = frappe.db.get_value(
			"Journal Entry",
			{"user_remark": ("like", f"Invoice delta transfer for {pi.name}%"), "docstatus": 1},
			"name",
		)
		ava_je = frappe.db.get_value("Asset Value Adjustment", ava.name, "journal_entry")
		clearing_net = flt(frappe.db.sql(
			"""select coalesce(sum(debit - credit), 0) from `tabGL Entry`
			   where account = %s and is_cancelled = 0
			     and voucher_no in (%s, %s)""",
			(diff_account, transfer_je or "x", ava_je or ava.name),
		)[0][0]) if transfer_je else None
		d1_ok = bool(transfer_je) and clearing_net == 0
		print(
			f"d1     delta transfer JE={transfer_je} clearing-net={clearing_net} (want 0) "
			f"{'OK' if d1_ok else 'FAIL'}"
		)
		ok = ok and d1_ok

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

		# --------------- Phase 11c D1: Case A.02 — disposed asset expensed
		expense = pick_plain_account(company, "Expense")
		frappe.db.set_value(
			"Company", company,
			{"default_post_disposal_invoice_diff_account": expense,
			 "disposal_account": frappe.db.get_value("Company", company, "disposal_account") or expense},
			update_modified=False,
		)
		from asset_enterprise import disposal as _disposal

		_disposal.scrap_asset(assets[0], scrapping_type="Damage")
		avas_before = frappe.db.count(
			"Asset Value Adjustment", {"asset": assets[0], "docstatus": 1})
		pi4 = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": "AE-SMOKE-ITEM",
						"qty": 1,
						"rate": 1200,  # +200 over PR on a scrapped asset
						"purchase_receipt": pr.name,
						"pr_detail": pr.items[0].name,
					}
				],
				"pi_asset_allocation": [{"asset": assets[0]}],
			}
		)
		pi4.flags.ignore_permissions = True
		pi4.insert()
		pi4.submit()
		a02_ft = frappe.db.exists(
			"Financial Treatment",
			{"source_name": pi4.name, "transaction_type": "Post-Disposal Invoice Adjustment",
			 "status": "Posted"},
		)
		avas_after = frappe.db.count(
			"Asset Value Adjustment", {"asset": assets[0], "docstatus": 1})
		new_ava = avas_after - avas_before
		hav_scrapped = recalculate_asset_values(assets[0], save=False)["historical_asset_value"]
		a02_ok = bool(a02_ft) and new_ava == 0 and flt(hav_scrapped) == 0
		print(
			f"a02    disposed-asset delta expensed: FT={bool(a02_ft)} new-AVAs={new_ava} (want 0) "
			f"HAV stays {hav_scrapped} (want 0) {'OK' if a02_ok else 'FAIL'}"
		)
		ok = ok and a02_ok

		# ------- GAP-012 client defect (2026-08-16): the allocation table
		# is a partial-invoice disambiguator, NOT the on-switch for the
		# invoice-difference treatment. An ordinary invoice that names no
		# assets must still route its delta.
		pr5 = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{"item_code": "AE-SMOKE-ITEM", "qty": 1, "rate": 1000,
					 "asset_location": seed.location}
				],
			}
		)
		pr5.flags.ignore_permissions = True
		pr5.insert()
		pr5.submit()
		asset5 = frappe.get_all("Asset", filters={"purchase_receipt": pr5.name}, pluck="name")[0]
		a5 = frappe.get_doc("Asset", asset5)
		a5.available_for_use_date = nowdate()
		a5.flags.ignore_permissions = True
		a5.save()
		a5.submit()

		pi5 = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{"item_code": "AE-SMOKE-ITEM", "qty": 1, "rate": 1300,  # +300 delta
					 "purchase_receipt": pr5.name, "pr_detail": pr5.items[0].name}
				],
				# deliberately NO pi_asset_allocation — the client's case
			}
		)
		pi5.flags.ignore_permissions = True
		pi5.insert()
		pi5.submit()

		auto_rows = frappe.get_all(
			"PI Asset Allocation", filters={"parent": pi5.name},
			fields=["asset", "pi_delta_amount"],
		)
		auto_ava = frappe.db.get_value(
			"Asset Value Adjustment",
			{"asset": asset5, "transaction_type": "Invoice Adjustment", "docstatus": 1},
			["name", "difference_amount"], as_dict=True,
		)
		auto_je = frappe.db.get_value(
			"Journal Entry",
			{"user_remark": ("like", f"Invoice delta transfer for {pi5.name}%"), "docstatus": 1},
			"name",
		)
		hav5 = recalculate_asset_values(asset5, save=False)["historical_asset_value"]
		auto_ok = (
			len(auto_rows) == 1
			and auto_rows[0].asset == asset5
			and flt(auto_rows[0].pi_delta_amount) == 300
			and auto_ava
			and flt(auto_ava.difference_amount) == 300
			and bool(auto_je)
			and flt(hav5) == 1300
		)
		print(
			f"autoall unallocated PI auto-resolved rows={[(r.asset, r.pi_delta_amount) for r in auto_rows]} "
			f"AVA={auto_ava and auto_ava.difference_amount} JE={auto_je} HAV={hav5} (want 1300) "
			f"{'OK' if auto_ok else 'FAIL'}"
		)
		ok = ok and bool(auto_ok)

		# Ambiguous partial invoice still asks the user — but SAYS so.
		pr6 = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{"item_code": "AE-SMOKE-ITEM", "qty": 3, "rate": 1000,
					 "asset_location": seed.location}
				],
			}
		)
		pr6.flags.ignore_permissions = True
		pr6.insert()
		pr6.submit()
		pi6 = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": company,
				"supplier": supplier,
				"posting_date": nowdate(),
				"items": [
					{"item_code": "AE-SMOKE-ITEM", "qty": 1, "rate": 1100,
					 "purchase_receipt": pr6.name, "pr_detail": pr6.items[0].name}
				],
			}
		)
		pi6.flags.ignore_permissions = True
		try:
			pi6.insert()
			print("ambig  FAIL (partial invoice accepted without an asset selection)")
			ok = False
		except frappe.ValidationError as e:
			g = "Asset Allocation" in str(e)
			print(f"ambig  partial invoice asks for the asset selection: {'OK' if g else 'FAIL'}")
			ok = ok and g

	finally:
		frappe.db.rollback(save_point="phase7_verify")
		left = frappe.db.count("Asset", {"asset_name": ("like", "AE Smoke%")})
		switch = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
		print(f"clean  rollback: leftovers={left} switch={switch} {'OK' if left == 0 and switch == switch_before else 'FAIL'}")
		ok = ok and left == 0 and switch == switch_before

	print("PHASE 7:", "PASS" if ok else "FAIL")
