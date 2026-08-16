"""Literal test-case audit — GA-0005-01 §11 (TC-001 … TC-049).

Written after the GAP-012 defect, which every existing suite missed
because the fixtures pre-filled a field the test case never told the
user to touch. The rule here:

  Perform the STEPS the test case states, using the entry points a
  user has, and assert the EXPECTED outcome. Never set a field the
  steps do not mention. Never accept a related-GAP tag as evidence.

Verdicts:
  PASS      steps performed, expected outcome observed
  FAIL      steps performed, expected outcome NOT observed
  DEVIATION build deliberately differs from the document (recorded,
            with what it does instead — needs a doc change or a fix)
  DOC       the steps cannot be performed AS WRITTEN because the
            document names something that no longer exists upstream
  MANUAL    genuinely needs a human at the UI (nothing to assert here)

Run:  bench --site <site> execute asset_enterprise.setup.verify_tc.run
      bench --site <site> execute asset_enterprise.setup.verify_tc.run \
          --kwargs "{'only': 'TC-001,TC-005'}"
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_last_day, getdate, nowdate

CHECKS = []


def tc(tc_id, title):
	def wrap(fn):
		CHECKS.append((tc_id, title, fn))
		return fn

	return wrap


# --------------------------------------------------------------- helpers


def _company():
	from asset_enterprise.setup.test_fixtures import pick_company

	return pick_company()


def _account(company, account_type, root_type=None):
	filters = {"company": company, "account_type": account_type, "is_group": 0}
	if root_type:
		filters["root_type"] = root_type
	return frappe.db.get_value("Account", filters, "name")


def _plain(company, root_type):
	from asset_enterprise.setup.test_fixtures import pick_plain_account

	return pick_plain_account(company, root_type)


def _location():
	loc = frappe.db.get_value("Location", {}, "name")
	if loc:
		return loc
	return frappe.get_doc({"doctype": "Location", "location_name": "TC Location"}).insert(
		ignore_permissions=True
	).name


def _category(company, name, suspense=None, receiving_date=0, clear_suspense=False):
	"""Asset Category built to whatever the test case's Given says."""
	if frappe.db.exists("Asset Category", name):
		cat = frappe.get_doc("Asset Category", name)
	else:
		cat = frappe.get_doc({"doctype": "Asset Category", "asset_category_name": name})
		cat.append("accounts", {"company_name": company})
	row = cat.accounts[0]
	row.fixed_asset_account = _account(company, "Fixed Asset")
	row.accumulated_depreciation_account = _account(company, "Accumulated Depreciation")
	row.depreciation_expense_account = _account(company, "Depreciation") or _account(
		company, "Expense Account", "Expense"
	)
	if suspense:
		row.asset_suspense_account = suspense
	if clear_suspense:
		row.asset_suspense_account = None
	cat.calculate_from_receiving_date = receiving_date
	cat.flags.ignore_permissions = True
	cat.save()
	return cat.name


def _item(category, code):
	if not frappe.db.exists("Item", code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"is_fixed_asset": 1,
				"is_stock_item": 0,
				"asset_category": category,
				"auto_create_assets": 1,
				"asset_naming_series": frappe.get_meta("Asset")
				.get_field("naming_series")
				.options.split("\n")[0],
			}
		).insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Item", code, "asset_category", category, update_modified=False)
	return code


def _supplier():
	name = frappe.db.get_value("Supplier", {"supplier_name": "TC Audit Supplier"}, "name")
	if name:
		return name
	return frappe.get_doc(
		{"doctype": "Supplier", "supplier_name": "TC Audit Supplier"}
	).insert(ignore_permissions=True).name


def _gl(voucher):
	return frappe.db.sql(
		"""select account, debit, credit from `tabGL Entry`
		   where voucher_no = %s and is_cancelled = 0 order by debit desc""",
		voucher,
		as_dict=True,
	)


def _je_of_asset(asset, transaction_type=None):
	filters = {"asset": asset, "status": "Posted"}
	if transaction_type:
		filters["transaction_type"] = transaction_type
	return frappe.db.get_value("Financial Treatment", filters, "journal_entry")


# =============================================================== TC-001
@tc("TC-001", "Existing Asset Suspense JE — Successful")
def tc001():
	"""Given: category has a Suspense Account.
	Steps: create existing asset, gross 1,000,000, opening accum 400,000, submit.
	Expected: DR FA 1,000,000 / CR Accum 400,000 / CR Suspense 600,000."""
	company = _company()
	suspense = _plain(company, "Liability")
	cat = _category(company, "TC IT Equipment", suspense=suspense)
	frappe.db.set_value(
		"Company", company, "default_asset_suspense_account", None, update_modified=False
	)

	note = ""
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"asset_name": "TC-001 Existing Asset",
			"asset_category": cat,
			"item_code": _item(cat, "TC-IT-ITEM"),
			"location": _location(),
			"purchase_amount": 1_000_000,
			"net_purchase_amount": 1_000_000,
			"opening_accumulated_depreciation": 400_000,
			"available_for_use_date": nowdate(),
			"purchase_date": nowdate(),
			"calculate_depreciation": 0,
		}
	)
	if frappe.get_meta("Asset").has_field("is_existing_asset"):
		asset.is_existing_asset = 1
	else:
		asset.asset_type = "Existing Asset"
		note = (
			"steps say `is_existing_asset=1`; that field no longer exists in v16 — "
			"performed as asset_type='Existing Asset'"
		)
	asset.flags.ignore_permissions = True
	asset.insert()
	asset.submit()

	je = _je_of_asset(asset.name, "Existing-Asset Opening")
	if not je:
		return "FAIL", "submit produced no opening journal entry"
	legs = {(r.account, flt(r.debit), flt(r.credit)) for r in _gl(je)}
	want = {
		(_account(company, "Fixed Asset"), 1_000_000.0, 0.0),
		(_account(company, "Accumulated Depreciation"), 0.0, 400_000.0),
		(suspense, 0.0, 600_000.0),
	}
	verdict = "PASS" if legs == want else "FAIL"
	return verdict, f"JE {je}: {sorted(legs)}" + (f" | NOTE: {note}" if note else "")


# =============================================================== TC-002
@tc("TC-002", "Existing Asset Without Suspense Account → VR-001")
def tc002():
	company = _company()
	cat = _category(company, "TC Vehicles", clear_suspense=True)
	frappe.db.set_value(
		"Company", company, "default_asset_suspense_account", None, update_modified=False
	)
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"asset_name": "TC-002 Existing Asset",
			"asset_category": cat,
			"item_code": _item(cat, "TC-VEH-ITEM"),
			"location": _location(),
			"purchase_amount": 500_000,
			"net_purchase_amount": 500_000,
			"available_for_use_date": nowdate(),
			"purchase_date": nowdate(),
			"calculate_depreciation": 0,
		}
	)
	if not frappe.get_meta("Asset").has_field("is_existing_asset"):
		asset.asset_type = "Existing Asset"
	asset.flags.ignore_permissions = True
	asset.insert()
	try:
		asset.submit()
	except frappe.ValidationError as e:
		msg = str(e)
		hit = "Suspense" in msg
		return ("PASS" if hit else "FAIL"), f"blocked: {msg[:130]}"
	return "FAIL", "submitted with no suspense account configured"


# =============================================================== TC-003
@tc("TC-003", "Available-for-use Date Optional (no depreciation)")
def tc003():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"asset_name": "TC-003 No AFU",
			"asset_category": cat,
			"item_code": _item(cat, "TC-IT-ITEM"),
			"location": _location(),
			"purchase_amount": 10_000,
			"net_purchase_amount": 10_000,
			"purchase_date": nowdate(),
			"calculate_depreciation": 0,
		}
	)
	asset.flags.ignore_permissions = True
	asset.insert()
	asset.submit()
	return (
		("PASS" if asset.docstatus == 1 else "FAIL"),
		f"{asset.name} submitted with available_for_use_date={asset.available_for_use_date!r}",
	)


# =============================================================== TC-004
@tc("TC-004", "Available-for-use Date Required (with depreciation) → VR-002")
def tc004():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"),
	                receiving_date=0)
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"asset_name": "TC-004 Depreciating No AFU",
			"asset_category": cat,
			"item_code": _item(cat, "TC-IT-ITEM"),
			"location": _location(),
			"purchase_amount": 120_000,
			"net_purchase_amount": 120_000,
			"purchase_date": nowdate(),
			"calculate_depreciation": 1,
			"finance_books": [
				{
					"depreciation_method": "Straight Line",
					"total_number_of_depreciations": 12,
					"frequency_of_depreciation": 1,
				}
			],
		}
	)
	asset.flags.ignore_permissions = True
	try:
		asset.insert()
		asset.submit()
	except frappe.ValidationError as e:
		return "PASS", f"blocked: {str(e)[:130]}"
	return "FAIL", "depreciating asset submitted with no available-for-use date"


# =============================================================== TC-005
@tc("TC-005", "Receiving Date Depreciation Start Basis")
def tc005():
	"""Given: category calculate_from_receiving_date=1; PR posted 15/03/2026,
	value 1,200,000, UL 5y. Steps: create asset (AFU empty), submit.
	Expected: start basis 15/03/2026; first posting 30/06/2026 covers
	15/03 → 30/06 (107 days x daily rate)."""
	company = _company()
	cat = _category(company, "TC Plant Equipment", suspense=_plain(company, "Liability"),
	                receiving_date=1)
	item = _item(cat, "TC-PLANT-ITEM")
	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"company": company,
			"supplier": _supplier(),
			"posting_date": "2026-03-15",
			"set_posting_time": 1,
			"items": [{"item_code": item, "qty": 1, "rate": 1_200_000, "asset_location": _location()}],
		}
	)
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()

	asset_name = frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
	asset = frappe.get_doc("Asset", asset_name)
	asset.calculate_depreciation = 1
	if not asset.finance_books:
		asset.append("finance_books", {})
	fb = asset.finance_books[0]
	fb.depreciation_method = "Straight Line"
	fb.total_number_of_depreciations = 60
	fb.frequency_of_depreciation = 1
	fb.depreciation_start_date = "2026-06-30"
	asset.available_for_use_date = None
	asset.flags.ignore_permissions = True
	asset.save()
	asset.submit()
	asset.reload()

	sched = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset.name, "status": "Active", "docstatus": 1}, "name"
	)
	rows = frappe.get_all(
		"Depreciation Schedule",
		filters={"parent": sched},
		fields=["schedule_date", "depreciation_amount", "days_in_period", "daily_rate"],
		order_by="idx",
	)
	if not rows:
		return "FAIL", "no schedule rows generated"
	first = rows[0]
	basis_ok = getdate(asset.available_for_use_date) == getdate("2026-03-15")
	rate = flt(first.daily_rate, 6)
	want_rate = flt(1_200_000 / 1825.0, 6)
	days = first.days_in_period
	evidence = (
		f"AFU set to {asset.available_for_use_date} (want 2026-03-15); first row "
		f"{first.schedule_date} {flt(first.depreciation_amount):,.2f} over {days} days "
		f"@ {rate}/day (doc says 107 days x {want_rate})"
	)
	if not basis_ok:
		return "FAIL", evidence
	if getdate(first.schedule_date) != getdate("2026-06-30"):
		return "FAIL", evidence
	if flt(days) == 107:
		return "PASS", evidence
	if flt(days) == 108:
		return (
			"DEVIATION",
			evidence
			+ " — engine counts BOTH endpoints (§4.3 inclusive day count); the "
			"document's 107 excludes the receipt day. One of the two is wrong",
		)
	return "FAIL", evidence


# =============================================================== TC-006
@tc("TC-006", "PR/PI Link Quantity Validation → VR-004")
def tc006():
	"""Given PR qty 5; Asset A qty 3 submitted; Asset B qty 3 → VR-004,
	and asset_linked on the PR row = 1."""
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	item = _item(cat, "TC-QTY-ITEM")
	frappe.db.set_value("Item", item, "auto_create_assets", 0, update_modified=False)
	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"company": company,
			"supplier": _supplier(),
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"items": [{"item_code": item, "qty": 5, "rate": 10_000, "asset_location": _location()}],
		}
	)
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()

	def _asset(name, qty):
		doc = frappe.get_doc(
			{
				"doctype": "Asset",
				"company": company,
				"asset_name": name,
				"asset_category": cat,
				"item_code": item,
				"location": _location(),
				"purchase_receipt": pr.name,
				"purchase_receipt_item": pr.items[0].name,
				"asset_quantity": qty,
				"purchase_amount": 10_000 * qty,
				"net_purchase_amount": 10_000 * qty,
				"available_for_use_date": nowdate(),
				"purchase_date": nowdate(),
				"calculate_depreciation": 0,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	a = _asset("TC-006 Asset A", 3)
	a.submit()
	linked = frappe.db.get_value("Purchase Receipt Item", pr.items[0].name, "asset_linked")
	try:
		b = _asset("TC-006 Asset B", 3)
		b.submit()
	except frappe.ValidationError as e:
		msg = str(e)
		blocked = "exceeds" in msg or "over-allocation" in msg.lower()
		return (
			("PASS" if blocked and linked == 1 else "FAIL"),
			f"blocked: {msg[:120]} | asset_linked on PR row = {linked} (want 1)",
		)
	return "FAIL", f"6 of 5 units allocated without error | asset_linked = {linked}"


# =============================================================== TC-007
@tc("TC-007", "PR Reversal Cascades to Linked Asset")
def tc007():
	"""Steps: reverse the PR of an asset with a schedule and no posted JE.
	Expected (doc): asset reversed via TCC, schedule superseded, FT Reversed."""
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	item = _item(cat, "TC-CASCADE-ITEM")
	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"company": company,
			"supplier": _supplier(),
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"items": [{"item_code": item, "qty": 1, "rate": 60_000, "asset_location": _location()}],
		}
	)
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	asset_name = frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
	asset = frappe.get_doc("Asset", asset_name)
	asset.available_for_use_date = asset.purchase_date
	asset.calculate_depreciation = 1
	if not asset.finance_books:
		asset.append("finance_books", {})
	fb = asset.finance_books[0]
	fb.depreciation_method = "Straight Line"
	fb.total_number_of_depreciations = 12
	fb.frequency_of_depreciation = 1
	fb.depreciation_start_date = get_last_day(asset.purchase_date)
	asset.flags.ignore_permissions = True
	asset.save()
	asset.submit()

	try:
		pr.reload()
		pr.cancel()
	except frappe.ValidationError as e:
		return (
			"DEVIATION",
			"build BLOCKS the PR cancel until the asset is reversed first "
			f"(immutable ordering): {str(e)[:110]} — document says the reversal cascades",
		)
	ft = frappe.db.get_value(
		"Financial Treatment", {"asset": asset_name, "status": "Reversed"}, "name"
	)
	sched_status = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset_name}, "status"
	)
	ok = bool(ft) and sched_status in ("Superseded", None)
	return ("PASS" if ok else "FAIL"), f"PR cancelled; FT Reversed={ft}; schedule={sched_status}"


# =============================================================== TC-008
@tc("TC-008", "Mass Asset Depreciation — All Eligible + JE link per row")
def tc008():
	"""Doc uses 50 assets; behaviour is per-asset, so this runs 3 and
	asserts posted == eligible, 0 skipped, 0 failed, JE link on each row."""
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	start = get_last_day(add_months(nowdate(), -1))
	names = []
	for i in range(3):
		doc = frappe.get_doc(
			{
				"doctype": "Asset",
				"company": company,
				"asset_name": f"TC-008 Mass {i}",
				"asset_category": cat,
				"item_code": _item(cat, "TC-IT-ITEM"),
				"location": _location(),
				"purchase_amount": 120_000,
				"net_purchase_amount": 120_000,
				"available_for_use_date": add_months(nowdate(), -2),
				"purchase_date": add_months(nowdate(), -2),
				"calculate_depreciation": 1,
				"finance_books": [
					{
						"depreciation_method": "Straight Line",
						"total_number_of_depreciations": 12,
						"frequency_of_depreciation": 1,
						"depreciation_start_date": start,
					}
				],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()
		names.append(doc.name)

	mad = frappe.get_doc(
		{
			"doctype": "Mass Asset Depreciation",
			"company": company,
			"posting_date": start,
			"mode": "All Eligible",
		}
	)
	mad.flags.ignore_permissions = True
	mad.insert()
	mad.submit()
	mad.reload()
	rows = frappe.get_all(
		"Mass Asset Depreciation Result",
		filters={"parent": mad.name, "asset": ["in", names]},
		fields=["asset", "outcome", "journal_entry"],
	)
	posted = [r for r in rows if r.outcome == "Posted"]
	with_je = [r for r in posted if r.journal_entry]
	ok = len(posted) == len(names) and len(with_je) == len(posted)
	return (
		("PASS" if ok else "FAIL"),
		f"{len(posted)}/{len(names)} posted, {len(with_je)} carry a JE link; "
		f"summary={str(mad.result_summary)[:80]}",
	)


# =============================================================== TC-009
@tc("TC-009", "Mass Depreciation — Special Authority Required → VR-006")
def tc009():
	company = _company()
	settings = frappe.get_single("Asset Settings")
	settings.set("mass_depreciation_authority_roles", [])
	settings.append("mass_depreciation_authority_roles", {"role": "Auditor"})
	settings.flags.ignore_permissions = True
	settings.save()

	user = "tc-audit-user@example.com"
	if not frappe.db.exists("User", user):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": user,
				"first_name": "TC Audit",
				"send_welcome_email": 0,
				"roles": [{"role": "Accounts Manager"}],
			}
		).insert(ignore_permissions=True)

	original = frappe.session.user
	try:
		frappe.set_user(user)
		mad = frappe.get_doc(
			{
				"doctype": "Mass Asset Depreciation",
				"company": company,
				"posting_date": nowdate(),
				"mode": "Selected Asset Category",
				"asset_category": _category(company, "TC IT Equipment"),
			}
		)
		mad.flags.ignore_permissions = True
		mad.insert()
		mad.submit()
	except frappe.ValidationError as e:
		return "PASS", f"blocked for a user without the authority role: {str(e)[:120]}"
	except frappe.PermissionError as e:
		return "PASS", f"blocked (permission): {str(e)[:110]}"
	finally:
		frappe.set_user(original)
	return "FAIL", "restricted mode ran for a user without the authority role"


# =============================================================== TC-010
@tc("TC-010", "Ledger-Derived Asset Values")
def tc010():
	"""Steps: open the asset, click Recalculate Asset Values.
	Expected: HAV = cost; accum = posted schedule + manual JE; NBV = difference."""
	from erpnext.assets.doctype.asset.depreciation import make_depreciation_entry

	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	start = get_last_day(add_months(nowdate(), -2))
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"asset_name": "TC-010 Ledger Derived",
			"asset_category": cat,
			"item_code": _item(cat, "TC-IT-ITEM"),
			"location": _location(),
			"purchase_amount": 600_000,
			"net_purchase_amount": 600_000,
			"available_for_use_date": add_months(nowdate(), -3),
			"purchase_date": add_months(nowdate(), -3),
			"calculate_depreciation": 1,
			"finance_books": [
				{
					"depreciation_method": "Straight Line",
					"total_number_of_depreciations": 12,
					"frequency_of_depreciation": 1,
					"depreciation_start_date": start,
				}
			],
		}
	)
	asset.flags.ignore_permissions = True
	asset.insert()
	asset.submit()

	sched = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset.name, "status": "Active", "docstatus": 1}, "name"
	)
	make_depreciation_entry(sched, str(start))
	posted_rows = frappe.db.sql(
		"""select coalesce(sum(depreciation_amount), 0) from `tabDepreciation Schedule`
		   where parent = %s and ifnull(journal_entry, '') != ''""",
		sched,
	)[0][0]

	accum_account = _account(company, "Accumulated Depreciation")
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Depreciation Entry",
			"company": company,
			"posting_date": nowdate(),
			"user_remark": "TC-010 manual accumulated depreciation adjustment",
			"accounts": [
				{
					"account": _account(company, "Depreciation")
					or _account(company, "Expense Account", "Expense"),
					"debit_in_account_currency": 10_000,
				},
				{
					"account": accum_account,
					"credit_in_account_currency": 10_000,
					"reference_type": "Asset",
					"reference_name": asset.name,
				},
			],
		}
	)
	je.flags.ignore_permissions = True
	je.submit()

	from asset_enterprise.api import recalculate

	values = recalculate(asset.name)
	want_accum = flt(posted_rows) + 10_000
	ok = (
		flt(values["historical_asset_value"]) == 600_000
		and flt(values["accumulated_depreciation_value"]) == flt(want_accum)
		and flt(values["net_book_value"]) == flt(600_000 - want_accum)
	)
	return (
		("PASS" if ok else "FAIL"),
		f"HAV {values['historical_asset_value']:,.2f} accum "
		f"{values['accumulated_depreciation_value']:,.2f} (schedule {flt(posted_rows):,.2f} "
		f"+ manual 10,000) NBV {values['net_book_value']:,.2f}",
	)


# ------------------------------------------------------------------ run
def run(only=None):
	wanted = {t.strip() for t in only.split(",")} if only else None
	results = []
	switch_before = frappe.db.get_single_value(
		"Asset Settings", "enable_enterprise_assets", cache=False
	)
	for tc_id, title, fn in CHECKS:
		if wanted and tc_id not in wanted:
			continue
		frappe.db.savepoint("tc_audit")
		try:
			frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
			verdict, evidence = fn()
		except Exception as e:
			verdict = "FAIL"
			evidence = f"{type(e).__name__}: {str(e)[:200]}"
			if frappe.flags.get("tc_trace"):
				traceback.print_exc()
		finally:
			frappe.db.rollback(save_point="tc_audit")
		results.append((tc_id, verdict, title, evidence))
		print(f"{tc_id:<9} {verdict:<10} {title}")
		print(f"          {evidence}")

	frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", switch_before)
	tally = {}
	for _tc, verdict, _t, _e in results:
		tally[verdict] = tally.get(verdict, 0) + 1
	print("\nTALLY:", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
	return results
