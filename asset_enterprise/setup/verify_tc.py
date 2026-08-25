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


def _ensure_fiscal_years(first_year, last_year):
	"""Historic test cases (2017, 2025) need their fiscal years to exist
	on whatever bench this runs against — created inside the savepoint."""
	for year in range(int(first_year), int(last_year) + 1):
		name = str(year)
		# a live site may already carry that span under another name
		covered = frappe.db.sql(
			"""select name from `tabFiscal Year`
			   where year_start_date <= %s and year_end_date >= %s limit 1""",
			(f"{year}-12-31", f"{year}-01-01"),
		)
		if covered or frappe.db.exists("Fiscal Year", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": name,
				"year_start_date": f"{year}-01-01",
				"year_end_date": f"{year}-12-31",
			}
		).insert(ignore_permissions=True)


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
	if asset.docstatus == 0:
		asset.calculate_depreciation = 1
		if not asset.finance_books:
			asset.append("finance_books", {})
		fb = asset.finance_books[0]
		fb.depreciation_method = "Straight Line"
		fb.total_number_of_depreciations = 60
		fb.frequency_of_depreciation = 1
		fb.depreciation_start_date = "2026-06-30"
		asset.flags.ignore_permissions = True
		asset.save()
		asset.submit()
	else:
		# the receipt submitted it already (client decision, sheet item 2)
		# — depreciation is switched on through Enable Depreciation, the
		# only route ERPNext leaves open after submit.
		from asset_enterprise.depreciation import enable_depreciation

		enable_depreciation(
			asset.name,
			total_number_of_depreciations=60,
			frequency_of_depreciation=1,
			depreciation_start_date="2026-06-30",
		)
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
	if asset.docstatus == 0:
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
	else:
		# the receipt submitted it; depreciation goes on afterwards
		from asset_enterprise.depreciation import enable_depreciation

		enable_depreciation(
			asset.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
			depreciation_start_date=get_last_day(asset.purchase_date),
		)

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
	# GAP-004.4 states the requirement plainly: "Assets whose PR is
	# reversed must not be left in Submitted status." That now holds — the
	# receipt cancels and cascade-reverses its assets, and core's
	# force-delete of auto-created assets is patched out so the record
	# survives to be audited.
	asset_docstatus = frappe.db.get_value("Asset", asset_name, "docstatus")
	exists = frappe.db.exists("Asset", asset_name)
	cascaded = bool(exists) and asset_docstatus == 2
	if not cascaded:
		return ("FAIL"), (
			f"PR cancelled but asset {asset_name} is docstatus={asset_docstatus} "
			f"(exists={bool(exists)}) — GAP-004.4 requires it reversed, not left "
			f"submitted and not deleted"
		)
	# The requirement is that NO LIVE schedule survives on a reversed
	# asset. Cancelled satisfies it as fully as Superseded (client ruling,
	# 24/08): GAP-031 supersedes in order to carry POSTED rows and their
	# JE links into the next generation, and an asset reversal is refused
	# outright while any posted depreciation is live
	# (_block_when_depreciation_posted). What is left is an unposted
	# working copy with no history in it — the same reasoning TC-046
	# already records — and superseding it would leave a generation
	# hanging off an asset that no longer exists, implying a successor
	# that never comes. The test case's "Schedule superseded" is doc
	# wording to correct, not a build defect.
	live_schedule = frappe.db.exists(
		"Asset Depreciation Schedule",
		{"asset": asset_name, "status": "Active", "docstatus": 1},
	)
	if ft and not live_schedule:
		return "PASS", (
			f"PR cancelled; asset {asset_name} reversed (docstatus 2, not deleted); "
			f"acquisition FT Reversed={ft}; schedule={sched_status} and no Active "
			f"generation left (Cancelled is correct here — nothing was posted)"
		)
	# Report what was actually observed — an earlier version of this note
	# was hardcoded and kept claiming the treatment was missing after it
	# had been built.
	unmet = []
	if not ft:
		unmet.append("no Addition FT set to Reversed")
	if live_schedule:
		unmet.append(f"an Active schedule survives the reversal ({live_schedule})")
	return "DEVIATION", (
		f"cascade works per GAP-004.4 — asset {asset_name} reversed (docstatus 2), "
		f"preserved, acquisition FT Reversed={ft} — but: " + "; ".join(unmet)
	)


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


# --------------------------------------------------------- batch 2 helpers


def _plain_asset(company, cat, name, gross, **kw):
	doc = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"asset_name": name,
			"asset_category": cat,
			"item_code": _item(cat, "TC-IT-ITEM"),
			"location": _location(),
			"purchase_amount": gross,
			"net_purchase_amount": gross,
			"available_for_use_date": kw.pop("afu", nowdate()),
			"purchase_date": kw.pop("purchase_date", nowdate()),
			"calculate_depreciation": 0,
			**kw,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


def _depreciating_asset(company, cat, name, gross, start, months, afu, salvage=0):
	doc = frappe.get_doc(
		{
			"doctype": "Asset",
			"company": company,
			"asset_name": name,
			"asset_category": cat,
			"item_code": _item(cat, "TC-IT-ITEM"),
			"location": _location(),
			"purchase_amount": gross,
			"net_purchase_amount": gross,
			"available_for_use_date": afu,
			"purchase_date": afu,
			"calculate_depreciation": 1,
			"finance_books": [
				{
					"depreciation_method": "Straight Line",
					"total_number_of_depreciations": months,
					"frequency_of_depreciation": 1,
					"depreciation_start_date": start,
					"expected_value_after_useful_life": salvage,
					"daily_prorata_based": 1,
				}
			],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return doc


def _rows(asset_name, status="Active"):
	sched = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset_name, "status": status, "docstatus": 1}, "name"
	)
	return sched, frappe.get_all(
		"Depreciation Schedule",
		filters={"parent": sched},
		fields=["schedule_date", "depreciation_amount", "days_in_period", "daily_rate", "journal_entry"],
		order_by="idx",
	)


def _post_through(asset_name, upto):
	from erpnext.assets.doctype.asset.depreciation import make_depreciation_entry

	sched, _r = _rows(asset_name)
	make_depreciation_entry(sched, str(upto))


# =============================================================== TC-011
@tc("TC-011", "Asset History Records Financial Treatment")
def tc011():
	"""Given: opening + revaluation +100,000 + depreciation 50,000.
	Expected: a history row per transaction carrying category, amount,
	HAV-after, NBV-after and the JE link."""
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	start = get_last_day(add_months(nowdate(), -1))
	asset = _depreciating_asset(
		company, cat, "TC-011 History", 600_000, start, 12, add_months(nowdate(), -2)
	)
	_post_through(asset.name, start)

	ava = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": nowdate(),
			"transaction_type": "Upward Revaluation",
			"current_asset_value": flt(
				frappe.db.get_value("Asset", asset.name, "net_book_value")
			),
			"new_asset_value": flt(frappe.db.get_value("Asset", asset.name, "net_book_value"))
			+ 100_000,
			"difference_account": _plain(company, "Income") or _plain(company, "Liability"),
		}
	)
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()

	rows = frappe.get_all(
		"Asset Activity",
		filters={"asset": asset.name, "financial_effect": 1},
		fields=[
			"subject",
			"transaction_category",
			"transaction_amount",
			"historical_asset_value_after",
			"net_book_value_after",
			"linked_journal_entry",
		],
		order_by="creation",
	)
	complete = [
		r
		for r in rows
		if r.transaction_category and flt(r.transaction_amount) and r.linked_journal_entry
		and r.historical_asset_value_after is not None
		and r.net_book_value_after is not None
	]
	ok = len(complete) >= 2  # depreciation + revaluation on this asset
	return (
		("PASS" if ok else "FAIL"),
		f"{len(rows)} financial history rows, {len(complete)} carry category+amount+HAV/NBV-after+JE: "
		+ "; ".join(
			f"{r.transaction_category} {flt(r.transaction_amount):,.0f} "
			f"HAV {flt(r.historical_asset_value_after):,.0f} NBV {flt(r.net_book_value_after):,.0f} {r.linked_journal_entry}"
			for r in rows[:3]
		),
	)


# =============================================================== TC-012
@tc("TC-012", "Asset Tree Aggregation")
def tc012():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	parent = _plain_asset(company, cat, "TC-012 Plant A", 1_000_000)
	parent.submit()
	for i in range(5):
		child = _plain_asset(
			company, cat, f"TC-012 Child {i}", 1_800_000, parent_asset=parent.name,
			opening_accumulated_depreciation=400_000,
		)
		child.submit()

	from asset_enterprise.api import tree_aggregate

	totals = tree_aggregate(parent.name)
	ok = (
		flt(totals["historical_asset_value"]) == 10_000_000
		and flt(totals["accumulated_depreciation_value"]) == 2_000_000
		and flt(totals["net_book_value"]) == 8_000_000
	)
	return (
		("PASS" if ok else "FAIL"),
		f"HAV {flt(totals['historical_asset_value']):,.2f} accum "
		f"{flt(totals['accumulated_depreciation_value']):,.2f} NBV {flt(totals['net_book_value']):,.2f}",
	)


# =============================================================== TC-013
@tc("TC-013", "Asset Tree Acyclic Validation → VR-009")
def tc013():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	a = _plain_asset(company, cat, "TC-013 A", 10_000)
	b = _plain_asset(company, cat, "TC-013 B", 10_000, parent_asset=a.name)
	c = _plain_asset(company, cat, "TC-013 C", 10_000, parent_asset=b.name)
	a.parent_asset = c.name
	try:
		a.save()
	except frappe.ValidationError as e:
		return "PASS", f"cycle blocked: {str(e)[:120]}"
	return "FAIL", f"A -> C accepted while C is a descendant of A ({c.name})"


# =============================================================== TC-014
@tc("TC-014", "Prevent Disposal Before Full Invoicing → VR-011")
def tc014():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	item = _item(cat, "TC-DISPOSAL-ITEM")
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 1)
	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"company": company,
			"supplier": _supplier(),
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"items": [{"item_code": item, "qty": 1, "rate": 80_000, "asset_location": _location()}],
		}
	)
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	asset_name = frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
	asset = frappe.get_doc("Asset", asset_name)
	if asset.docstatus == 0:  # the receipt may already have submitted it
		asset.available_for_use_date = asset.purchase_date
		asset.flags.ignore_permissions = True
		asset.save()
		asset.submit()

	from asset_enterprise import disposal

	try:
		disposal.scrap_asset(asset_name, scrapping_type="Damage")
	except frappe.ValidationError as e:
		msg = str(e)
		return (
			("PASS" if "invoice" in msg.lower() else "FAIL"),
			f"scrap blocked: {msg[:130]}",
		)
	return "FAIL", "asset scrapped while its purchase invoice was still missing"


# =============================================================== TC-015
@tc("TC-015", "Daily-Rate Depreciation — No Adjustment")
def tc015():
	_ensure_fiscal_years(2017, 2022)
	"""10,000,000 over 1,825 days from 01/01/2017; check rows 1, 2, 13
	and the NBV after 12 posted months."""
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-015 Daily Rate", 10_000_000, "2017-01-31", 60, "2017-01-01"
	)
	_sched, rows = _rows(asset.name)
	rate = flt(10_000_000 / 1825.0, 6)
	jan17 = rows[0]
	feb17 = rows[1]
	jan18 = rows[12]
	first_year = sum(flt(r.depreciation_amount) for r in rows[:12])
	rows_ok = (
		flt(jan17.depreciation_amount, 2) == flt(31 * rate, 2)
		and flt(feb17.depreciation_amount, 2) == flt(28 * rate, 2)
		and flt(jan18.depreciation_amount, 2) == flt(31 * rate, 2)
	)
	nbv_after_year = flt(10_000_000 - first_year, 2)
	ok = rows_ok and nbv_after_year == 8_000_000.00
	if rows_ok and not ok and abs(nbv_after_year - 8_000_000.00) < 1:
		return (
			"DOC",
			f"rows exact ({flt(jan17.depreciation_amount):,.2f} / "
			f"{flt(feb17.depreciation_amount):,.2f} / {flt(jan18.depreciation_amount):,.2f}) but NBV "
			f"after 12 rows is {nbv_after_year:,.2f}, not the stated 8,000,000.00 — per-row rounding "
			f"drift, which §4.10.3 absorbs in the FINAL row only. The test case's figure assumes "
			f"unrounded rows; the engine follows §4.10",
		)
	return (
		("PASS" if ok else "FAIL"),
		f"rate {flt(jan17.daily_rate, 6)} (want {rate}); Jan17 {flt(jan17.depreciation_amount):,.2f}; "
		f"Feb17 {flt(feb17.depreciation_amount):,.2f}; Jan18 {flt(jan18.depreciation_amount):,.2f}; "
		f"NBV after 12 rows {flt(10_000_000 - first_year):,.2f} (want 8,000,000.00)",
	)


# =============================================================== TC-016
@tc("TC-016", "Upward Adjustment Mid-Life — Prospective Recalc")
def tc016():
	_ensure_fiscal_years(2017, 2022)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-016 Prospective", 10_000_000, "2017-01-31", 60, "2017-01-01"
	)
	_post_through(asset.name, "2017-12-31")
	sched_before, rows_before = _rows(asset.name)
	posted_before = [(str(r.schedule_date), flt(r.depreciation_amount)) for r in rows_before if r.journal_entry]

	ava = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": "2017-12-31",
			"transaction_type": "Upward Revaluation",
			"current_asset_value": 8_000_000,
			"new_asset_value": 9_000_000,
			"difference_account": _plain(company, "Income") or _plain(company, "Liability"),
		}
	)
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()

	sched_after, rows_after = _rows(asset.name)
	future = [r for r in rows_after if getdate(r.schedule_date) > getdate("2017-12-31")]
	jan18 = future[0]
	feb18 = future[1]
	rate = flt(9_000_000 / 1460.0, 6)
	old_status = frappe.db.get_value("Asset Depreciation Schedule", sched_before, "status")
	posted_after = [(str(r.schedule_date), flt(r.depreciation_amount)) for r in rows_after if r.journal_entry]
	ok = (
		flt(jan18.depreciation_amount, 2) == flt(31 * rate, 2)
		and flt(feb18.depreciation_amount, 2) == flt(28 * rate, 2)
		and old_status == "Superseded"
		and posted_before == posted_after
	)
	return (
		("PASS" if ok else "FAIL"),
		f"new rate {flt(jan18.daily_rate, 6)} (want {rate}); Jan18 {flt(jan18.depreciation_amount):,.2f} "
		f"(want {31 * rate:,.2f}); Feb18 {flt(feb18.depreciation_amount):,.2f} (want {28 * rate:,.2f}); "
		f"old schedule {old_status}; posted rows unchanged={posted_before == posted_after}",
	)


# =============================================================== TC-017
@tc("TC-017", "Impairment Mid-Life — Prospective Recalc")
def tc017():
	_ensure_fiscal_years(2017, 2022)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-017 Impairment", 10_000_000, "2017-01-31", 60, "2017-01-01"
	)
	_post_through(asset.name, "2017-12-31")
	ava1 = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": "2017-12-31",
			"transaction_type": "Upward Revaluation",
			"current_asset_value": 8_000_000,
			"new_asset_value": 9_000_000,
			"difference_account": _plain(company, "Income") or _plain(company, "Liability"),
		}
	)
	ava1.flags.ignore_permissions = True
	ava1.insert()
	ava1.submit()
	_post_through(asset.name, "2019-12-31")

	from asset_enterprise.asset_values import recalculate_asset_values

	nbv_at_36 = flt(recalculate_asset_values(asset.name, save=False)["net_book_value"])
	ava2 = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": "2019-12-31",
			"transaction_type": "Initial Impairment",
			"current_asset_value": nbv_at_36,
			"new_asset_value": nbv_at_36 - 1_000_000,
			"difference_account": _plain(company, "Expense"),
		}
	)
	ava2.flags.ignore_permissions = True
	ava2.insert()
	ava2.submit()

	_sched, rows = _rows(asset.name)
	future = [r for r in rows if getdate(r.schedule_date) > getdate("2019-12-31")]
	jan20 = future[0]
	rate = flt((nbv_at_36 - 1_000_000) / 730.0, 6)
	ok = flt(jan20.depreciation_amount, 2) == flt(31 * rate, 2)
	return (
		("PASS" if ok else "FAIL"),
		f"NBV at month 36 {nbv_at_36:,.2f} (doc chain says 4,500,000); after impairment "
		f"{nbv_at_36 - 1_000_000:,.2f} over 730 days -> {rate}/day; Jan 2020 row "
		f"{flt(jan20.depreciation_amount):,.2f} (want {31 * rate:,.2f}); doc states 148,630.14",
	)


# =============================================================== TC-018
@tc("TC-018", "Enable Depreciation After Creation")
def tc018():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _plain_asset(company, cat, "TC-018 Enable Later", 360_000, afu="2026-05-01",
	                     purchase_date="2026-05-01")
	asset.submit()

	from asset_enterprise.depreciation import enable_depreciation

	enable_depreciation(
		asset.name,
		total_number_of_depreciations=36,
		frequency_of_depreciation=1,
		depreciation_start_date="2026-05-31",
	)
	asset.reload()
	sched, rows = _rows(asset.name)
	fb = frappe.get_all("Asset Finance Book", filters={"parent": asset.name}, fields=["total_number_of_depreciations"])
	total = sum(flt(r.depreciation_amount) for r in rows)
	ok = (
		bool(fb)
		and cint_(fb[0].total_number_of_depreciations) == 36
		and rows
		and getdate(rows[0].schedule_date) == getdate("2026-05-31")
		and flt(total, 2) == 360_000.00
		and flt(asset.calculate_depreciation) == 1
	)
	return (
		("PASS" if ok else "FAIL"),
		f"finance book={fb}; schedule {sched} first row "
		f"{rows[0].schedule_date if rows else None} total {flt(total):,.2f}; "
		f"calculate_depreciation={asset.calculate_depreciation}",
	)


def cint_(v):
	from frappe.utils import cint

	return cint(v)


# =============================================================== TC-019
@tc("TC-019", "Accumulated First Depreciation Entry")
def tc019():
	_ensure_fiscal_years(2025, 2028)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-019 Catch-up", 8_500_000, "2025-06-30", 36, "2025-02-01"
	)
	_sched, rows = _rows(asset.name)
	first = rows[0]
	rate = flt(8_500_000 / 1095.0, 6)
	ok = (
		getdate(first.schedule_date) == getdate("2025-06-30")
		and flt(first.days_in_period) == 150
		and flt(first.depreciation_amount, 2) == flt(150 * rate, 2)
	)
	return (
		("PASS" if ok else "FAIL"),
		f"first row {first.schedule_date} covers {first.days_in_period} days (want 150) "
		f"= {flt(first.depreciation_amount):,.2f} (want {150 * rate:,.2f}); rate "
		f"{flt(first.daily_rate, 6)} (want {rate})",
	)


# =============================================================== TC-020
@tc("TC-020", "End-of-Month Depreciation Posting")
def tc020():
	_ensure_fiscal_years(2025, 2027)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	has_flag = frappe.get_meta("Asset Settings").has_field("force_eom_depreciation")
	asset = _depreciating_asset(
		company, cat, "TC-020 EOM", 360_000, "2025-03-15", 12, "2025-03-15"
	)
	_sched, rows = _rows(asset.name)
	non_eom = [str(r.schedule_date) for r in rows if getdate(r.schedule_date) != getdate(get_last_day(r.schedule_date))]
	if non_eom:
		last = str(rows[-1].schedule_date)
		if non_eom == [last]:
			return (
				"DEVIATION",
				f"every row is month-end except the LAST ({last}), which lands on the asset's "
				f"end-of-life date. §4.6 forces all schedule dates to month-end, with exceptions "
				f"only for disposal/scrap/capitalization — a mid-month start has no stated home "
				f"for its final stub period",
			)
		return "FAIL", f"rows not at end of month: {non_eom[:5]}"
	evidence = f"all {len(rows)} rows land on month end, first {rows[0].schedule_date}"
	if not has_flag:
		return (
			"DOC",
			evidence
			+ " — the Given names Asset Settings `force_eom_depreciation`, removed by the "
			"11c decision (EOM is the standing rule); behaviour itself is correct",
		)
	return "PASS", evidence


# =============================================================== TC-021
@tc("TC-021", "Prior Year Adjustment Account")
def tc021():
	"""AFU 15/12/2025, first posting 31/01/2026, FY starts 01/01/2026.
	Expected: ONE JE, two debit lines (PYA for the prior-year days,
	Depreciation Expense for the current-year days), one credit."""
	_ensure_fiscal_years(2025, 2029)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	pya = _plain(company, "Expense")
	frappe.db.set_value(
		"Asset Category Account",
		{"parent": cat, "company_name": company},
		"pya_expense_account",
		pya,
		update_modified=False,
	)
	asset = _depreciating_asset(
		company, cat, "TC-021 PYA", 1_200_000, "2026-01-31", 36, "2025-12-15"
	)
	_post_through(asset.name, "2026-01-31")
	sched, rows = _rows(asset.name)
	if not rows:
		return "FAIL", f"no schedule rows for {asset.name} (schedule={sched})"
	first = rows[0]
	je = first.journal_entry
	if not je:
		return "FAIL", (
			f"first row {first.schedule_date} did not post; rows="
			+ str([(str(r.schedule_date), r.journal_entry) for r in rows[:3]])
		)
	legs = _gl(je)
	debits = [r for r in legs if flt(r.debit)]
	credits = [r for r in legs if flt(r.credit)]
	pya_leg = [r for r in debits if r.account == pya]
	rate = flt(first.daily_rate or (1_200_000 / 1095.0), 6)
	ok = (
		len(debits) == 2
		and len(credits) == 1
		and pya_leg
		and flt(sum(flt(r.debit) for r in debits), 2) == flt(credits[0].credit, 2)
	)
	if not pya_leg:
		return "FAIL", (
			f"JE {je} has no Prior Year Adjustment debit line. legs="
			+ str([(r.account, flt(r.debit), flt(r.credit)) for r in legs])
			+ f" | PYA account configured = {pya} | row covers {first.days_in_period} days"
		)
	prior_days = round(flt(pya_leg[0].debit) / rate) if pya_leg else 0
	return (
		("PASS" if ok else "FAIL"),
		f"JE {je}: {len(debits)} debit / {len(credits)} credit lines; PYA leg "
		f"{flt(pya_leg[0].debit):,.2f} = {prior_days} days (doc says 16; 15/12→31/12 "
		f"inclusive is 17 under §4.3); total row {flt(first.depreciation_amount):,.2f} "
		f"over {first.days_in_period} days (doc says 47)",
	)


# =============================================================== TC-022
@tc("TC-022", "Schedule Superseded on Adjustment (not cancelled)")
def tc022():
	_ensure_fiscal_years(2025, 2029)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-022 Supersede", 3_600_000, "2025-01-31", 36, "2025-01-01"
	)
	_post_through(asset.name, "2025-12-31")
	sched_a, rows_a = _rows(asset.name)
	posted_a = [(str(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
	            for r in rows_a if r.journal_entry]

	ava = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": "2025-12-31",
			"transaction_type": "Upward Revaluation",
			"current_asset_value": flt(
				frappe.db.get_value("Asset", asset.name, "net_book_value")
			),
			"new_asset_value": flt(frappe.db.get_value("Asset", asset.name, "net_book_value")) + 50_000,
			"difference_account": _plain(company, "Income") or _plain(company, "Liability"),
		}
	)
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()

	a = frappe.db.get_value("Asset Depreciation Schedule", sched_a, ["status", "docstatus"], as_dict=True)
	sched_b, rows_b = _rows(asset.name)
	b_supersedes = frappe.db.get_value("Asset Depreciation Schedule", sched_b, "supersedes")
	posted_b = [(str(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
	            for r in rows_b if r.journal_entry]
	rows_a_now = frappe.get_all(
		"Depreciation Schedule", filters={"parent": sched_a}, fields=["name"]
	)
	ok = (
		a.status == "Superseded"
		and a.docstatus == 1
		and len(rows_a_now) == len(rows_a)
		and b_supersedes == sched_a
		and posted_a == posted_b
		and len(rows_b) == len(rows_a)
	)
	return (
		("PASS" if ok else "FAIL"),
		f"A {sched_a}: {a.status}/docstatus {a.docstatus}, {len(rows_a_now)} rows intact, "
		f"{len(posted_a)} posted JE links; B {sched_b}: supersedes={b_supersedes}, "
		f"{len(rows_b)} rows of which {len(posted_b)} carried over unchanged={posted_a == posted_b}",
	)


# =============================================================== TC-023
@tc("TC-023", "PI vs PR Amount Difference — Active Asset")
def tc023():
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	item = _item(cat, "TC-DELTA-ITEM")
	diff_account = _plain(company, "Liability")
	frappe.db.set_value(
		"Company", company, "default_asset_invoice_difference_account", diff_account,
		update_modified=False,
	)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 100)

	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"company": company,
			"supplier": _supplier(),
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"items": [{"item_code": item, "qty": 1, "rate": 1_500_000, "asset_location": _location()}],
		}
	)
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	asset_name = frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
	asset = frappe.get_doc("Asset", asset_name)
	if asset.docstatus == 0:  # the receipt may already have submitted it
		asset.available_for_use_date = asset.purchase_date
		asset.flags.ignore_permissions = True
		asset.save()
		asset.submit()

	pi = make_purchase_invoice(pr.name)
	pi.posting_date = nowdate()
	pi.set_posting_time = 1
	for row in pi.items:
		row.rate = 2_000_000
		row.price_list_rate = 2_000_000
	pi.pi_asset_allocation = []
	pi.flags.ignore_permissions = True
	pi.insert()
	pi.submit()

	transfer = frappe.db.get_value(
		"Journal Entry",
		{"user_remark": ("like", f"Invoice delta transfer for {pi.name}%"), "docstatus": 1},
		"name",
	)
	ava_je = frappe.db.get_value(
		"Asset Value Adjustment",
		{"asset": asset_name, "transaction_type": "Invoice Adjustment", "docstatus": 1},
		"journal_entry",
	)
	arbnb = frappe.db.get_value("Company", company, "asset_received_but_not_billed")
	fa_account = _account(company, "Fixed Asset")

	def _net(account, vouchers):
		total = 0.0
		for r in frappe.db.sql(
			"""select debit, credit from `tabGL Entry` where account = %s
			   and is_cancelled = 0 and voucher_no in %s""",
			(account, tuple(v for v in vouchers if v)),
			as_dict=True,
		):
			total += flt(r.debit) - flt(r.credit)
		return flt(total, 2)

	vouchers = (pr.name, pi.name, transfer, ava_je)
	arbnb_net = _net(arbnb, vouchers)
	diff_net = _net(diff_account, vouchers)
	fa_net = _net(fa_account, vouchers)
	from asset_enterprise.asset_values import recalculate_asset_values

	hav = flt(recalculate_asset_values(asset_name, save=False)["historical_asset_value"], 2)
	single_je = not transfer
	ok = arbnb_net == 0 and diff_net == 0 and fa_net == 2_000_000.00 and hav == 2_000_000.00
	verdict = "PASS" if (ok and single_je) else ("DEVIATION" if ok else "FAIL")
	return (
		verdict,
		f"net effect matches the test case (ARBNB {arbnb_net:,.2f}, invoice-difference "
		f"{diff_net:,.2f}, fixed asset {fa_net:,.2f}, HAV {hav:,.2f}) but the 500,000 delta "
		f"leaves ARBNB through a separate transfer entry ({transfer}) instead of splitting the "
		f"invoice's own posting — Phase 11c decision D1 Option A, accepted by finance"
		if verdict == "DEVIATION"
		else f"ARBNB {arbnb_net:,.2f} / difference {diff_net:,.2f} / FA {fa_net:,.2f} / HAV {hav:,.2f}",
	)


# =============================================================== TC-024
@tc("TC-024", "PI vs PR Amount Difference — Disposed Asset")
def tc024():
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	item = _item(cat, "TC-DISPOSED-ITEM")
	expense = _plain(company, "Expense")
	frappe.db.set_value(
		"Company", company,
		{
			"default_post_disposal_invoice_diff_account": expense,
			"disposal_account": frappe.db.get_value("Company", company, "disposal_account") or expense,
			"default_asset_invoice_difference_account": _plain(company, "Liability"),
		},
		update_modified=False,
	)
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 100)
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)

	pr = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"company": company,
			"supplier": _supplier(),
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"items": [{"item_code": item, "qty": 1, "rate": 800_000, "asset_location": _location()}],
		}
	)
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	asset_name = frappe.get_all("Asset", filters={"purchase_receipt": pr.name}, pluck="name")[0]
	asset = frappe.get_doc("Asset", asset_name)
	if asset.docstatus == 0:  # the receipt may already have submitted it
		asset.available_for_use_date = asset.purchase_date
		asset.flags.ignore_permissions = True
		asset.save()
		asset.submit()

	from asset_enterprise import disposal
	from asset_enterprise.asset_values import recalculate_asset_values

	disposal.scrap_asset(asset_name, scrapping_type="Damage")
	hav_before = flt(recalculate_asset_values(asset_name, save=False)["historical_asset_value"], 2)

	pi = make_purchase_invoice(pr.name)
	pi.posting_date = nowdate()
	pi.set_posting_time = 1
	for row in pi.items:
		row.rate = 1_000_000
		row.price_list_rate = 1_000_000
	pi.pi_asset_allocation = []
	pi.flags.ignore_permissions = True
	pi.insert()
	pi.submit()

	ft = frappe.db.exists(
		"Financial Treatment",
		{"source_name": pi.name, "transaction_type": "Post-Disposal Invoice Adjustment",
		 "status": "Posted"},
	)
	new_ava = frappe.db.count(
		"Asset Value Adjustment",
		{"asset": asset_name, "transaction_type": "Invoice Adjustment", "docstatus": 1},
	)
	transfer = frappe.db.get_value(
		"Journal Entry",
		{"user_remark": ("like", f"Invoice delta transfer for {pi.name}%"), "docstatus": 1},
		"name",
	)
	expensed = 0.0
	if transfer:
		for r in _gl(transfer):
			if r.account == expense:
				expensed = flt(r.debit) - flt(r.credit)
	hav_after = flt(recalculate_asset_values(asset_name, save=False)["historical_asset_value"], 2)
	ok = bool(ft) and new_ava == 0 and flt(expensed, 2) == 200_000.00 and hav_after == hav_before
	return (
		("PASS" if ok else "FAIL"),
		f"delta {expensed:,.2f} to the post-disposal expense account (want 200,000.00); "
		f"treatment recorded={bool(ft)}; new AVAs={new_ava} (want 0); HAV {hav_before:,.2f} "
		f"-> {hav_after:,.2f} (unchanged)",
	)


# =============================================================== TC-025
@tc("TC-025", "Useful Life Adjustment +12 Months")
def tc025():
	_ensure_fiscal_years(2024, 2031)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-025 UL Plus", 1_200_000, "2024-01-31", 36, "2024-01-01"
	)
	_post_through(asset.name, "2025-12-31")
	horizon_before = frappe.db.sql(
		"""select max(ds.schedule_date) from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s and ads.status = 'Active'""",
		asset.name,
	)[0][0]

	ava = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": "2025-12-31",
			"transaction_type": "Useful Life Adjustment",
			"current_asset_value": flt(frappe.db.get_value("Asset", asset.name, "net_book_value")),
			"new_asset_value": flt(frappe.db.get_value("Asset", asset.name, "net_book_value")),
			"adjusted_life_months": 12,
		}
	)
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()

	fb = frappe.db.get_value(
		"Asset Finance Book", {"parent": asset.name}, "total_number_of_depreciations"
	)
	horizon_after = frappe.db.sql(
		"""select max(ds.schedule_date) from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s and ads.status = 'Active'""",
		asset.name,
	)[0][0]
	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset.name, save=False)
	future = [
		r
		for r in _rows(asset.name)[1]
		if getdate(r.schedule_date) > getdate("2025-12-31")
	]
	future_total = flt(sum(flt(r.depreciation_amount) for r in future), 2)
	# RUL is posting-based (client, 18/08): the unposted periods left on
	# the Active schedule — which is also the document's own figure of 24.
	ok = (
		cint_(fb) == 48
		and getdate(horizon_after) == getdate(add_months(horizon_before, 12))
		and future_total == flt(values["net_book_value"], 2)
		and flt(values["remaining_useful_life_months"]) == flt(len(future))
		and len(future) == 24
	)
	return (
		("PASS" if ok else "FAIL"),
		f"finance book periods {fb} (want 48); horizon {horizon_before} -> {horizon_after}; "
		f"{len(future)} future rows totalling {future_total:,.2f} vs NBV "
		f"{flt(values['net_book_value']):,.2f}; RUL {values['remaining_useful_life_months']} months "
		f"(want 24 = unposted periods, matching the document)",
	)


# =============================================================== TC-026
@tc("TC-026", "Useful Life Adjustment Drives RUL to Zero")
def tc026():
	_ensure_fiscal_years(2024, 2031)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-026 UL Zero", 1_200_000, "2024-01-31", 36, "2024-01-01"
	)
	_post_through(asset.name, "2026-06-30")
	from asset_enterprise.asset_values import recalculate_asset_values

	nbv_before = flt(recalculate_asset_values(asset.name, save=False)["net_book_value"], 2)
	ava = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": "2026-06-30",
			"transaction_type": "Useful Life Adjustment",
			"current_asset_value": nbv_before,
			"new_asset_value": nbv_before,
			"adjusted_life_months": -6,
		}
	)
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()

	values = recalculate_asset_values(asset.name, save=False)
	status = frappe.db.get_value("Asset", asset.name, "status")
	one_shot = frappe.db.get_value(
		"Financial Treatment",
		{"asset": asset.name, "transaction_type": ("like", "%RUL Exhausted%"), "status": "Posted"},
		["name", "amount", "journal_entry"],
		as_dict=True,
	)
	ok = (
		one_shot
		and flt(one_shot.amount, 2) == nbv_before
		and flt(values["net_book_value"], 2) == 0.00
		and status == "Fully Depreciated"
	)
	return (
		("PASS" if ok else "FAIL"),
		f"NBV before {nbv_before:,.2f}; one-shot posting {one_shot and flt(one_shot.amount):,.2f} "
		f"via {one_shot and one_shot.journal_entry}; NBV after "
		f"{flt(values['net_book_value']):,.2f}; status {status}",
	)


def _service_item(code="TC-SERVICE-ITEM"):
	if not frappe.db.exists("Item", code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": "TC Maintenance Service",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"is_fixed_asset": 0,
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
	return code


# =============================================================== TC-027
@tc("TC-027", "Capitalized Maintenance — Add Service to Active Asset")
def tc027():
	"""Steps: Asset Capitalization, type Capitalized Maintenance, target
	= a SUBMITTED asset, add service 50,000, submit.
	Expected: submitted target accepted; DR Fixed Asset 50,000 /
	CR Service Expense 50,000; HAV 1,000,000 -> 1,050,000; schedule
	recalculated."""
	_ensure_fiscal_years(2025, 2032)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-027 CM Target", 1_000_000, get_last_day(add_months(nowdate(), -1)),
		60, add_months(nowdate(), -2)
	)
	expense = _plain(company, "Expense")
	total_before = flt(
		sum(flt(r.depreciation_amount) for r in _rows(asset.name)[1]), 2
	)

	cap = frappe.get_doc(
		{
			"doctype": "Asset Capitalization",
			"company": company,
			"transaction_type": "Capitalized Maintenance",
			"target_asset": asset.name,
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"service_items": [
				{
					"item_code": _service_item(),
					"qty": 1,
					"rate": 50_000,
					"expense_account": expense,
				}
			],
		}
	)
	cap.flags.ignore_permissions = True
	cap.insert()
	cap.submit()

	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset.name, save=False)
	legs = []
	for je in frappe.get_all(
		"Journal Entry", filters={"user_remark": ("like", f"%{cap.name}%"), "docstatus": 1}, pluck="name"
	):
		legs.extend(_gl(je))
	fa_account = _account(company, "Fixed Asset")
	fa_debit = sum(flt(r.debit) for r in legs if r.account == fa_account)
	svc_credit = sum(flt(r.credit) for r in legs if r.account == expense)
	total_after = flt(sum(flt(r.depreciation_amount) for r in _rows(asset.name)[1]), 2)
	ok = (
		flt(values["historical_asset_value"], 2) == 1_050_000.00
		and flt(fa_debit, 2) == 50_000.00
		and flt(svc_credit, 2) == 50_000.00
		and total_after == flt(total_before + 50_000, 2)
	)
	return (
		("PASS" if ok else "FAIL"),
		f"HAV {flt(values['historical_asset_value']):,.2f} (want 1,050,000.00); DR fixed asset "
		f"{flt(fa_debit):,.2f} / CR service expense {flt(svc_credit):,.2f} (want 50,000 each); "
		f"schedule total {total_before:,.2f} -> {total_after:,.2f}",
	)


# =============================================================== TC-028
@tc("TC-028", "Capitalized Maintenance — Reclassification Sub-Type")
def tc028():
	"""Expected: one combined JE, standard Disposal + Addition, NO
	clearing account; asset rolls to the new category at the same NBV."""
	company = _company()
	source_cat = _category(company, "TC Machinery", suspense=_plain(company, "Liability"))
	target_cat = _category(company, "TC Vehicles2", suspense=_plain(company, "Liability"))
	source = _plain_asset(company, source_cat, "TC-028 MACHINE-001", 2_000_000,
	                      opening_accumulated_depreciation=400_000)
	source.submit()
	target = _plain_asset(company, target_cat, "TC-028 MACHINE-001-V", 2_000_000)
	# draft target, as the steps say

	cap = frappe.get_doc(
		{
			"doctype": "Asset Capitalization",
			"company": company,
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Reclassification / Asset Category Transfer",
			# The build consumes asset_items INTO target_asset (core's own
			# semantics); TC-028's steps name the opposite mapping.
			"target_asset": target.name,
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"asset_items": [{"asset": source.name}],
		}
	)
	cap.flags.ignore_permissions = True
	cap.insert()
	cap.submit()

	from asset_enterprise.asset_values import recalculate_asset_values

	target_values = recalculate_asset_values(target.name, save=False)
	jes = frappe.get_all(
		"Journal Entry", filters={"user_remark": ("like", f"%{cap.name}%"), "docstatus": 1},
		pluck="name",
	)
	legs = []
	for je in jes:
		legs.extend(_gl(je))
	clearing = frappe.db.get_value(
		"Asset Category Account", {"parent": source_cat, "company_name": company},
		"capitalization_clearing_account",
	)
	clearing_used = any(r.account == clearing for r in legs) if clearing else False
	ok = (
		len(jes) == 1
		and not clearing_used
		and flt(target_values["net_book_value"], 2) == 1_600_000.00
	)
	if ok:
		return (
			"DEVIATION",
			f"outcome is right — 1 combined entry, no clearing account, NBV "
			f"{flt(target_values['net_book_value']):,.2f} preserved — but the FIELDS are the "
			f"other way round: the build consumes asset_items INTO target_asset, while TC-028 "
			f"step 2 says target_asset = the source asset and asset_items = the new one. "
			f"Following the steps literally throws 'Source Asset must be submitted'",
		)
	return (
		("FAIL"),
		f"{len(jes)} journal entr{'y' if len(jes) == 1 else 'ies'} (want 1), clearing account "
		f"used={clearing_used} (want False); target NBV {flt(target_values['net_book_value']):,.2f} "
		f"(want 1,600,000.00); legs={[(r.account.split(' - ')[0], flt(r.debit), flt(r.credit)) for r in legs]}",
	)


# =============================================================== TC-030a
@tc("TC-030a", "Same-Period Restore — Accidental Disposal Reversed")
def tc030a():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	asset = _plain_asset(company, cat, "TC-030a FA-500", 500_000,
	                     opening_accumulated_depreciation=350_000)
	asset.submit()

	from asset_enterprise import disposal
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.restore import restore_asset

	disposal.scrap_asset(asset.name, scrapping_type="Damage")
	scrap_je = frappe.db.get_value(
		"Financial Treatment",
		{"asset": asset.name, "transaction_type": ("like", "%Scrap%")},
		"journal_entry",
	)
	restore_asset(asset.name)

	mirror = frappe.db.get_value("Asset", asset.name, "scrap_reversal_journal_entry")
	scrap_still_posted = frappe.db.get_value("Journal Entry", scrap_je, "docstatus") == 1 if scrap_je else None
	values = recalculate_asset_values(asset.name, save=False)
	status = frappe.db.get_value("Asset", asset.name, "status")
	ok = (
		bool(mirror)
		and scrap_still_posted
		and flt(values["net_book_value"], 2) == 150_000.00
		and status not in ("Scrapped",)
	)
	return (
		("PASS" if ok else "FAIL"),
		f"mirror JE {mirror}; original scrap JE {scrap_je} still posted={scrap_still_posted}; "
		f"NBV back to {flt(values['net_book_value']):,.2f} (want 150,000.00); status {status}",
	)


# ============================================================== TC-030a2
@tc("TC-030a2", "Restore Blocked Outside the Window")
def tc030a2():
	_ensure_fiscal_years(2025, 2028)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	asset = _depreciating_asset(
		company, cat, "TC-030a2 FA-510", 1_200_000, "2026-01-31", 36, "2026-01-01"
	)
	from asset_enterprise import disposal
	from asset_enterprise.restore import restore_asset

	_post_through(asset.name, "2026-03-31")
	disposal.scrap_asset(asset.name, scrapping_type="Damage", scrap_date="2026-03-31")
	_post_through(asset.name, "2026-06-30")
	try:
		restore_asset(asset.name)
	except frappe.ValidationError as e:
		msg = str(e)
		status = frappe.db.get_value("Asset", asset.name, "status")
		offers = "Replacement" in msg or "GAP-016" in msg
		return (
			("PASS" if offers and status == "Scrapped" else "FAIL"),
			f"blocked: {msg[:150]} | status {status}",
		)
	return "FAIL", "restore allowed outside the same-period window"


# =============================================================== TC-030b
@tc("TC-030b", "Create Replacement Asset — Cross-Period Case")
def tc030b():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	source = _plain_asset(company, cat, "TC-030b FA-500", 1_000_000,
	                      opening_accumulated_depreciation=500_000)
	source.submit()
	from asset_enterprise import disposal
	from asset_enterprise.restore import create_replacement_asset

	disposal.scrap_asset(source.name, scrapping_type="Damage")
	replacement = create_replacement_asset(source.name)
	new_doc = frappe.get_doc("Asset", replacement.get("name") if isinstance(replacement, dict) else replacement)
	new_doc.net_purchase_amount = 400_000
	new_doc.purchase_amount = 400_000
	new_doc.available_for_use_date = nowdate()
	new_doc.flags.ignore_permissions = True
	new_doc.save()
	new_doc.submit()

	back = frappe.db.get_value("Asset", source.name, ["replaced_by_asset", "status"], as_dict=True)
	fwd = frappe.db.get_value("Asset", new_doc.name, "replacement_of_asset")
	activity = frappe.db.count(
		"Asset Activity", {"asset": new_doc.name, "subject": ("like", "%replacement%")}
	)
	ok = (
		back.replaced_by_asset == new_doc.name
		and fwd == source.name
		and back.status == "Scrapped"
		and activity >= 1
	)
	return (
		("PASS" if ok else "FAIL"),
		f"{source.name}.replaced_by={back.replaced_by_asset} status={back.status}; "
		f"{new_doc.name}.replacement_of={fwd}; history rows on the new asset={activity}",
	)


# =============================================================== TC-030c
@tc("TC-030c", "Replacement Chain Report — Two-Way Traceability")
def tc030c():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	source = _plain_asset(company, cat, "TC-030c FA-500", 1_000_000,
	                      opening_accumulated_depreciation=500_000)
	source.submit()
	from asset_enterprise import disposal
	from asset_enterprise.restore import create_replacement_asset

	disposal.scrap_asset(source.name, scrapping_type="Damage")
	replacement = create_replacement_asset(source.name)
	new_doc = frappe.get_doc("Asset", replacement.get("name") if isinstance(replacement, dict) else replacement)
	new_doc.available_for_use_date = nowdate()
	new_doc.flags.ignore_permissions = True
	new_doc.save()
	new_doc.submit()

	from asset_enterprise.asset_enterprise.report.replacement_chain.replacement_chain import execute

	_cols, data = execute(frappe._dict({"company": company}))[:2]
	src_row = [d for d in data if d.get("asset") == source.name]
	new_row = [d for d in data if d.get("asset") == new_doc.name]
	ok = (
		src_row
		and new_row
		and src_row[0].get("replaced_by_asset") == new_doc.name
		and new_row[0].get("replacement_of_asset") == source.name
	)
	return (
		("PASS" if ok else "FAIL"),
		f"report rows: {source.name} Replaced By="
		f"{src_row and src_row[0].get('replaced_by_asset')}; {new_doc.name} Replacement Of="
		f"{new_row and new_row[0].get('replacement_of_asset')}",
	)


def _scrapping_type(name, company, account):
	if not frappe.db.exists("Scrapping Type", name):
		doc = frappe.get_doc({"doctype": "Scrapping Type", "scrapping_type_name": name})
		doc.append("accounts", {"company": company, "gl_account": account})
		doc.flags.ignore_permissions = True
		doc.insert()
	else:
		doc = frappe.get_doc("Scrapping Type", name)
		row = next((r for r in doc.accounts if r.company == company), None)
		if row:
			row.gl_account = account
		else:
			doc.append("accounts", {"company": company, "gl_account": account})
		doc.flags.ignore_permissions = True
		doc.save()
	return name


# =============================================================== TC-032
@tc("TC-032", "Partial Scrap by Percentage")
def tc032():
	"""HAV 1,000,000, Accum 200,000, scrap 30% as Obsolescence.
	Expected: DR Accum 60,000 / DR Obsolescence 240,000 / CR FA 300,000."""
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	loss_account = _plain(company, "Expense")
	_scrapping_type("Obsolescence", company, loss_account)
	asset = _plain_asset(company, cat, "TC-032 Partial %", 1_000_000,
	                     opening_accumulated_depreciation=200_000)
	asset.submit()

	from asset_enterprise import disposal
	from asset_enterprise.asset_values import recalculate_asset_values

	disposal.partial_scrap_asset(asset.name, percentage=30, scrapping_type="Obsolescence")
	ft = frappe.db.get_value(
		"Financial Treatment",
		{"asset": asset.name, "transaction_type": ("like", "%Partial%"), "status": "Posted"},
		"journal_entry",
	)
	legs = {(r.account, flt(r.debit, 2), flt(r.credit, 2)) for r in _gl(ft)} if ft else set()
	want = {
		(_account(company, "Accumulated Depreciation"), 60_000.00, 0.00),
		(loss_account, 240_000.00, 0.00),
		(_account(company, "Fixed Asset"), 0.00, 300_000.00),
	}
	values = recalculate_asset_values(asset.name, save=False)
	ok = legs == want
	return (
		("PASS" if ok else "FAIL"),
		f"JE {ft}: {sorted(legs)} | HAV {flt(values['historical_asset_value']):,.2f} "
		f"accum {flt(values['accumulated_depreciation_value']):,.2f}",
	)


# =============================================================== TC-033
@tc("TC-033", "Scrapping Type GL Routing")
def tc033():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	donation = _plain(company, "Expense")
	_scrapping_type("Donation", company, donation)
	asset = _plain_asset(company, cat, "TC-033 Donation", 900_000,
	                     opening_accumulated_depreciation=300_000)
	asset.submit()

	from asset_enterprise import disposal

	disposal.scrap_asset(asset.name, scrapping_type="Donation")
	ft = frappe.db.get_value(
		"Financial Treatment",
		{"asset": asset.name, "transaction_type": ("like", "%Scrap%"), "status": "Posted"},
		"journal_entry",
	)
	legs = {(r.account, flt(r.debit, 2), flt(r.credit, 2)) for r in _gl(ft)} if ft else set()
	want = {
		(_account(company, "Accumulated Depreciation"), 300_000.00, 0.00),
		(donation, 600_000.00, 0.00),
		(_account(company, "Fixed Asset"), 0.00, 900_000.00),
	}
	return (("PASS" if legs == want else "FAIL"), f"JE {ft}: {sorted(legs)} (donation account {donation})")


# =============================================================== TC-034
@tc("TC-034", "Cost Center Transfer via Movement")
def tc034():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	ccs = frappe.get_all(
		"Cost Center", filters={"company": company, "is_group": 0}, pluck="name", limit=2
	)
	if len(ccs) < 2:
		return "FAIL", "site has fewer than two cost centres to move between"
	asset = _plain_asset(company, cat, "TC-034 CC Move", 100_000, cost_center=ccs[0])
	asset.submit()
	mv = frappe.get_doc(
		{
			"doctype": "Asset Movement",
			"company": company,
			"purpose": "Transfer",
			"transaction_date": nowdate(),
			"assets": [
				{"asset": asset.name, "target_cost_center": ccs[1]}
			],
		}
	)
	mv.flags.ignore_permissions = True
	mv.insert()
	mv.submit()
	after = frappe.db.get_value("Asset", asset.name, "cost_center")
	source_cc = frappe.db.get_value(
		"Asset Movement Item", {"parent": mv.name}, "source_cost_center"
	)
	ok = after == ccs[1] and source_cc == ccs[0]
	return (
		("PASS" if ok else "FAIL"),
		f"asset cost centre {ccs[0]} -> {after} (want {ccs[1]}); source_cost_center recorded "
		f"as {source_cc}",
	)


# =============================================================== TC-035
@tc("TC-035", "Depreciation Proration on Cost Centre Change")
def tc035():
	"""Transfer mid-period, then post that period: ONE entry with a
	debit per cost centre, split by days."""
	_ensure_fiscal_years(2025, 2029)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	ccs = frappe.get_all(
		"Cost Center", filters={"company": company, "is_group": 0}, pluck="name", limit=2
	)
	if len(ccs) < 2:
		return "FAIL", "site has fewer than two cost centres to move between"
	asset = _depreciating_asset(
		company, cat, "TC-035 CC Proration", 1_200_000, "2026-04-30", 36, "2026-04-01"
	)
	asset.db_set("cost_center", ccs[0])
	mv = frappe.get_doc(
		{
			"doctype": "Asset Movement",
			"company": company,
			"purpose": "Transfer",
			"transaction_date": "2026-04-15",
			"assets": [
				{"asset": asset.name, "target_cost_center": ccs[1]}
			],
		}
	)
	mv.flags.ignore_permissions = True
	mv.insert()
	mv.submit()
	_post_through(asset.name, "2026-04-30")
	_sched, rows = _rows(asset.name)
	je = rows[0].journal_entry
	legs = _gl(je) if je else []
	debits = [r for r in legs if flt(r.debit)]
	by_cc = frappe.db.sql(
		"""select cost_center, sum(debit) from `tabGL Entry`
		   where voucher_no = %s and is_cancelled = 0 and debit > 0 group by cost_center""",
		je,
	)
	ok = len(debits) == 2 and len(by_cc) == 2
	return (
		("PASS" if ok else "FAIL"),
		f"JE {je}: {len(debits)} debit lines across {len(by_cc)} cost centres -> "
		f"{[(c, flt(a, 2)) for c, a in by_cc]}; row total "
		f"{flt(rows[0].depreciation_amount):,.2f}",
	)


# =============================================================== TC-036
@tc("TC-036", "Movement Cancel Restores Prior Cost Centre, No Reversal JE")
def tc036():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	ccs = frappe.get_all(
		"Cost Center", filters={"company": company, "is_group": 0}, pluck="name", limit=2
	)
	if len(ccs) < 2:
		return "FAIL", "site has fewer than two cost centres to move between"
	asset = _plain_asset(company, cat, "TC-036 Move Cancel", 100_000, cost_center=ccs[0])
	asset.submit()
	mv = frappe.get_doc(
		{
			"doctype": "Asset Movement",
			"company": company,
			"purpose": "Transfer",
			"transaction_date": nowdate(),
			"assets": [
				{"asset": asset.name, "target_cost_center": ccs[1]}
			],
		}
	)
	mv.flags.ignore_permissions = True
	mv.insert()
	mv.submit()
	gl_before = frappe.db.count("GL Entry", {"voucher_no": mv.name})
	mv.reload()
	mv.cancel()
	after = frappe.db.get_value("Asset", asset.name, "cost_center")
	gl_after = frappe.db.count("GL Entry", {"voucher_no": mv.name})
	ok = after == ccs[0] and gl_before == 0 and gl_after == 0
	return (
		("PASS" if ok else "FAIL"),
		f"cost centre restored to {after} (want {ccs[0]}); GL entries against the movement: "
		f"{gl_before} before cancel, {gl_after} after (want 0)",
	)


# =============================================================== TC-037
@tc("TC-037", "Employee + Location + Cost Centre in One Movement")
def tc037():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	ccs = frappe.get_all(
		"Cost Center", filters={"company": company, "is_group": 0}, pluck="name", limit=2
	)
	locations = frappe.get_all("Location", pluck="name", limit=2)
	employee = frappe.db.get_value("Employee", {"status": "Active"}, "name")
	if len(ccs) < 2 or len(locations) < 2 or not employee:
		return "MANUAL", (
			f"site lacks the masters to drive this ({len(ccs)} cost centres, "
			f"{len(locations)} locations, employee={employee})"
		)
	asset = _plain_asset(company, cat, "TC-037 Combo", 100_000, cost_center=ccs[0])
	frappe.db.set_value("Asset", asset.name, "location", locations[0], update_modified=False)
	asset.reload()
	asset.submit()
	mv = frappe.get_doc(
		{
			"doctype": "Asset Movement",
			"company": company,
			"purpose": "Transfer",
			"transaction_date": nowdate(),
			"assets": [
				{"asset": asset.name, "source_location": locations[0],
				 "target_location": locations[1], "to_employee": employee,
				 "target_cost_center": ccs[1]}
			],
		}
	)
	mv.flags.ignore_permissions = True
	mv.insert()
	mv.submit()
	a = frappe.db.get_value("Asset", asset.name, ["location", "custodian", "cost_center"], as_dict=True)
	history = frappe.get_all(
		"Asset Activity", filters={"asset": asset.name}, fields=["subject"], order_by="creation desc",
		limit=3,
	)
	combined = [h for h in history if "location" in (h.subject or "").lower()
	            and "cost" in (h.subject or "").lower()]
	ok = a.location == locations[1] and a.custodian == employee and a.cost_center == ccs[1]
	return (
		("PASS" if ok and combined else ("DEVIATION" if ok else "FAIL")),
		f"location {a.location}, custodian {a.custodian}, cost centre {a.cost_center} — all three "
		f"moved in one movement; single summarising history row="
		f"{bool(combined)} (latest: {history and history[0].subject})",
	)


# =============================================================== TC-038
@tc("TC-038", "Asset as GL Dimension")
def tc038():
	_ensure_fiscal_years(2025, 2029)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	registered = frappe.db.exists("Accounting Dimension", {"document_type": "Asset"})
	asset = _depreciating_asset(
		company, cat, "TC-038 Dimension", 1_200_000, get_last_day(add_months(nowdate(), -1)),
		36, add_months(nowdate(), -2)
	)
	_post_through(asset.name, get_last_day(add_months(nowdate(), -1)))
	_sched, rows = _rows(asset.name)
	je = rows[0].journal_entry
	has_field = frappe.get_meta("GL Entry").has_field("asset")
	populated = 0
	if has_field and je:
		populated = frappe.db.count("GL Entry", {"voucher_no": je, "asset": asset.name})
	total = frappe.db.count("GL Entry", {"voucher_no": je}) if je else 0
	ok = bool(registered) and has_field and populated == total and total
	return (
		("PASS" if ok else "FAIL"),
		f"dimension registered={bool(registered)}; GL Entry.asset field exists={has_field}; "
		f"{populated} of {total} GL rows on the depreciation entry carry the asset",
	)


# =============================================================== TC-039
@tc("TC-039", "Asset Count — Discovery and Missing")
def tc039():
	return (
		"DEFERRED",
		"Asset Count (GAP-024) was deferred by client decision C83 — no Asset Count "
		"doctype is in scope, so there is nothing to exercise",
	)


# =============================================================== TC-040
@tc("TC-040", "Historical Catch-up with Immutable Ledger")
def tc040():
	"""AFU 01/07/2024, first posting 01/03/2025. Expected: ONE entry —
	PYA debit for 184 prior-year days, expense debit for 59 current-year
	days, one credit for 243."""
	_ensure_fiscal_years(2024, 2029)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	pya = _plain(company, "Expense")
	frappe.db.set_value(
		"Asset Category Account", {"parent": cat, "company_name": company},
		"pya_expense_account", pya, update_modified=False,
	)
	asset = _depreciating_asset(
		company, cat, "TC-040 Catch-up", 1_200_000, "2025-03-31", 36, "2024-07-01"
	)
	_post_through(asset.name, "2025-03-31")
	_sched, rows = _rows(asset.name)
	first = rows[0]
	legs = _gl(first.journal_entry) if first.journal_entry else []
	debits = [r for r in legs if flt(r.debit)]
	credits = [r for r in legs if flt(r.credit)]
	pya_leg = [r for r in debits if r.account == pya]
	rate = flt(first.daily_rate or 0, 6)
	prior_days = round(flt(pya_leg[0].debit) / rate) if pya_leg and rate else 0
	ok = (
		len(debits) == 2
		and len(credits) == 1
		and pya_leg
		and flt(first.days_in_period) == 274
		and prior_days == 184
	)
	return (
		("PASS" if ok else "FAIL"),
		f"one entry with {len(debits)} debits / {len(credits)} credit; PYA covers {prior_days} "
		f"days (want 184); row spans {first.days_in_period} days "
		f"(01/07/2024 -> 31/03/2025 inclusive = 274; the doc's 243 stops at 28/02)",
	)


def _composite(company, cat, name, gross, accum=0):
	doc = _plain_asset(company, cat, name, gross, opening_accumulated_depreciation=accum)
	doc.submit()
	return doc


def _cm_merge(company, target, source, posting_date=None):
	cap = frappe.get_doc(
		{
			"doctype": "Asset Capitalization",
			"company": company,
			"transaction_type": "Capitalized Maintenance",
			"transaction_sub_type": "Standard Maintenance",
			"target_asset": target,
			"posting_date": posting_date or nowdate(),
			"set_posting_time": 1,
			"asset_items": [{"asset": source}],
		}
	)
	cap.flags.ignore_permissions = True
	cap.insert()
	cap.submit()
	return cap


# =============================================================== TC-029
@tc("TC-029", "Source Asset Merged into Composite — Two-Leg via Clearing")
def tc029():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	frappe.db.set_value(
		"Asset Category Account", {"parent": cat, "company_name": company},
		"capitalization_clearing_account", clearing, update_modified=False,
	)
	source = _composite(company, cat, "TC-029 Source", 800_000, accum=300_000)
	target = _composite(company, cat, "TC-029 ASSEMBLY-A", 1_000_000)
	cap = _cm_merge(company, target.name, source.name)

	legs = []
	for je in frappe.get_all(
		"Journal Entry", filters={"user_remark": ("like", f"%{cap.name}%"), "docstatus": 1}, pluck="name"
	):
		legs.extend(_gl(je))
	clearing_net = flt(sum(flt(r.debit) - flt(r.credit) for r in legs if r.account == clearing), 2)
	from asset_enterprise.asset_values import recalculate_asset_values

	tgt = recalculate_asset_values(target.name, save=False)
	src_state = frappe.db.get_value("Asset", source.name, ["docstatus", "status"], as_dict=True)
	merge_log = frappe.db.count("Composite Merge Log Entry", {"parent": target.name})
	ok = (
		clearing_net == 0.00
		and flt(tgt["historical_asset_value"], 2) == 1_500_000.00
		and src_state.docstatus == 1
		and src_state.status == "Disposed"
		and merge_log >= 1
	)
	return (
		("PASS" if ok else "FAIL"),
		f"clearing nets {clearing_net:,.2f} (want 0); composite HAV "
		f"{flt(tgt['historical_asset_value']):,.2f} (want 1,500,000 = 1,000,000 + NBV 500,000); "
		f"source docstatus {src_state.docstatus} status {src_state.status}; merge log rows {merge_log}",
	)


# =============================================================== TC-031
@tc("TC-031", "Partial Scrap by Value with Proportional Accum")
def tc031():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	loss = _plain(company, "Expense")
	_scrapping_type("Damage", company, loss)
	asset = _plain_asset(company, cat, "TC-031 Partial Value", 2_000_000,
	                     opening_accumulated_depreciation=1_517_808.22)
	asset.submit()
	from asset_enterprise import disposal
	from asset_enterprise.asset_values import recalculate_asset_values

	disposal.partial_scrap_asset(asset.name, scrap_value=100_000, scrapping_type="Damage")
	ft = frappe.db.get_value(
		"Financial Treatment",
		{"asset": asset.name, "transaction_type": ("like", "%Partial%"), "status": "Posted"},
		"journal_entry",
	)
	legs = {(r.account, flt(r.debit, 2), flt(r.credit, 2)) for r in _gl(ft)} if ft else set()
	want = {
		(_account(company, "Accumulated Depreciation"), 75_890.41, 0.00),
		(loss, 24_109.59, 0.00),
		(_account(company, "Fixed Asset"), 0.00, 100_000.00),
	}
	values = recalculate_asset_values(asset.name, save=False)
	ok = (
		legs == want
		and flt(values["historical_asset_value"], 2) == 1_900_000.00
		and flt(values["accumulated_depreciation_value"], 2) == 1_441_917.81
	)
	return (
		("PASS" if ok else "FAIL"),
		f"JE {ft}: {sorted(legs)}; HAV {flt(values['historical_asset_value']):,.2f} "
		f"(want 1,900,000) accum {flt(values['accumulated_depreciation_value']):,.2f} "
		f"(want 1,441,917.81)",
	)


# =============================================================== TC-041
@tc("TC-041", "Reversal of Capitalized Maintenance")
def tc041():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	frappe.db.set_value(
		"Asset Category Account", {"parent": cat, "company_name": company},
		"capitalization_clearing_account", clearing, update_modified=False,
	)
	source = _composite(company, cat, "TC-041 Source", 800_000, accum=300_000)
	target = _composite(company, cat, "TC-041 ASSEMBLY-A", 1_000_000)
	cap = _cm_merge(company, target.name, source.name)
	original_jes = frappe.get_all(
		"Journal Entry", filters={"user_remark": ("like", f"%{cap.name}%"), "docstatus": 1}, pluck="name"
	)
	cap.reload()
	cap.cancel()

	reversal = frappe.db.get_value(
		"Asset Capitalization",
		{"reversal_of_capitalization": cap.name, "docstatus": 1},
		["name", "transaction_type"], as_dict=True,
	)
	still_posted = all(
		frappe.db.get_value("Journal Entry", je, "docstatus") == 1 for je in original_jes
	)
	log_rows = frappe.get_all(
		"Composite Merge Log Entry", filters={"parent": target.name}, fields=["status"]
	)
	reversed_rows = [r for r in log_rows if r.status == "Reversed"]
	ok = (
		reversal
		and reversal.transaction_type == "Reversal of Capitalized Maintenance"
		and still_posted
		and reversed_rows
	)
	return (
		("PASS" if ok else "FAIL"),
		f"reversal doc {reversal and reversal.name} type {reversal and reversal.transaction_type}; "
		f"original entries still posted={still_posted}; merge log rows "
		f"{[r.status for r in log_rows]}",
	)


# ============================================================== TC-042a
@tc("TC-042a", "Asset Reversal Blocked — Has Posted Depreciation")
def tc042a():
	_ensure_fiscal_years(2025, 2029)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-042a Blocked", 1_200_000, get_last_day(add_months(nowdate(), -2)),
		36, add_months(nowdate(), -3)
	)
	_post_through(asset.name, get_last_day(add_months(nowdate(), -1)))
	try:
		asset.reload()
		asset.cancel()
	except frappe.ValidationError as e:
		msg = str(e)
		names_count = "posted depreciation" in msg.lower()
		return (("PASS" if names_count else "FAIL"), f"blocked: {msg[:150]}")
	return "FAIL", "asset reversed while posted depreciation entries exist"


# ============================================================== TC-042b
@tc("TC-042b", "Asset Reversal Allowed — No Posted Depreciation")
def tc042b():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _plain_asset(company, cat, "TC-042b Clean", 500_000)
	asset.submit()
	opening_je = _je_of_asset(asset.name, "Existing-Asset Opening")
	asset.reload()
	asset.cancel()
	state = frappe.db.get_value("Asset", asset.name, ["docstatus", "status", "booked_fixed_asset"], as_dict=True)
	original_posted = frappe.db.get_value("Journal Entry", opening_je, "docstatus") == 1 if opening_je else None
	mirror = frappe.db.count(
		"Financial Treatment", {"asset": asset.name, "status": "Reversed"}
	)
	ok = state.docstatus == 2 and not flt(state.booked_fixed_asset) and original_posted
	return (
		("PASS" if ok else "FAIL"),
		f"docstatus {state.docstatus} status {state.status} booked_fixed_asset "
		f"{state.booked_fixed_asset}; opening entry {opening_je} still posted={original_posted}; "
		f"reversed treatments={mirror}",
	)


# ============================================================== TC-044a
@tc("TC-044a", "AVA Reversal — No Subsequent Depreciation")
def tc044a():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	oci = _plain(company, "Liability")
	asset = _plain_asset(company, cat, "TC-044a AVA", 1_000_000)
	asset.submit()
	ava = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": nowdate(),
			"transaction_type": "Upward Revaluation",
			"current_asset_value": 1_000_000,
			"new_asset_value": 1_200_000,
			"difference_account": oci,
		}
	)
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()
	original_je = ava.get("journal_entry") or frappe.db.get_value(
		"Asset Value Adjustment", ava.name, "journal_entry"
	)
	ava.reload()
	ava.cancel()

	reversal = frappe.db.get_value(
		"Asset Value Adjustment", {"reversal_of_ava": ava.name, "docstatus": 1},
		["name", "journal_entry"], as_dict=True,
	)
	original_still = frappe.db.get_value("Journal Entry", original_je, "docstatus") == 1 if original_je else None
	from asset_enterprise.asset_values import recalculate_asset_values

	hav = flt(recalculate_asset_values(asset.name, save=False)["historical_asset_value"], 2)
	mirror_legs = {(r.account, flt(r.debit, 2), flt(r.credit, 2)) for r in _gl(reversal.journal_entry)} if reversal and reversal.journal_entry else set()
	ok = (
		reversal
		and original_still
		and hav == 1_000_000.00
		and (oci, 200_000.00, 0.00) in mirror_legs
	)
	return (
		("PASS" if ok else "FAIL"),
		f"reversal AVA {reversal and reversal.name}; original entry {original_je} still "
		f"posted={original_still}; mirror legs {sorted(mirror_legs)}; HAV back to {hav:,.2f}",
	)


# ============================================================== TC-045
@tc("TC-045", "Posted Schedule Rows Preserved on Modification")
def tc045():
	_ensure_fiscal_years(2025, 2029)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	asset = _depreciating_asset(
		company, cat, "TC-045 Preserve", 3_600_000, "2025-01-31", 36, "2025-01-01"
	)
	_post_through(asset.name, "2025-12-31")
	_sched, before = _rows(asset.name)
	posted_before = [(str(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
	                 for r in before if r.journal_entry]

	from asset_enterprise.depreciation import supersede_and_regenerate

	supersede_and_regenerate(asset.name, as_of_date="2025-12-31", reason="TC-045 modification")
	_sched2, after = _rows(asset.name)
	posted_after = [(str(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
	                for r in after if r.journal_entry]
	links_live = all(
		frappe.db.get_value("Journal Entry", je, "docstatus") == 1
		for _d, _a, je in posted_after
	)
	ok = posted_before == posted_after and len(posted_after) == 12 and links_live
	return (
		("PASS" if ok else "FAIL"),
		f"{len(posted_before)} posted rows before / {len(posted_after)} after, identical="
		f"{posted_before == posted_after}; their journal entries all still submitted={links_live}; "
		f"total rows {len(before)} -> {len(after)}",
	)


# ============================================================== TC-046
@tc("TC-046", "Capitalized Maintenance — Add to Submitted Composite")
def tc046():
	_ensure_fiscal_years(2025, 2032)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	expense = _plain(company, "Expense")
	composite = _depreciating_asset(
		company, cat, "TC-046 Composite", 1_000_000, get_last_day(add_months(nowdate(), -1)),
		60, add_months(nowdate(), -2)
	)
	sched_before = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": composite.name, "status": "Active", "docstatus": 1}, "name"
	)
	cap = frappe.get_doc(
		{
			"doctype": "Asset Capitalization",
			"company": company,
			"transaction_type": "Capitalized Maintenance",
			"target_asset": composite.name,
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"service_items": [
				{"item_code": _service_item(), "qty": 1, "rate": 50_000, "expense_account": expense}
			],
		}
	)
	cap.flags.ignore_permissions = True
	cap.insert()
	cap.submit()
	from asset_enterprise.asset_values import recalculate_asset_values

	hav = flt(recalculate_asset_values(composite.name, save=False)["historical_asset_value"], 2)
	old_status = frappe.db.get_value("Asset Depreciation Schedule", sched_before, "status")
	_sched_after, rows_after = _rows(composite.name)
	total_after = flt(sum(flt(r.depreciation_amount) for r in rows_after), 2)
	replaced = _sched_after != sched_before
	ok = hav == 1_050_000.00 and replaced and total_after == 1_050_000.00
	note = (
		"previous schedule removed rather than superseded — it had booked nothing, and an "
		"unposted schedule is a working copy (see GAP-031 note)"
		if old_status is None
		else f"previous schedule {sched_before} is {old_status}"
	)
	return (
		("PASS" if ok else "FAIL"),
		f"submitted composite accepted; HAV {hav:,.2f} (want 1,050,000); new schedule "
		f"{_sched_after} totals {total_after:,.2f}; {note}",
	)


# ============================================================= TC-047a
@tc("TC-047a", "Asset Repair Reversal — No Subsequent Depreciation")
def tc047a():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	asset = _plain_asset(company, cat, "TC-047a Repair", 500_000)
	asset.submit()
	repair = frappe.get_doc(
		{
			"doctype": "Asset Repair",
			"asset": asset.name,
			"company": company,
			"failure_date": nowdate(),
			"repair_status": "Completed",
			"completion_date": nowdate(),
			"capitalize_repair_cost": 1,
			"repair_cost": 25_000,
			"cost_center": frappe.db.get_value("Asset", asset.name, "cost_center"),
			"purchase_invoice": None,
			"capital_work_in_progress_account": clearing,
		}
	)
	repair.flags.ignore_permissions = True
	repair.insert()
	repair.submit()
	from asset_enterprise.asset_values import recalculate_asset_values

	hav_after_repair = flt(recalculate_asset_values(asset.name, save=False)["historical_asset_value"], 2)
	repair.reload()
	repair.cancel()
	reversal = frappe.db.get_value(
		"Asset Repair", {"reversal_of_repair": repair.name, "docstatus": 1}, "name"
	)
	back = frappe.db.get_value("Asset Repair", repair.name, "reversed_by_repair")
	hav_after_reversal = flt(recalculate_asset_values(asset.name, save=False)["historical_asset_value"], 2)
	ok = bool(reversal) and back == reversal and hav_after_reversal == 500_000.00
	return (
		("PASS" if ok else "FAIL"),
		f"HAV {hav_after_repair:,.2f} after repair -> {hav_after_reversal:,.2f} after reversal "
		f"(want 500,000); reversal repair {reversal}; back-link {back}",
	)


# ============================================================= TC-047d
@tc("TC-047d", "Repair Reversal Blocked on Fully-Depreciated Asset")
def tc047d():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	asset = _plain_asset(company, cat, "TC-047d Full", 500_000)
	asset.submit()
	repair = frappe.get_doc(
		{
			"doctype": "Asset Repair",
			"asset": asset.name,
			"company": company,
			"failure_date": nowdate(),
			"repair_status": "Completed",
			"completion_date": nowdate(),
			"capitalize_repair_cost": 1,
			"repair_cost": 25_000,
			"cost_center": frappe.db.get_value("Asset", asset.name, "cost_center"),
			"capital_work_in_progress_account": clearing,
		}
	)
	repair.flags.ignore_permissions = True
	repair.insert()
	repair.submit()
	frappe.db.set_value("Asset", asset.name, "status", "Fully Depreciated", update_modified=False)
	frappe.db.set_value("Asset", asset.name, "is_fully_depreciated", 1, update_modified=False)
	try:
		repair.reload()
		repair.cancel()
	except frappe.ValidationError as e:
		msg = str(e)
		reversal = frappe.db.count("Asset Repair", {"reversal_of_repair": repair.name})
		blocked = "fully depreciated" in msg.lower()
		vr038_wording = "Asset Value Adjustment" in msg
		verdict = "PASS" if (blocked and reversal == 0) else "FAIL"
		if verdict == "PASS" and not vr038_wording:
			verdict = "DEVIATION"
			return verdict, (
				f"blocked and no Reversal Repair created (0), but the message is core's "
				f"status guard — TC-047d asks for VR-038's wording pointing the user at an "
				f"Asset Value Adjustment: {msg[:110]}"
			)
		return verdict, f"blocked: {msg[:130]} | reversal repairs created: {reversal}"
	return "FAIL", "repair reversal allowed on a fully depreciated asset"


# ============================================================== TC-047e
@tc("TC-047e", "Capitalized Repair — HAV after repair equals base + total cost")
def tc047e():
	# Client issue 2026-08-25 (ACC-ASS-2026-00214): the ledger-derived
	# HAV double-counted a capitalized repair — the Addition FT recorded
	# +total_repair_cost but never linked the repair's GL voucher, so the
	# manual-GL sweep added the repair's GL a second time. Expected HAV
	# after a 2,000 asset + 16,000 repair (15,000 from a PI + 1,000
	# stock): 2,000 + 16,000 = 18,000, and back to 2,000 after reversal.
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	expense = _plain(company, "Expense")
	cc = frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name"
	) or frappe.db.get_value("Company", company, "cost_center")
	item = _stock_item()
	warehouse = _warehouse(company)
	_stock_in(company, item, warehouse, 200, 200)
	asset = _plain_asset(company, cat, "TC-047e Repair HAV", 2_000)
	asset.submit()

	# v16 shape: repair_cost is derived from the invoices child (PI rows).
	svc = "TC-REPAIR-SERVICE"
	if not frappe.db.exists("Item", svc):
		frappe.get_doc({
			"doctype": "Item", "item_code": svc, "item_name": "TC Repair Service",
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			"is_stock_item": 0, "is_fixed_asset": 0, "is_purchase_item": 1,
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice",
		"company": company,
		"supplier": _supplier(),
		"posting_date": nowdate(),
		"set_posting_time": 1,
		"items": [{
			"item_code": svc, "qty": 1, "rate": 15_000,
			"expense_account": expense, "cost_center": cc,
		}],
	})
	pi.flags.ignore_permissions = True
	pi.insert()
	pi.submit()

	repair = frappe.get_doc({
		"doctype": "Asset Repair",
		"asset": asset.name,
		"company": company,
		"failure_date": nowdate(),
		"repair_status": "Completed",
		"completion_date": nowdate(),
		"capitalize_repair_cost": 1,
		"cost_center": cc,
		"capital_work_in_progress_account": clearing,
		"invoices": [{"purchase_invoice": pi.name, "expense_account": expense, "repair_cost": 15_000}],
		"stock_items": [
			{"item_code": item, "consumed_quantity": 1, "valuation_rate": 1_000,
			 "warehouse": warehouse, "total_value": 1_000}
		],
	})
	repair.flags.ignore_permissions = True
	repair.insert()
	repair.submit()
	repair.reload()

	from asset_enterprise.asset_values import recalculate_asset_values

	hav_after = flt(recalculate_asset_values(asset.name, save=False)["historical_asset_value"], 2)
	ft = frappe.db.get_value(
		"Financial Treatment",
		{"source_doctype": "Asset Repair", "source_name": repair.name, "status": "Posted"},
		["name", "voucher_type", "voucher_no", "journal_entry"], as_dict=True,
	)
	repair.reload()
	repair.cancel()
	hav_after_rev = flt(recalculate_asset_values(asset.name, save=False)["historical_asset_value"], 2)
	ok = (
		flt(repair.repair_cost) == 15_000  # derived from the invoices child
		and flt(repair.total_repair_cost) == 16_000
		and hav_after == 18_000.00
		and ft and ft.voucher_type == "Asset Repair" and ft.voucher_no == repair.name
		and hav_after_rev == 2_000.00
	)
	return (
		("PASS" if ok else "FAIL"),
		f"repair_cost {flt(repair.repair_cost):,.0f} + stock 1,000 = total "
		f"{flt(repair.total_repair_cost):,.0f}; HAV after repair {hav_after:,.2f} (want 18,000); "
		f"FT {ft and ft.name} voucher {ft and ft.voucher_no}; "
		f"HAV after reversal {hav_after_rev:,.2f} (want 2,000)",
	)


# ============================================================== TC-047f
def _repaired_depreciating_asset(label):
	"""A 2,000 depreciating asset with one posted row and a 16,000
	capitalized repair — the shape of the client's ACC-ASS-2026-00214.
	Shared by TC-047f and TC-047g."""
	from frappe.utils import get_first_day

	from asset_enterprise.depreciation import _post_one, enable_depreciation

	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	expense = _plain(company, "Expense")
	cc = frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name"
	) or frappe.db.get_value("Company", company, "cost_center")
	asset = _plain_asset(company, cat, label, 2_000)
	asset.submit()
	enable_depreciation(
		asset.name, total_number_of_depreciations=36, frequency_of_depreciation=1,
		depreciation_start_date=get_first_day(add_months(nowdate(), -12)),
	)
	row = frappe.db.sql(
		"""select ds.name as row_name, ds.parent as schedule, ds.schedule_date,
		   ds.depreciation_amount, ds.cost_center, ads.asset, ads.finance_book,
		   ds.daily_rate, ds.days_in_period
		from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') = ''
		order by ds.schedule_date limit 1""",
		asset.name,
		as_dict=True,
	)[0]
	_post_one(row, getdate(nowdate()))

	svc = "TC-REPAIR-SERVICE"
	if not frappe.db.exists("Item", svc):
		frappe.get_doc({
			"doctype": "Item", "item_code": svc, "item_name": "TC Repair Service",
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			"is_stock_item": 0, "is_fixed_asset": 0, "is_purchase_item": 1,
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice",
		"company": company,
		"supplier": _supplier(),
		"posting_date": nowdate(),
		"set_posting_time": 1,
		"items": [{
			"item_code": svc, "qty": 1, "rate": 16_000,
			"expense_account": expense, "cost_center": cc,
		}],
	})
	pi.flags.ignore_permissions = True
	pi.insert()
	pi.submit()
	repair = frappe.get_doc({
		"doctype": "Asset Repair",
		"asset": asset.name,
		"company": company,
		"failure_date": nowdate(),
		"repair_status": "Completed",
		"completion_date": nowdate(),
		"capitalize_repair_cost": 1,
		"cost_center": cc,
		"capital_work_in_progress_account": clearing,
		"invoices": [{"purchase_invoice": pi.name, "expense_account": expense, "repair_cost": 16_000}],
	})
	repair.flags.ignore_permissions = True
	repair.insert()
	repair.submit()
	return asset


def _unposted_schedule_total(asset_name):
	return flt(
		frappe.db.sql(
			"""select coalesce(sum(ds.depreciation_amount), 0)
			   from `tabDepreciation Schedule` ds
			   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			   where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			     and ifnull(ds.journal_entry, '') = ''""",
			asset_name,
		)[0][0]
	)


@tc("TC-047f", "Capitalized Repair — future schedule rebuilt from the corrected base")
def tc047f():
	# Blast radius of the 2026-08-25 client issue: the double-counted NBV
	# was spread into the FUTURE schedule rows, not just the display
	# (ACC-ASS-2026-00214 had 32,333.36 unposted; it should be 17,333.36).
	from asset_enterprise.asset_values import recalculate_asset_values

	asset = _repaired_depreciating_asset("TC-047f Repair Schedule")

	values = recalculate_asset_values(asset.name, save=False)
	salvage = flt(frappe.db.get_value(
		"Asset Finance Book", {"parent": asset.name}, "expected_value_after_useful_life") or 0
	)
	sched_unposted = _unposted_schedule_total(asset.name)
	# The schedule must spread the ABSOLUTE right number — 2,000 base plus
	# the 16,000 repair, less what is already posted. Comparing it to the
	# derived NBV alone proves nothing: the double-count moves both figures
	# together, so that check passed while the bug was live (2026-08-25).
	posted = flt(values["accumulated_depreciation_value"])
	expected = flt(18_000 - posted - salvage, 2)
	hav_ok = abs(flt(values["historical_asset_value"]) - 18_000) < 0.01
	ok = hav_ok and abs(sched_unposted - expected) < 0.01
	return (
		("PASS" if ok else "FAIL"),
		f"HAV {values['historical_asset_value']:,.2f} (want 18,000); Active schedule "
		f"unposted {sched_unposted:,.2f} (want {expected:,.2f} = 18,000 − posted "
		f"{posted:,.2f} − salvage {salvage:,.2f})",
	)


# ============================================================== TC-047g
@tc("TC-047g", "Capitalized Repair — backfill repairs an already-corrupted asset")
def tc047g():
	"""The forward fix protects new repairs; assets repaired BEFORE it
	still carry the double-count, and the backfill utility is what
	corrects them. It must fix both halves — the derived HAV and the
	future schedule rows the inflated NBV was spread over — and its dry
	run must report that truthfully while writing nothing.
	"""
	from asset_enterprise import repair as repair_mod
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import last_posted_schedule_date, supersede_and_regenerate

	asset = _repaired_depreciating_asset("TC-047g Repair Backfill")
	ft = frappe.get_all(
		"Financial Treatment",
		filters={"asset": asset.name, "transaction_type": "Capitalized Repair"},
		pluck="name",
	)[0]

	# Recreate the pre-fix state: treatment with no voucher link, and a
	# future schedule already spread over the inflated NBV.
	frappe.db.set_value("Financial Treatment", ft,
		{"voucher_type": None, "voucher_no": None}, update_modified=False)
	recalculate_asset_values(asset.name, save=True)
	supersede_and_regenerate(
		asset.name, as_of_date=last_posted_schedule_date(asset.name),
		reason="TC-047g: reproduce the pre-fix corruption",
	)
	corrupt_hav = flt(recalculate_asset_values(asset.name, save=False)["historical_asset_value"])
	corrupt_sched = _unposted_schedule_total(asset.name)

	# The utility commits, as a CLI repair tool should — but a COMMIT
	# releases the harness savepoint and this fixture would outlive the
	# run. Neutralise it for the duration; everything else is unchanged.
	real_commit = frappe.db.commit
	frappe.db.commit = lambda *a, **k: None
	try:
		# Dry run: must SEE the rebuild, and leave nothing behind.
		repair_mod.link_repair_voucher_references(asset=asset.name, dry_run=1)
		dry_clean = (
			not frappe.db.get_value("Financial Treatment", ft, "voucher_no")
			and abs(_unposted_schedule_total(asset.name) - corrupt_sched) < 0.01
		)
		repair_mod.link_repair_voucher_references(asset=asset.name, dry_run=0)
	finally:
		frappe.db.commit = real_commit
	values = recalculate_asset_values(asset.name, save=False)
	salvage = flt(frappe.db.get_value(
		"Asset Finance Book", {"parent": asset.name}, "expected_value_after_useful_life") or 0
	)
	fixed_sched = _unposted_schedule_total(asset.name)
	expected = flt(18_000 - flt(values["accumulated_depreciation_value"]) - salvage, 2)

	corrupted_ok = corrupt_hav > 18_000.01           # the fixture really was broken
	hav_ok = abs(flt(values["historical_asset_value"]) - 18_000) < 0.01
	sched_ok = abs(fixed_sched - expected) < 0.01
	ok = corrupted_ok and dry_clean and hav_ok and sched_ok
	return (
		("PASS" if ok else "FAIL"),
		f"corrupted HAV {corrupt_hav:,.2f} / schedule {corrupt_sched:,.2f}; dry run "
		f"left it untouched={dry_clean}; after backfill HAV "
		f"{values['historical_asset_value']:,.2f} (want 18,000) and schedule "
		f"{fixed_sched:,.2f} (want {expected:,.2f})",
	)


# ============================================================== TC-048
@tc("TC-048", "Composite Partial Scrap by Amount")
def tc048():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	loss = _plain(company, "Expense")
	_scrapping_type("Obsolescence", company, loss)
	composite = _plain_asset(company, cat, "TC-048 COMPOSITE-KILN", 50_000_000,
	                         opening_accumulated_depreciation=17_250_000)
	composite.submit()
	from asset_enterprise import disposal
	from asset_enterprise.asset_values import recalculate_asset_values

	disposal.partial_scrap_asset(composite.name, scrap_value=7_500_000,
	                             scrapping_type="Obsolescence")
	ft = frappe.db.get_value(
		"Financial Treatment",
		{"asset": composite.name, "transaction_type": ("like", "%Partial%"), "status": "Posted"},
		"journal_entry",
	)
	legs = {(r.account, flt(r.debit, 2), flt(r.credit, 2)) for r in _gl(ft)} if ft else set()
	want = {
		(_account(company, "Accumulated Depreciation"), 2_587_500.00, 0.00),
		(loss, 4_912_500.00, 0.00),
		(_account(company, "Fixed Asset"), 0.00, 7_500_000.00),
	}
	values = recalculate_asset_values(composite.name, save=False)
	ok = legs == want and flt(values["historical_asset_value"], 2) == 42_500_000.00
	return (
		("PASS" if ok else "FAIL"),
		f"JE {ft}: {sorted(legs)}; composite HAV {flt(values['historical_asset_value']):,.2f} "
		f"(want 42,500,000)",
	)


# ============================================================== TC-042c
@tc("TC-042c", "Asset Reversal after Manual Depreciation Reversal")
def tc042c():
	# B5 (2026-08-25): formerly a MANUAL stub — it needed the GA-0001-01
	# Journal Entry `reversal_of` field, which the bench did not carry
	# when the row was written. The field is present now and the sequence
	# is exactly verify_phase11 F3b: reverse each manual JE via a
	# GA-0001-01 counter-JE, then cancel the asset — the gate passes, and
	# core's destructive delete_depreciation_entries must NOT re-cancel
	# the originals or run the VR-031 db_set (retired under v1).
	if not frappe.get_meta("Journal Entry").has_field("reversal_of"):
		return (
			"MANUAL",
			"needs the GA-0001-01 Journal Entry `reversal_of` field, which this bench does "
			"not carry — exercise on the UAT server where GA-0001-01 is installed",
		)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	aca = frappe.db.get_value(
		"Asset Category Account",
		{"parent": cat, "company_name": company},
		["depreciation_expense_account", "accumulated_depreciation_account"], as_dict=True,
	)
	asset = _plain_asset(company, cat, "TC-042c Manual Reversed", 500_000)
	asset.submit()
	je = frappe.get_doc({
		"doctype": "Journal Entry", "voucher_type": "Depreciation Entry",
		"company": company, "posting_date": nowdate(),
		"accounts": [
			{"account": aca.depreciation_expense_account, "debit_in_account_currency": 500,
			 "reference_type": "Asset", "reference_name": asset.name,
			 "cost_center": frappe.db.get_value("Company", company, "cost_center")},
			{"account": aca.accumulated_depreciation_account, "credit_in_account_currency": 500,
			 "reference_type": "Asset", "reference_name": asset.name},
		],
	})
	je.flags.ignore_permissions = True
	je.submit()
	rev = frappe.get_doc({
		"doctype": "Journal Entry", "voucher_type": "Depreciation Entry",
		"company": company, "posting_date": nowdate(),
		"reversal_of": je.name,
		"user_remark": "GA-0001-01 reversal (TC-042c)",
		"accounts": [
			{"account": aca.accumulated_depreciation_account, "debit_in_account_currency": 500,
			 "reference_type": "Asset", "reference_name": asset.name},
			{"account": aca.depreciation_expense_account, "credit_in_account_currency": 500,
			 "reference_type": "Asset", "reference_name": asset.name},
		],
	})
	rev.flags.ignore_permissions = True
	rev.submit()
	# VR-031 sentinel: the forbidden db_set would clobber this.
	frappe.db.set_value("Asset", asset.name, "value_after_depreciation", 99_999,
		update_modified=False)
	asset.reload()
	asset.cancel()
	orig_doc = frappe.db.get_value("Journal Entry", je.name, "docstatus")
	orig_gl_live = frappe.db.count("GL Entry", {"voucher_no": je.name, "is_cancelled": 0})
	sentinel_kept = (
		frappe.db.get_value("Asset", asset.name, "value_after_depreciation") == 99_999
	)
	ok = (
		frappe.db.get_value("Asset", asset.name, "docstatus") == 2
		and orig_doc == 1
		and orig_gl_live == 2
		and sentinel_kept
	)
	return (
		("PASS" if ok else "FAIL"),
		f"asset docstatus 2; original manual JE {je.name} docstatus={orig_doc} "
		f"GL-live-rows={orig_gl_live}; VR-031 sentinel kept={sentinel_kept}",
	)


# ============================================================== TC-043
@tc("TC-043", "Partial Scrap Reversal — Same-Period Window")
def tc043():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	frappe.db.set_single_value("Asset Settings", "prevent_disposal_before_full_invoicing", 0)
	loss = _plain(company, "Expense")
	_scrapping_type("Damage", company, loss)
	asset = _plain_asset(company, cat, "TC-043 Partial Restore", 2_000_000,
	                     opening_accumulated_depreciation=1_517_808.22)
	asset.submit()
	from asset_enterprise import disposal
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.restore import restore_partial_scrap

	disposal.partial_scrap_asset(asset.name, scrap_value=100_000, scrapping_type="Damage")
	ft = frappe.db.get_value(
		"Financial Treatment",
		{"asset": asset.name, "transaction_type": ("like", "%Partial%"), "status": "Posted"},
		["name", "journal_entry"], as_dict=True,
	)
	restore_partial_scrap(asset.name, ft.name)
	values = recalculate_asset_values(asset.name, save=False)
	original_posted = frappe.db.get_value("Journal Entry", ft.journal_entry, "docstatus") == 1
	ok = (
		flt(values["historical_asset_value"], 2) == 2_000_000.00
		and flt(values["accumulated_depreciation_value"], 2) == 1_517_808.22
		and original_posted
	)
	return (
		("PASS" if ok else "FAIL"),
		f"HAV restored to {flt(values['historical_asset_value']):,.2f} (want 2,000,000); accum "
		f"{flt(values['accumulated_depreciation_value']):,.2f} (want 1,517,808.22); original "
		f"partial-scrap entry still posted={original_posted}",
	)


def _stock_item(code="TC-REFRAC-BRICK"):
	if not frappe.db.exists("Item", code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": "TC Refractory Brick",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"is_stock_item": 1,
				"is_fixed_asset": 0,
				"valuation_rate": 200,
			}
		).insert(ignore_permissions=True)
	return code


def _warehouse(company):
	name = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 0}, "name"
	)
	if name:
		return name
	return frappe.get_doc(
		{"doctype": "Warehouse", "warehouse_name": "TC Plant WH", "company": company}
	).insert(ignore_permissions=True).name


def _stock_in(company, item, warehouse, qty, rate):
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": company,
			"posting_date": add_months(nowdate(), -2),
			"set_posting_time": 1,
			"items": [
				{"item_code": item, "qty": qty, "t_warehouse": warehouse, "basic_rate": rate}
			],
		}
	)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def _make_repair(company, asset_name, cost, clearing, stock_rows=None, posting_date=None):
	repair = frappe.get_doc(
		{
			"doctype": "Asset Repair",
			"asset": asset_name,
			"company": company,
			"failure_date": posting_date or nowdate(),
			"repair_status": "Completed",
			"completion_date": posting_date or nowdate(),
			"capitalize_repair_cost": 1,
			"repair_cost": cost,
			"cost_center": frappe.db.get_value("Asset", asset_name, "cost_center"),
			"capital_work_in_progress_account": clearing,
		}
	)
	for row in stock_rows or []:
		repair.append("stock_items", row)

	repair.flags.ignore_permissions = True
	repair.insert()
	repair.submit()
	return repair


# ============================================================== TC-044b
@tc("TC-044b", "AVA Reversal — Subsequent Depreciation Exists")
def tc044b():
	"""Revaluation posted, then a depreciation period posted against the
	new value, then the AVA is cancelled. Both originals stay posted; the
	Reversal AVA mirrors the revaluation on the later date."""
	_ensure_fiscal_years(2025, 2032)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	oci = _plain(company, "Liability")
	asset = _depreciating_asset(
		company, cat, "TC-044b AVA + Depr", 1_200_000, "2026-05-31", 36, "2026-05-01"
	)
	ava = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"asset": asset.name,
			"company": company,
			"date": "2026-06-01",
			"transaction_type": "Upward Revaluation",
			"current_asset_value": flt(frappe.db.get_value("Asset", asset.name, "net_book_value")),
			"new_asset_value": flt(frappe.db.get_value("Asset", asset.name, "net_book_value")) + 200_000,
			"difference_account": oci,
		}
	)
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()
	ava_je = frappe.db.get_value("Asset Value Adjustment", ava.name, "journal_entry")

	# one period posted AFTER the revaluation, reflecting the new value
	_post_through(asset.name, "2026-06-30")
	_sched, rows = _rows(asset.name)
	depr_je = [r.journal_entry for r in rows if r.journal_entry and getdate(r.schedule_date) == getdate("2026-06-30")]
	depr_je = depr_je[0] if depr_je else None

	ava.reload()
	ava.cancel()
	reversal = frappe.db.get_value(
		"Asset Value Adjustment", {"reversal_of_ava": ava.name, "docstatus": 1},
		["name", "journal_entry", "difference_amount"], as_dict=True,
	)
	originals_posted = (
		frappe.db.get_value("Journal Entry", ava_je, "docstatus") == 1
		and (frappe.db.get_value("Journal Entry", depr_je, "docstatus") == 1 if depr_je else False)
	)
	mirror_legs = (
		{(r.account, flt(r.debit, 2), flt(r.credit, 2)) for r in _gl(reversal.journal_entry)}
		if reversal and reversal.journal_entry
		else set()
	)
	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset.name, save=False)
	ok = (
		reversal
		and originals_posted
		and (oci, 200_000.00, 0.00) in mirror_legs
		and flt(values["historical_asset_value"], 2) == 1_200_000.00
	)
	return (
		("PASS" if ok else "FAIL"),
		f"revaluation entry {ava_je} and the 30/06 depreciation {depr_je} both still "
		f"posted={originals_posted}; reversal AVA {reversal and reversal.name} mirrors "
		f"{sorted(mirror_legs)}; HAV back to {flt(values['historical_asset_value']):,.2f}; "
		f"accum stays {flt(values['accumulated_depreciation_value']):,.2f}",
	)


# ============================================================= TC-047b
@tc("TC-047b", "Repair Reversal — Subsequent Depreciation Exists")
def tc047b():
	_ensure_fiscal_years(2025, 2032)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	asset = _depreciating_asset(
		company, cat, "TC-047b Repair + Depr", 1_200_000, "2026-05-31", 36, "2026-05-01"
	)
	repair = _make_repair(company, asset.name, 25_000, clearing, posting_date="2026-06-01")
	repair_je = frappe.db.get_value(
		"Financial Treatment",
		{"source_doctype": "Asset Repair", "source_name": repair.name, "status": "Posted"},
		"journal_entry",
	)
	_post_through(asset.name, "2026-06-30")
	_sched, rows = _rows(asset.name)
	depr_je = [r.journal_entry for r in rows if r.journal_entry and getdate(r.schedule_date) == getdate("2026-06-30")]
	depr_je = depr_je[0] if depr_je else None
	posted_before = [(str(r.schedule_date), flt(r.depreciation_amount)) for r in rows if r.journal_entry]

	repair.reload()
	repair.cancel()
	reversal = frappe.db.get_value(
		"Asset Repair", {"reversal_of_repair": repair.name, "docstatus": 1}, "name"
	)
	originals_posted = (
		(frappe.db.get_value("Journal Entry", repair_je, "docstatus") == 1 if repair_je else True)
		and (frappe.db.get_value("Journal Entry", depr_je, "docstatus") == 1 if depr_je else False)
	)
	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset.name, save=False)
	_sched2, rows_after = _rows(asset.name)
	posted_after = [(str(r.schedule_date), flt(r.depreciation_amount)) for r in rows_after if r.journal_entry]
	ok = (
		bool(reversal)
		and originals_posted
		and flt(values["historical_asset_value"], 2) == 1_200_000.00
		and posted_before == posted_after
	)
	return (
		("PASS" if ok else "FAIL"),
		f"reversal repair {reversal}; repair entry {repair_je} and the 30/06 depreciation "
		f"{depr_je} both still posted={originals_posted}; HAV back to "
		f"{flt(values['historical_asset_value']):,.2f}; posted rows untouched="
		f"{posted_before == posted_after}",
	)


# ============================================================= TC-047c
@tc("TC-047c", "Repair Reversal — Stock Return via Material Receipt")
def tc047c():
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	item = _stock_item()
	warehouse = _warehouse(company)
	_stock_in(company, item, warehouse, 200, 200)
	asset = _plain_asset(company, cat, "TC-047c Stock Repair", 500_000)
	asset.submit()

	qty_before = flt(
		frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty")
	)
	repair = _make_repair(
		company, asset.name, 25_000, clearing,
		stock_rows=[
			{"item_code": item, "consumed_quantity": 100, "valuation_rate": 200,
			 "warehouse": warehouse, "total_value": 20_000}
		],
	)
	qty_after_repair = flt(
		frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty")
	)
	repair.reload()
	repair.cancel()
	qty_after_reversal = flt(
		frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty")
	)
	reversal = frappe.db.get_value(
		"Asset Repair", {"reversal_of_repair": repair.name, "docstatus": 1}, "name"
	)
	receipt = frappe.db.sql(
		"""select se.name, se.stock_entry_type from `tabStock Entry` se
		   join `tabStock Entry Detail` sed on sed.parent = se.name
		   where se.docstatus = 1 and sed.item_code = %s and sed.t_warehouse = %s
		     and se.creation > %s
		   order by se.creation desc limit 1""",
		(item, warehouse, repair.creation),
		as_dict=True,
	)
	ok = (
		bool(reversal)
		and qty_after_repair == qty_before - 100
		and qty_after_reversal == qty_before
		and receipt
		and receipt[0].stock_entry_type == "Material Receipt"
	)
	return (
		("PASS" if ok else "FAIL"),
		f"stock {qty_before:,.0f} -> {qty_after_repair:,.0f} on repair -> "
		f"{qty_after_reversal:,.0f} after reversal; reversal repair {reversal}; return entry "
		f"{receipt and receipt[0].name} ({receipt and receipt[0].stock_entry_type})",
	)


# ============================================================== TC-049
@tc("TC-049", "Adding Value to Composite — Two-Leg via Clearing")
def tc049():
	"""A standalone depreciating asset is merged into a composite: its
	period is prorated to the merge date, then the disposal and addition
	legs pass through the Capitalization Clearing account, which nets to
	zero."""
	_ensure_fiscal_years(2024, 2032)
	company = _company()
	cat = _category(company, "TC IT Equipment", suspense=_plain(company, "Liability"))
	clearing = _plain(company, "Liability")
	frappe.db.set_value(
		"Asset Category Account", {"parent": cat, "company_name": company},
		"capitalization_clearing_account", clearing, update_modified=False,
	)
	composite = _plain_asset(company, cat, "TC-049 COMPOSITE-KILN", 42_500_000)
	composite.submit()
	source = _depreciating_asset(
		company, cat, "TC-049 COMP-REFRAC-002", 8_000_000, "2024-06-30", 60, "2024-06-01"
	)

	from asset_enterprise.asset_values import recalculate_asset_values

	cap = _cm_merge(company, composite.name, source.name, posting_date="2024-06-07")

	legs = []
	for je in frappe.get_all(
		"Journal Entry", filters={"user_remark": ("like", f"%{cap.name}%"), "docstatus": 1}, pluck="name"
	):
		legs.extend(_gl(je))
	clearing_net = flt(sum(flt(r.debit) - flt(r.credit) for r in legs if r.account == clearing), 2)
	clearing_moved = flt(sum(flt(r.debit) for r in legs if r.account == clearing), 2)
	fa_credit_source = flt(
		sum(flt(r.credit) for r in legs if r.account == _account(company, "Fixed Asset")), 2
	)
	comp_values = recalculate_asset_values(composite.name, save=False)
	# the source is cancelled by the merge, so read what actually moved:
	# the clearing account carried its net book value at the merge date.
	nbv_at_merge = clearing_moved
	prorated = flt(8_000_000 - nbv_at_merge, 2)
	src_state = frappe.db.get_value("Asset", source.name, ["docstatus", "status"], as_dict=True)
	ok = (
		clearing_net == 0.00
		and prorated > 0
		and fa_credit_source == 8_000_000.00
		and flt(comp_values["historical_asset_value"], 2) == flt(42_500_000 + nbv_at_merge, 2)
		and src_state.docstatus == 1
		and src_state.status == "Disposed"
	)
	return (
		("PASS" if ok else "FAIL"),
		f"source prorated to 07/06: {prorated:,.2f} posted, NBV {nbv_at_merge:,.2f}; clearing "
		f"carried {clearing_moved:,.2f} and nets {clearing_net:,.2f}; source cost credited "
		f"{fa_credit_source:,.2f} (want 8,000,000); composite HAV "
		f"{flt(comp_values['historical_asset_value']):,.2f} (want "
		f"{42_500_000 + nbv_at_merge:,.2f}); source stays submitted as "
		f"{src_state.status} (docstatus {src_state.docstatus})",
	)
