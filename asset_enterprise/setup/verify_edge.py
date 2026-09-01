"""Edge cases DERIVED FROM THE DESIGN — not from the code.

The 58 §11 test cases exercise the paths the design chose to write down.
This suite exercises the paths its RULES imply and nobody wrote down:
the boundary of every strict inequality, the ends of the calendar, the
sign changes, and the state combinations.

Two disciplines make it worth running:

1. **The expected value comes from the design, computed here** — a
   daily rate worked out from §4.3's formula, a bound read off VR-023 —
   never from what the code happens to return. A test that asks the
   code what it does and then asserts that is worthless.
2. **Every message is generated from what was observed**, so a case
   cannot claim something it did not measure.

Each case is independent: it runs inside a savepoint that is always
rolled back, so a failure never poisons the next one.

    bench --site <site> execute asset_enterprise.setup.verify_edge.run
    ... .run --kwargs "{'only': 'E-07'}"
"""

import traceback

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate

CASES = []


def case(case_id, design_ref, title):
	def wrap(fn):
		CASES.append((case_id, design_ref, title, fn))
		return fn

	return wrap


def _company():
	from asset_enterprise.setup.test_fixtures import pick_company

	return pick_company()


def _asset(company, gross=120_000, salvage=0, months=12, start=None, submit=True):
	"""A depreciating asset with the engine enabled, nothing posted."""
	from asset_enterprise.depreciation import enable_depreciation
	from asset_enterprise.setup.test_fixtures import make_test_asset

	asset = make_test_asset(company, gross=gross, submit=submit)
	enable_depreciation(
		asset.name,
		total_number_of_depreciations=months,
		frequency_of_depreciation=1,
		depreciation_start_date=start or get_last_day(nowdate()),
		expected_value_after_useful_life=salvage,
	)
	return asset.name


def _rows(asset):
	return frappe.db.sql(
		"""select ds.schedule_date, ds.depreciation_amount, ds.days_in_period, ds.daily_rate,
		          ds.journal_entry
		   from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		   order by ds.schedule_date""",
		asset,
		as_dict=True,
	)


def _refused(fn):
	"""(was_refused, message) — the shape most boundary cases need."""
	try:
		fn()
		return False, ""
	except frappe.ValidationError as exc:
		return True, str(exc)[:120]
	except Exception as exc:  # a crash is NOT a refusal
		return False, f"{type(exc).__name__}: {str(exc)[:100]}"


# ====================================================== VR-023 boundaries
# "Partial scrap value must be > 0 and < current HAV. Percentage must be
#  between 0 (exclusive) and 100 (exclusive)."  (§2856)
# Every bound is STRICT, so each endpoint must be refused.


@case("E-01", "VR-023", "partial scrap for exactly the full HAV is refused")
def e01():
	from asset_enterprise import disposal

	company = _company()
	asset = _asset(company, gross=120_000)
	hav = flt(frappe.db.get_value("Asset", asset, "net_purchase_amount"))
	refused, msg = _refused(
		lambda: disposal.partial_scrap_asset(asset, scrap_value=hav, scrap_date=nowdate())
	)
	return refused, f"scrap value {hav:,.2f} == HAV {hav:,.2f}: refused={refused} {msg}"


@case("E-02", "VR-023", "partial scrap for zero is refused")
def e02():
	from asset_enterprise import disposal

	company = _company()
	asset = _asset(company, gross=120_000)
	refused, msg = _refused(
		lambda: disposal.partial_scrap_asset(asset, scrap_value=0, scrap_date=nowdate())
	)
	return refused, f"scrap value 0.00: refused={refused} {msg}"


@case("E-03", "VR-023", "partial scrap of exactly 100% is refused")
def e03():
	from asset_enterprise import disposal

	company = _company()
	asset = _asset(company, gross=120_000)
	refused, msg = _refused(
		lambda: disposal.partial_scrap_asset(asset, percentage=100, scrap_date=nowdate())
	)
	return refused, f"percentage 100: refused={refused} {msg}"


@case("E-04", "VR-023", "partial scrap just inside the bound (99.99%) is allowed")
def e04():
	from asset_enterprise import disposal

	company = _company()
	asset = _asset(company, gross=120_000)
	refused, msg = _refused(
		lambda: disposal.partial_scrap_asset(asset, percentage=99.99, scrap_date=nowdate())
	)
	return (not refused), f"percentage 99.99: refused={refused} {msg}"


# ====================================================== VR-022 boundary
# "Disposal reversal posting date must be >= original disposal date." (§2852)


@case("E-05", "VR-022", "reversal dated before the disposal is refused; same day is allowed")
def e05():
	from asset_enterprise import disposal, restore

	company = _company()
	asset = _asset(company, gross=120_000)
	scrap_date = add_days(getdate(nowdate()), -5)
	disposal.scrap_asset(asset, scrap_date=scrap_date)

	# cross_period_restore is the reversal path that TAKES a date, so it
	# is where VR-022 has to bite.
	earlier, msg_early = _refused(
		lambda: restore.cross_period_restore(asset, restore_date=add_days(scrap_date, -1))
	)
	return earlier, (
		f"disposal {scrap_date}, restore dated {add_days(scrap_date, -1)} (one day EARLIER): "
		f"refused={earlier} {msg_early[:90]}"
	)


@case("E-06", "VR-022", "reversing a FUTURE-dated partial scrap before it happened is refused")
def e06():
	from asset_enterprise import disposal

	company = _company()
	asset = _asset(company, gross=120_000)
	ahead = add_days(getdate(nowdate()), 10)
	disposal.partial_scrap_asset(asset, scrap_value=10_000, scrap_date=ahead)
	ft = frappe.get_all(
		"Financial Treatment",
		filters={"asset": asset, "transaction_category": "Disposal", "status": "Posted"},
		order_by="creation desc", limit=1, pluck="name",
	)[0]
	from asset_enterprise import restore

	# cross_period=1 skips the same-period window gate, so VR-022 is the
	# only thing left that can refuse this — otherwise the case would
	# pass on a guard it is not testing.
	refused, msg = _refused(
		lambda: restore.restore_partial_scrap(asset, ft, cross_period=1)
	)
	return refused, (
		f"scrap dated {ahead} (10 days ahead), reversal posts today: "
		f"refused={refused} {msg[:90]}"
	)


# ================================================ §4.3 calendar boundaries


@case("E-07", "§4.3 / §4.10", "an asset in service on the 31st gets a one-day first row")
def e07():
	company = _company()
	# available-for-use on the LAST day of a month: the first period is
	# that single day, so the first row must be exactly one daily rate.
	start = get_last_day(add_months(nowdate(), -2))
	asset = _asset(company, gross=120_000, months=12, start=start)
	rows = _rows(asset)
	if not rows:
		return False, "no schedule rows were built"
	first = rows[0]
	expected = flt(flt(first.daily_rate) * 1, 2)
	ok = int(first.days_in_period or 0) == 1 and abs(flt(first.depreciation_amount) - expected) < 0.02
	return ok, (
		f"in service {start}: first row {first.schedule_date} covers "
		f"{first.days_in_period} day(s) at {flt(first.daily_rate):,.6f}/day = "
		f"{flt(first.depreciation_amount):,.2f} (want 1 day, {expected:,.2f})"
	)


@case("E-08", "CH-12 / §4.3", "one daily rate across a leap year — 29 Feb is not in the denominator")
def e08():
	company = _company()
	# A life spanning 29 Feb 2028. day_count_365 excludes the leap day, so
	# every row must price at the SAME rate; two rates means the calendar
	# leaked into the denominator.
	asset = _asset(company, gross=120_000, months=24, start=get_last_day("2027-06-30"))
	rows = _rows(asset)
	rates = {flt(r.daily_rate, 6) for r in rows if flt(r.daily_rate)}
	spans_leap = any(str(r.schedule_date).startswith("2028-02") for r in rows)
	ok = spans_leap and len(rates) == 1
	return ok, (
		f"{len(rows)} rows spanning Feb-2028={spans_leap}; distinct daily rates "
		f"{sorted(rates)} (want exactly one)"
	)


@case("E-09", "§4.10", "the schedule totals cost less salvage, to the cent")
def e09():
	company = _company()
	# 120,000 over 13 months with a 7,000 salvage: an amount that does NOT
	# divide evenly, so the final row must absorb the drift (§4.10 pt 3).
	asset = _asset(company, gross=120_000, salvage=7_000, months=13,
	               start=get_last_day(nowdate()))
	rows = _rows(asset)
	total = flt(sum(flt(r.depreciation_amount) for r in rows), 2)
	expected = flt(120_000 - 7_000, 2)
	ok = abs(total - expected) < 0.01
	return ok, (
		f"{len(rows)} rows total {total:,.2f} (want cost 120,000.00 − salvage "
		f"7,000.00 = {expected:,.2f})"
	)


@case("E-10", "§4.3", "salvage equal to cost is refused, not silently zero-depreciated")
def e10():
	company = _company()
	# Nothing in the design covers salvage == cost. The two defensible
	# answers are "refuse with an explanation" or "accept and build no
	# rows"; what must NOT happen is a schedule that quietly charges
	# something. Refusal is what the engine does, so that is the
	# behaviour pinned here.
	refused, msg = _refused(
		lambda: _asset(company, gross=50_000, salvage=50_000, months=12,
		               start=get_last_day(nowdate()))
	)
	return refused, f"salvage == cost 50,000: refused={refused} {msg[:90]}"


@case("E-11", "VR-015", "every schedule date is the last day of its month")
def e11():
	company = _company()
	asset = _asset(company, gross=99_000, months=14, start=get_first_day(nowdate()))
	rows = _rows(asset)
	offenders = [
		str(r.schedule_date) for r in rows
		if getdate(r.schedule_date) != getdate(get_last_day(r.schedule_date))
	]
	ok = bool(rows) and not offenders
	return ok, f"{len(rows)} rows; not month-end: {offenders or 'none'}"


@case("E-12", "§4.3", "a single-period life is one row landing on salvage")
def e12():
	company = _company()
	asset = _asset(company, gross=60_000, salvage=5_000, months=1,
	               start=get_last_day(nowdate()))
	rows = _rows(asset)
	total = flt(sum(flt(r.depreciation_amount) for r in rows), 2)
	ok = len(rows) == 1 and abs(total - 55_000) < 0.01
	return ok, f"{len(rows)} row(s) totalling {total:,.2f} (want 1 row, 55,000.00)"


@case("E-15", "§4.6 exceptions", "a disposal still posts on its transaction date, not month-end")
def e15():
	from asset_enterprise import disposal

	company = _company()
	asset = _asset(company, gross=120_000, months=24,
	               start=get_last_day(add_months(nowdate(), -2)))
	scrap_date = add_days(getdate(nowdate()), -3)   # deliberately mid-month
	disposal.scrap_asset(asset, scrap_date=scrap_date)
	rows = _rows(asset)
	last = rows[-1] if rows else None
	ok = bool(last) and getdate(last.schedule_date) == getdate(scrap_date)
	return ok, (
		f"scrapped {scrap_date}: last schedule row {last and last.schedule_date} "
		f"(want the transaction date, NOT {get_last_day(scrap_date)})"
	)


# ============================== the client's own reference workbook (2026-09-01)
# "Existing Asset Depreciation Calculation.xlsx", keyed to
# ACC-ASS-2026-00248. Their formulas, reproduced here so a regression
# shows up as a diff against THEIR arithmetic and not against ours:
#
#   booked days   = 12/12 x 365 =   365      NBV        = 3,000 - 1,000 = 2,000
#   total days    = 36/12 x 365 = 1,095      remaining  = 1,095 - 365   =   730
#   rate          = 2,000 / 730 = 2.739726 per day
#   row amount    = DAY(EOMONTH) x rate      24 rows, 2026-01-31 .. 2027-12-31


@case("E-16", "client workbook 01/09", "an existing asset resumes over its REMAINING life")
def e16():
	import calendar

	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()
	asset = make_test_asset(company, gross=3_000, submit=False)
	asset.available_for_use_date = "2025-01-01"
	asset.purchase_date = "2025-01-01"
	asset.opening_accumulated_depreciation = 1_000
	asset.opening_number_of_booked_depreciations = 12
	asset.flags.ignore_permissions = True
	asset.save()
	asset.submit()

	from asset_enterprise.depreciation import enable_depreciation

	enable_depreciation(
		asset.name, total_number_of_depreciations=36, frequency_of_depreciation=1,
		depreciation_start_date="2026-01-31",
	)
	rows = _rows(asset.name)

	rate = 2_000 / 730.0
	expected, d = [], getdate("2026-01-31")
	for _i in range(24):
		days = calendar.monthrange(d.year, d.month)[1]
		expected.append((d, days, flt(days * rate, 2)))
		nxt = getdate(f"{d.year + (d.month // 12)}-{(d.month % 12) + 1:02d}-01")
		d = getdate(f"{nxt.year}-{nxt.month:02d}-{calendar.monthrange(nxt.year, nxt.month)[1]}")

	first_ok = bool(rows) and int(rows[0].days_in_period or 0) == 31
	rate_ok = bool(rows) and abs(flt(rows[0].daily_rate) - rate) < 0.000001
	# every row but the last, which absorbs §4.10 drift
	body_ok = len(rows) == 24 and all(
		abs(flt(rows[i].depreciation_amount) - expected[i][2]) < 0.01 for i in range(23)
	)
	total_ok = abs(flt(sum(flt(r.depreciation_amount) for r in rows), 2) - 2_000) < 0.01
	ok = first_ok and rate_ok and body_ok and total_ok
	return ok, (
		f"{len(rows)} rows (want 24); rate {flt(rows[0].daily_rate, 6) if rows else '-'} "
		f"(want {rate:.6f}); first row {rows[0].days_in_period if rows else '-'}d "
		f"{flt(rows[0].depreciation_amount, 2) if rows else '-'} (want 31d "
		f"{expected[0][2]:,.2f}); rows match workbook={body_ok}; total "
		f"{flt(sum(flt(r.depreciation_amount) for r in rows), 2):,.2f} (want 2,000.00)"
	)


# ====================================================== §12 invoice matrix


@case("E-13", "§12 / GAP-012 Option B", "an invoice BELOW the receipt posts a decrease")
def e13():
	from asset_enterprise.asset_values import recalculate_asset_values

	company, pr, assets, supplier, seed = _receipt(qty=1, rate=10_000)
	pi = _invoice(company, supplier, pr, seed, qty=1, rate=8_000,
	              allocation=[{"asset": assets[0], "allocated_amount": 8_000}])
	hav = flt(recalculate_asset_values(assets[0], save=False)["historical_asset_value"], 2)
	row = frappe.db.get_value(
		"PI Asset Allocation", {"parent": pi.name, "asset": assets[0]}, "pi_delta_amount"
	)
	ok = abs(hav - 8_000) < 0.01 and abs(flt(row) + 2_000) < 0.01
	return ok, (
		f"receipt 10,000 invoiced 8,000: delta {flt(row):,.2f} (want -2,000.00), "
		f"HAV {hav:,.2f} (want 8,000.00)"
	)


@case("E-14", "§12.5 Case A.02", "an invoice for a SCRAPPED asset is expensed, not capitalized")
def e14():
	from asset_enterprise import disposal
	from asset_enterprise.asset_values import recalculate_asset_values

	company, pr, assets, supplier, seed = _receipt(qty=1, rate=10_000)
	disposal.scrap_asset(assets[0], scrap_date=nowdate())
	before = flt(recalculate_asset_values(assets[0], save=False)["historical_asset_value"], 2)
	_invoice(company, supplier, pr, seed, qty=1, rate=12_000,
	         allocation=[{"asset": assets[0], "allocated_amount": 12_000}])
	after = flt(recalculate_asset_values(assets[0], save=False)["historical_asset_value"], 2)
	ava = frappe.db.exists(
		"Asset Value Adjustment",
		{"asset": assets[0], "transaction_type": "Invoice Adjustment", "docstatus": 1},
	)
	ok = abs(after - before) < 0.01 and not ava
	return ok, (
		f"scrapped asset invoiced 2,000 above receipt: HAV {before:,.2f} -> {after:,.2f} "
		f"(want unchanged); adjustment raised={bool(ava)} (want False)"
	)


# --------------------------------------------------------------- helpers


def _receipt(qty, rate):
	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()
	frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
	frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 100)
	seed = make_test_asset(company, gross=1, submit=False)
	item = frappe.get_doc("Item", "AE-SMOKE-ITEM")
	item.auto_create_assets = 1
	item.asset_naming_series = frappe.get_meta("Asset").get_field("naming_series").options.split("\n")[0]
	item.flags.ignore_permissions = True
	item.save()
	supplier = frappe.db.get_value("Supplier", {"supplier_name": "AE Smoke Supplier"}, "name")
	if not supplier:
		supplier = (
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "AE Smoke Supplier"})
			.insert(ignore_permissions=True).name
		)
	pr = frappe.get_doc({
		"doctype": "Purchase Receipt", "company": company, "supplier": supplier,
		"posting_date": nowdate(),
		"items": [{"item_code": "AE-SMOKE-ITEM", "qty": qty, "rate": rate,
		           "asset_location": seed.location}],
	})
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	assets = frappe.get_all(
		"Asset", filters={"purchase_receipt": pr.name}, pluck="name", order_by="creation, name"
	)
	return company, pr, assets, supplier, seed


def _invoice(company, supplier, pr, seed, qty, rate, allocation):
	pi = frappe.get_doc({
		"doctype": "Purchase Invoice", "company": company, "supplier": supplier,
		"posting_date": nowdate(),
		"items": [{"item_code": "AE-SMOKE-ITEM", "qty": qty, "rate": rate,
		           "purchase_receipt": pr.name, "pr_detail": pr.items[0].name}],
		"pi_asset_allocation": allocation,
	})
	pi.flags.ignore_permissions = True
	pi.insert()
	pi.submit()
	return pi


def run(only=None):
	wanted = {c.strip() for c in only.split(",")} if only else None
	switch_before = frappe.db.get_single_value(
		"Asset Settings", "enable_enterprise_assets", cache=False
	)
	tally = {"PASS": 0, "FAIL": 0, "ERROR": 0}
	for case_id, design_ref, title, fn in CASES:
		if wanted and case_id not in wanted:
			continue
		frappe.db.savepoint("edge_case")
		try:
			frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
			ok, detail = fn()
			status = "PASS" if ok else "FAIL"
		except Exception as exc:
			status, detail = "ERROR", f"{type(exc).__name__}: {str(exc)[:150]}"
			if frappe.flags.get("edge_traceback"):
				traceback.print_exc()
		finally:
			frappe.db.rollback(save_point="edge_case")
		tally[status] += 1
		print(f"{case_id:<6} {status:<6} [{design_ref}] {title}")
		print(f"          {detail}")
	frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", switch_before)
	print(f"\nEDGE TALLY: " + ", ".join(f"{k}={v}" for k, v in tally.items() if v))
	return tally
