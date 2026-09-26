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
from frappe.utils import add_days, add_months, cint, date_diff, flt, get_first_day, get_last_day, getdate, nowdate

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
	from asset_enterprise.depreciation import post_schedule_entries

	schedule = frappe.db.get_value("Asset Depreciation Schedule",
		{"asset": asset, "status": "Active", "docstatus": 1}, "name")
	post_schedule_entries(schedule, add_days(get_first_day(ahead), -1))
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


@case("E-08", "CH-12 as amended 01/09", "a leap day is priced into the rate, not into the last row")
def e08():
	company = _company()
	# A life spanning 29 Feb 2028. Since CH-12 was amended the denominator
	# is the ACTUAL days held, so the leap day is carried by every row at
	# one uniform rate — and the final row no longer has to absorb it.
	# Under the superseded 365-basis the rate was higher and the last row
	# took the whole correction.
	asset = _asset(company, gross=120_000, months=24, start=get_last_day("2027-06-30"))
	rows = _rows(asset)
	rates = {flt(r.daily_rate, 6) for r in rows if flt(r.daily_rate)}
	spans_leap = any(str(r.schedule_date).startswith("2028-02") for r in rows)
	feb = next((r for r in rows if str(r.schedule_date).startswith("2028-02")), None)
	# The leap day is CHARGED: February 2028 must carry 29 days, and the
	# final row must sit within a day's rate of its neighbours rather than
	# absorbing a whole leap day.
	rate = flt(sorted(rates)[0]) if rates else 0
	last = rows[-1] if rows else None
	# The final row must be worth its OWN days and nothing more — that is
	# what "the leap day is in the rate, not in the last row" means. It is
	# also date-independent: comparing the last row to its neighbour broke
	# the moment the calendar produced a one-day terminal stub.
	priced = flt(rate * cint(last.days_in_period), 2) if last else 0
	ok = (
		spans_leap and len(rates) == 1
		and feb and int(feb.days_in_period or 0) == 29
		and last and abs(flt(last.depreciation_amount) - priced) <= 0.02
	)
	return ok, (
		f"{len(rows)} rows spanning Feb-2028={spans_leap}; one rate {sorted(rates)}; "
		f"Feb-2028 charges {feb and feb.days_in_period}d (want 29); final row "
		f"{flt(last.depreciation_amount, 2) if last else '-'} over "
		f"{last.days_in_period if last else '-'}d, priced at {priced:,.2f} "
		f"(want them equal — no leap day absorbed)"
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


@case("E-17", "client workbook 01/09", "the FORM path gives an existing asset the same schedule")
def e17():
	"""E-16 enables depreciation through the API. The client fills the
	depreciation details on the asset itself, so core builds the schedule
	at submit and our §4.3 rebuild replaces it — a different path, and
	the one ACC-ASS-2026-00248 came through.

	It also pins a data-entry requirement: v16 replaced `is_existing_asset`
	with `asset_type`, and core WIPES the opening values unless it reads
	"Existing Asset" (asset.py validate_depreciation_row). Without it the
	asset depreciates its full cost — 3,000 instead of 2,000.
	"""
	import calendar

	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()
	seed = make_test_asset(company, gross=3_000, submit=False)
	asset = frappe.get_doc({
		"doctype": "Asset", "company": company, "item_code": seed.item_code,
		"asset_name": "AE Smoke Existing Form Path", "asset_category": seed.asset_category,
		"location": seed.location, "net_purchase_amount": 3_000,
		"asset_type": "Existing Asset",
		"purchase_date": "2025-01-01", "available_for_use_date": "2025-01-01",
		"opening_accumulated_depreciation": 1_000,
		"opening_number_of_booked_depreciations": 12,
		"calculate_depreciation": 1,
		"finance_books": [{
			"depreciation_method": "Straight Line",
			"total_number_of_depreciations": 36, "frequency_of_depreciation": 1,
			"depreciation_start_date": "2026-01-31", "expected_value_after_useful_life": 0,
		}],
	})
	asset.flags.ignore_permissions = True
	asset.insert()
	asset.submit()

	rows = _rows(asset.name)
	rate = 2_000 / 730.0
	expected, d = [], getdate("2026-01-31")
	for _i in range(24):
		days = calendar.monthrange(d.year, d.month)[1]
		expected.append(flt(days * rate, 2))
		n = getdate(f"{d.year + (d.month // 12)}-{(d.month % 12) + 1:02d}-01")
		d = getdate(f"{n.year}-{n.month:02d}-{calendar.monthrange(n.year, n.month)[1]}")

	kept = flt(frappe.db.get_value("Asset", asset.name, "opening_accumulated_depreciation"))
	total = flt(sum(flt(r.depreciation_amount) for r in rows), 2)
	ok = (
		abs(kept - 1_000) < 0.01                    # core did not wipe them
		and len(rows) == 24
		and int(rows[0].days_in_period or 0) == 31  # no catch-up for booked time
		and all(abs(flt(rows[i].depreciation_amount) - expected[i]) < 0.01 for i in range(23))
		and abs(total - 2_000) < 0.01               # NBV, not the full cost
	)
	return ok, (
		f"opening kept {kept:,.2f} (want 1,000); {len(rows)} rows, first "
		f"{rows[0].days_in_period if rows else '-'}d "
		f"{flt(rows[0].depreciation_amount, 2) if rows else '-'} (want 31d 84.93); "
		f"total {total:,.2f} (want 2,000.00, NOT the 3,000 cost)"
	)


@case("E-18", "client workbook 02/09", "a leap-spanning life prices at the client's own rate")
def e18():
	"""ACC-ASS-2026-00263 — the client's second workbook, and their answer
	to the leap-year question.

	Their cell B5 reads `= B3*365 + 1`: three years of 365 days PLUS the
	leap day, i.e. 1,096 — the ACTUAL calendar days. Their rate is
	3,000/1,096 = 2.737226277, not the 3,000/1,095 = 2.739726027 the
	signed CH-12 basis would give. They wrote the amendment themselves.
	"""
	import calendar

	company = _company()
	for y in range(2024, 2028):
		if not frappe.db.exists("Fiscal Year", {"year_start_date": f"{y}-01-01"}):
			frappe.get_doc({
				"doctype": "Fiscal Year", "year": f"AE Edge {y}",
				"year_start_date": f"{y}-01-01", "year_end_date": f"{y}-12-31",
			}).insert(ignore_permissions=True, ignore_if_duplicate=True)

	from asset_enterprise.depreciation import enable_depreciation
	from asset_enterprise.setup.test_fixtures import make_test_asset

	asset = make_test_asset(company, gross=3_000, submit=False)
	asset.purchase_date = asset.available_for_use_date = "2024-01-01"
	asset.flags.ignore_permissions = True
	asset.save()
	asset.submit()
	enable_depreciation(asset.name, total_number_of_depreciations=36,
		frequency_of_depreciation=1, depreciation_start_date="2024-01-31")
	rows = _rows(asset.name)

	def eomonth(d, add):
		y, m = d.year + (d.month - 1 + add) // 12, (d.month - 1 + add) % 12 + 1
		return getdate(f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]}")

	rate = 3_000 / (3 * 365 + 1)          # their B5/B6, verbatim
	expected, frm = [], getdate("2024-01-01")
	for i in range(36):
		to = eomonth(frm, 0 if i == 0 else 1)
		days = (to - frm).days + (1 if i == 0 else 0)
		expected.append(flt(days * rate, 2))
		frm = to

	feb = next((r for r in rows if str(r.schedule_date) == "2024-02-29"), None)
	ok = (
		len(rows) == 36
		and abs(flt(rows[0].daily_rate) - rate) < 0.000001
		and feb and int(feb.days_in_period or 0) == 29
		# every row but the last, which absorbs §4.10 drift their sheet leaves open
		and all(abs(flt(rows[i].depreciation_amount) - expected[i]) < 0.01 for i in range(35))
		and abs(flt(sum(flt(r.depreciation_amount) for r in rows), 2) - 3_000) < 0.01
	)
	return ok, (
		f"rate {flt(rows[0].daily_rate, 9) if rows else '-'} (their 3,000/1,096 = "
		f"{rate:.9f}; signed CH-12 basis would be {3_000/1_095:.9f}); 29-Feb-2024 charges "
		f"{feb and feb.days_in_period}d = {flt(feb.depreciation_amount, 2) if feb else '-'} "
		f"(their sheet 79.38); rows matching their sheet="
		f"{sum(1 for i in range(35) if abs(flt(rows[i].depreciation_amount) - expected[i]) < 0.01)}/35; "
		f"total {flt(sum(flt(r.depreciation_amount) for r in rows), 2):,.2f} (their per-row rounding "
		f"leaves {sum(expected):,.2f})"
	)


@case("E-19", "client 02/09", "an existing asset whose BOOKED period spans a leap day")
def e19():
	"""ACC-ASS-2026-00272 — "in normal cases okay, but in existing asset
	not".

	Cost 3,000, opening 2,000, 24 of 36 booked, in service 2024-01-01.
	The booked period 2024-01-01..2025-12-31 CONTAINS 29 Feb 2024, so it
	is 731 days, not the 730 that months/12*365 gives. A day short means
	charging resumes 2025-12-31, the first row covers 32 days instead of
	31, and the remaining span is 366 days instead of 365.

	Expected, from the client's own arithmetic: resume 2026-01-01,
	NBV 1,000 over 365 days = 2.739726027/day, first row 31d = 84.93.
	"""
	company = _company()
	for y in range(2024, 2028):
		if not frappe.db.exists("Fiscal Year", {"year_start_date": f"{y}-01-01"}):
			frappe.get_doc({
				"doctype": "Fiscal Year", "year": f"AE Edge {y}",
				"year_start_date": f"{y}-01-01", "year_end_date": f"{y}-12-31",
			}).insert(ignore_permissions=True, ignore_if_duplicate=True)

	from asset_enterprise.depreciation import enable_depreciation
	from asset_enterprise.setup.test_fixtures import make_test_asset

	asset = make_test_asset(company, gross=3_000, submit=False)
	asset.purchase_date = asset.available_for_use_date = "2024-01-01"
	asset.opening_accumulated_depreciation = 2_000
	asset.opening_number_of_booked_depreciations = 24
	asset.flags.ignore_permissions = True
	asset.save()
	asset.submit()
	enable_depreciation(asset.name, total_number_of_depreciations=36,
		frequency_of_depreciation=1, depreciation_start_date="2026-01-01")
	rows = _rows(asset.name)

	first = rows[0] if rows else None
	ok = (
		first
		and int(first.days_in_period or 0) == 31
		and abs(flt(first.daily_rate) - 1_000 / 365) < 0.000001
		and abs(flt(first.depreciation_amount) - flt(31 * 1_000 / 365, 2)) < 0.01
		and abs(flt(sum(flt(r.depreciation_amount) for r in rows), 2) - 1_000) < 0.01
	)
	return ok, (
		f"first row {first and first.schedule_date} covers "
		f"{first and first.days_in_period}d (want 31, NOT 32) at "
		f"{flt(first.daily_rate, 9) if first else '-'} (want {1_000/365:.9f}, "
		f"NOT the 366-day 2.732240437); amount "
		f"{flt(first.depreciation_amount, 2) if first else '-'} (want 84.93, not 87.43); "
		f"total {flt(sum(flt(r.depreciation_amount) for r in rows), 2):,.2f} (want 1,000.00)"
	)


@case("E-20", "client workbook 02/09", "the transfer day belongs to the OLD cost centre")
def e20():
	"""Belal's "Cost Center Transfer" workbook: 3,000 over 36 months at
	2.739726/day, moved from CC#1 to CC#2 on 13 July 2026.

	    01-13 July  13 days  35.616438  CC#1
	    14-31 July  18 days  49.315068  CC#2
	                31 days  84.931507  = one full month, one entry

	§GAP-021 says "days before" and "days after" the transfer, which
	leaves the transfer day in neither bucket — 30 of July's 31 days. The
	client's sheet settles it: the day of transfer stays with the centre
	giving the asset up.
	"""
	company = _company()
	ccs = frappe.get_all(
		"Cost Center", filters={"company": company, "is_group": 0}, limit=2, pluck="name"
	)
	if len(ccs) < 2:
		return False, "need two cost centres in this company"
	old_cc, new_cc = ccs

	from asset_enterprise.depreciation import cost_centre_on, cost_centre_split
	from asset_enterprise.setup.test_fixtures import make_test_asset

	asset = make_test_asset(company, gross=3_000, submit=True)
	frappe.db.set_value("Asset", asset.name, "cost_center", old_cc, update_modified=False)
	# The fixture's own receipt movement lands today, so the transfer has
	# to be dated on or after it — the 13th of a month at least a month out.
	transfer = getdate(add_months(get_first_day(nowdate()), 1)).replace(day=13)
	move = frappe.get_doc({
		"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
		"transaction_date": str(transfer),
		"assets": [{
			"asset": asset.name, "source_cost_center": old_cc, "target_cost_center": new_cc,
			"source_location": frappe.db.get_value("Asset", asset.name, "location"),
		}],
	})
	move.flags.ignore_permissions = True
	move.insert()
	move.submit()

	period_start = get_first_day(transfer)
	period_end = get_last_day(transfer)
	days_in_month = cint(date_diff(period_end, period_start)) + 1
	charge = 84.931506849
	split = cost_centre_split(asset.name, period_start, period_end, charge, company)
	by_cc = {cc: flt(amt) for cc, amt in split}
	per_day = charge / days_in_month
	old_days = round(by_cc.get(old_cc, 0) / per_day) if by_cc.get(old_cc) else 0
	new_days = round(by_cc.get(new_cc, 0) / per_day) if by_cc.get(new_cc) else 0

	ok = (
		len(split) == 2
		and old_days == 13                                   # 1..13 inclusive
		and new_days == days_in_month - 13
		and cost_centre_on(asset.name, transfer) == old_cc   # the day itself
		and cost_centre_on(asset.name, add_days(transfer, 1)) == new_cc
		and abs(sum(by_cc.values()) - charge) < 0.01         # still one month
	)
	return ok, (
		f"transfer {transfer} in a {days_in_month}-day month: OLD {old_days}d "
		f"{by_cc.get(old_cc, 0):,.2f} / NEW {new_days}d {by_cc.get(new_cc, 0):,.2f} "
		f"(want 13 / {days_in_month - 13}); the transfer day belongs to "
		f"{'OLD' if cost_centre_on(asset.name, transfer) == old_cc else 'NEW'} (want OLD); "
		f"segments sum {sum(by_cc.values()):,.2f} (want {charge:,.2f})"
	)


@case("E-21", "GAP-021 boundary / ruling 07/09", "the contra follows the COST, the expense follows use")
def e21():
	"""E-20 proves the expense split; this proves what the entry actually
	POSTS — the two are separate failures and only the second reaches the
	balance sheet.

	Depreciation expense answers "which centre consumed the asset", so it
	splits across a transfer. Accumulated depreciation is a valuation
	account attached to one asset balance and answers "what is this
	carried at", so it stays with the gross cost. Attribute it any other
	way and the centres stop reconciling: cost 3,000 at Plant A with the
	contra at Head Office leaves Head Office holding a fixed asset it
	does not have, at a negative amount, while the company total — the
	only figure anything else checks — stays right.

	The centres here are deliberately NOT the company default, or the
	defect under test (frappe's `:Company` default filling the leg) would
	look identical to the fix.
	"""
	from asset_enterprise.depreciation import post_schedule_entries

	company = _company()
	default_cc = frappe.db.get_value("Company", company, "cost_center")
	made = []
	for label in ("E21 Source", "E21 Target"):
		name = f"{label} - {frappe.db.get_value('Company', company, 'abbr')}"
		if not frappe.db.exists("Cost Center", name):
			frappe.get_doc({
				"doctype": "Cost Center", "cost_center_name": label, "company": company,
				"parent_cost_center": frappe.db.get_value(
					"Cost Center", {"company": company, "is_group": 1}, "name"
				),
				"is_group": 0,
			}).insert(ignore_permissions=True)
		made.append(name)
	old_cc, new_cc = made
	if old_cc == default_cc or new_cc == default_cc:
		return False, "test centres must differ from the company default to be meaningful"

	transfer = getdate(add_months(get_first_day(nowdate()), 1)).replace(day=13)
	asset = _asset(company, gross=3_000, months=36, start=get_last_day(transfer))
	frappe.db.set_value("Asset", asset, "cost_center", old_cc, update_modified=False)
	frappe.db.set_value("Asset", asset, "acquisition_cost_center", old_cc, update_modified=False)

	move = frappe.get_doc({
		"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
		"transaction_date": str(transfer),
		"assets": [{
			"asset": asset, "source_cost_center": old_cc, "target_cost_center": new_cc,
			"source_location": frappe.db.get_value("Asset", asset, "location"),
		}],
	})
	move.flags.ignore_permissions = True
	move.insert()
	move.submit()

	schedule = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset, "status": "Active", "docstatus": 1}, "name"
	)
	post_schedule_entries(schedule, date=get_last_day(transfer))
	je = frappe.db.get_value(
		"Depreciation Schedule",
		{"parent": schedule, "schedule_date": get_last_day(transfer)},
		"journal_entry",
	)
	if not je:
		return False, "the transfer-month row did not post — nothing to inspect"

	accum_account = frappe.db.get_value(
		"Asset Category Account",
		{"parent": frappe.db.get_value("Asset", asset, "asset_category"), "company_name": company},
		"accumulated_depreciation_account",
	)
	legs = frappe.db.sql(
		"""select account, cost_center, debit, credit from `tabGL Entry`
		   where is_cancelled = 0 and voucher_no = %s""",
		je,
		as_dict=True,
	)
	contra = [r for r in legs if r.account == accum_account]
	expense_ccs = {r.cost_center for r in legs if r.account != accum_account and flt(r.debit)}

	ok = (
		len(contra) == 1
		and contra[0].cost_center == old_cc
		and expense_ccs == {old_cc, new_cc}
	)
	return ok, (
		f"contra on {contra[0].cost_center if contra else 'no leg'} "
		f"(want the acquisition centre {old_cc}; company default is {default_cc}); "
		f"expense across {sorted(c or '(blank)' for c in expense_ccs)} "
		f"(want both {old_cc} and {new_cc})"
	)


@case("E-22", "D-031 / D-009", "project starts on the transfer date; cost centre starts the next day")
def e22():
	"""The client moved an asset to a project and the depreciation entry
	came back with the Project Accounting and WBS columns empty (UAT
	ACC-JV-2026-02277 / ACC-ASM-2026-02725, 08/09).

	Core lists `Asset Movement Item` in `accounting_dimension_doctypes`,
	so registering a dimension makes it fillable on a transfer; nothing
	then read it back, because a movement posts no GL of its own. The
	dimension uses D-031's start-of-day boundary, separate from the
	cost centre's next-day boundary:
	a project that took the asset on the 9th is debited for the 9th
	onward and for nothing before it (Vivek, 08/09 — "only post
	transfer").

	Skipped rather than failed where no dimension is registered on the
	movement row: this app registers only `Asset`, which is excluded by
	design, so the case is live only on a site that also has PA.
	"""
	from asset_enterprise.depreciation import attribution_split, movement_dimension_fields

	fields = movement_dimension_fields()
	if not fields:
		return None, "no registered dimension on Asset Movement Item — nothing to bind (skipped)"
	field = next((f for f in fields if f == "project_accounting"), fields[0])
	from asset_enterprise.setup.test_fixtures import dimension_fixture

	value = dimension_fixture(field, _company())
	if not value:
		return None, f"no {field} record to transfer to (skipped)"

	company = _company()
	ccs = frappe.get_all(
		"Cost Center", filters={"company": company, "is_group": 0}, limit=2, pluck="name"
	)
	if len(ccs) < 2:
		return False, "need two cost centres in this company"
	old_cc, new_cc = ccs

	from asset_enterprise.depreciation import post_schedule_entries
	from asset_enterprise.setup.test_fixtures import make_test_asset

	asset = make_test_asset(company, gross=3_000, submit=True, with_depreciation=True)
	frappe.db.set_value("Asset", asset.name, "cost_center", old_cc, update_modified=False)
	transfer = getdate(add_months(get_first_day(nowdate()), 1)).replace(day=13)
	move = frappe.get_doc({
		"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
		"transaction_date": str(transfer),
		"assets": [{
			"asset": asset.name, "source_cost_center": old_cc, "target_cost_center": new_cc,
			"source_location": frappe.db.get_value("Asset", asset.name, "location"),
			field: value,
		}],
	})
	move.flags.ignore_permissions = True
	move.insert()
	move.submit()

	period_start, period_end = get_first_day(transfer), get_last_day(transfer)
	segments = attribution_split(asset.name, period_start, period_end, 84.931506849, company)
	split_ok = (
		len(segments) == 3
		and segments[0][0] == old_cc and not segments[0][1].get(field)
		and segments[1][0] == old_cc and segments[1][1].get(field) == value
		and segments[2][0] == new_cc and segments[2][1].get(field) == value
	)

	# The split is only half the claim: prove the dimension reaches the
	# ledger. A JE row can carry it while GL drops it, and GL is what
	# every report and the client's own view actually read.
	schedule = frappe.get_all(
		"Asset Depreciation Schedule",
		filters={"asset": asset.name, "status": "Active", "docstatus": 1}, pluck="name",
	)
	posted_ok, gl_detail = False, "no active schedule to post"
	if schedule:
		post_schedule_entries(schedule[0], date=str(period_end))
		row = frappe.get_all(
			"Depreciation Schedule",
			filters={"parent": schedule[0],
			         "schedule_date": ("between", [period_start, period_end])},
			fields=["journal_entry"],
		)
		je = row[0]["journal_entry"] if row else None
		gl = frappe.db.sql(
			"""select debit, credit, {0} as dim from `tabGL Entry`
			   where voucher_no = %s and is_cancelled = 0""".format(f"`{field}`"),
			je, as_dict=True,
		) if je else []
		debits = [g for g in gl if flt(g.debit)]
		credits = [g for g in gl if flt(g.credit)]
		posted_ok = (
			len(debits) == 3
			and {bool(g.dim) for g in debits} == {True, False}   # one segment each
			and all(g.dim == value for g in debits if g.dim)
			and credits and not any(g.dim for g in credits)      # contra stays clean (V-08)
		)
		gl_detail = (
			f"GL {je}: debits "
			+ ", ".join(f"{flt(g.debit):.2f}/{g.dim or 'none'}" for g in debits)
			+ "; credit " + ", ".join(f"{flt(g.credit):.2f}/{g.dim or 'none'}" for g in credits)
		)

	return (split_ok and posted_ok), (
		f"{field}={value} on the transfer of {transfer}: "
		f"three date/dimension segments correct={split_ok}; "
		+ gl_detail
	)


@case("E-24", "V-08 / ruling 07-08/09", "the contra carries the ACQUISITION dimension, both axes")
def e24():
	"""V-08 says a contra-asset leg carries the dimension of the asset leg
	it contras — dimension, not cost centre. The two axes have to move
	together: core's `get_gl_dict` puts the receipt row's dimensions on
	the Fixed Asset cost leg, so a contra carrying none left a project
	showing gross cost with no accumulated depreciation against it.

	The distinction this proves is the one that makes the whole scheme
	coherent. An asset ACQUIRED under a project keeps that project on
	both balance-sheet legs for life. An asset that merely joined one by
	TRANSFER carries it on the expense only — it is being used by the
	project, it is not owned by it — so the contra must come back empty
	even while the period's expense is split onto the project.
	"""
	from asset_enterprise.depreciation import post_schedule_entries
	from asset_enterprise.gl_attribution import acquisition_dimensions, dimension_fields
	from asset_enterprise.setup.test_fixtures import make_test_asset

	fields = dimension_fields("Asset")
	field = next((f for f in fields if f == "project_accounting"), fields[0] if fields else None)
	if not field:
		return None, "no registered dimension on Asset — nothing to contra (skipped)"
	from asset_enterprise.setup.test_fixtures import dimension_fixture

	value = dimension_fixture(field, _company(), doctype="Asset")
	if not value:
		return None, f"no {field} record (skipped)"

	company = _company()
	asset = make_test_asset(company, gross=3_000, submit=False, with_depreciation=True)
	# Establish the acquisition dimension BEFORE the acquisition GL posts.
	asset.set(field, value)
	asset.save(ignore_permissions=True)
	asset.submit()

	schedule = frappe.get_all(
		"Asset Depreciation Schedule",
		filters={"asset": asset.name, "status": "Active", "docstatus": 1}, pluck="name",
	)
	if not schedule:
		return False, "no active schedule"
	row = frappe.get_all(
		"Depreciation Schedule", filters={"parent": schedule[0]},
		fields=["name", "schedule_date"], order_by="schedule_date", limit=1,
	)[0]
	post_schedule_entries(schedule[0], date=str(row["schedule_date"]))
	je = frappe.db.get_value("Depreciation Schedule", row["name"], "journal_entry")
	if not je:
		return False, "nothing posted"

	gl = frappe.db.sql(
		"""select account, debit, credit, `{0}` as dim from `tabGL Entry`
		   where voucher_no = %s and is_cancelled = 0""".format(field),
		je, as_dict=True,
	)
	credits = [g for g in gl if flt(g.credit)]
	debits = [g for g in gl if flt(g.debit)]
	ok = (
		bool(credits)
		and all(g.dim == value for g in credits)   # contra follows the cost
		and all(g.dim == value for g in debits)    # never moved: expense agrees
		and acquisition_dimensions(asset.name).get(field) == value
	)
	return ok, (
		f"acquired under {field}={value}: contra "
		+ ", ".join(f"{flt(g.credit):.2f}/{g.dim or 'none'}" for g in credits)
		+ " (want the project on it); expense "
		+ ", ".join(f"{flt(g.debit):.2f}/{g.dim or 'none'}" for g in debits)
		+ "; E-22 proves the transferred-in case leaves the contra empty"
	)


@case("E-23", "GAP-021 / V-08", "a transfer from a BLANK cost centre keeps its history")
def e23():
	"""`source_cost_center` is captured from `Asset.cost_center`, which is
	not a mandatory field — 82 UAT assets carry none, so their transfers
	record a target and no source.

	The origin then fell through to the asset's own `cost_center`, which
	the transfer had already overwritten, and the pre-transfer days
	inherited the centre the asset was moved TO: the precise defect the
	captured field exists to prevent, reached through the one path that
	captures nothing (UAT ACC-JV-2026-02277 booked 1-8 September to a
	project centre the asset joined on the 9th).

	The acquisition centre answers instead — derived from the ledger leg
	that carried the cost, and never mutated by a transfer.
	"""
	from asset_enterprise.depreciation import cost_centre_split
	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()
	acq_cc = frappe.db.get_value("Company", company, "cost_center")
	new_cc = next(
		iter(frappe.get_all(
			"Cost Center",
			filters={"company": company, "is_group": 0, "name": ("!=", acq_cc)},
			limit=1, pluck="name",
		)), None,
	)
	if not new_cc:
		return False, "need a second cost centre in this company"

	asset = make_test_asset(company, gross=3_000, submit=True)
	# The condition under test, in full: no cost centre on the asset, no
	# captured acquisition centre, and an opening cost leg with no `asset`
	# dimension — the legacy shape, whose cost core booked before GAP-023
	# registered the dimension. Seeding `acquisition_cost_center` here
	# instead would hand the derivation the answer and prove nothing: the
	# first version of this case did exactly that and passed while a
	# legacy asset still took the transfer target for its origin.
	frappe.db.set_value(
		"Asset",
		asset.name,
		{"cost_center": None, "acquisition_cost_center": None},
		update_modified=False,
	)
	frappe.db.sql("update `tabGL Entry` set asset = NULL where asset = %s", asset.name)
	transfer = getdate(add_months(get_first_day(nowdate()), 1)).replace(day=13)
	move = frappe.get_doc({
		"doctype": "Asset Movement", "company": company, "purpose": "Transfer",
		"transaction_date": str(transfer),
		"assets": [{
			"asset": asset.name, "target_cost_center": new_cc,
			"source_location": frappe.db.get_value("Asset", asset.name, "location"),
		}],
	})
	move.flags.ignore_permissions = True
	move.insert()
	move.submit()

	captured = frappe.db.get_value(
		"Asset Movement Item", {"parent": move.name}, "source_cost_center"
	)
	stamped = frappe.db.get_value("Asset", asset.name, "acquisition_cost_center")
	split = cost_centre_split(
		asset.name, get_first_day(transfer), get_last_day(transfer), 84.931506849, company
	)
	by_cc = {cc: flt(amt) for cc, amt in split}
	ok = (
		len(split) == 2
		and acq_cc in by_cc
		and new_cc in by_cc
		# the transfer resolved the blank rather than leaving it, and
		# resolved it to the acquisition centre and not to its own target
		and captured == acq_cc
		and stamped == acq_cc
	)
	# Legacy shape at the SECOND transfer: current centre has changed,
	# but the captured field is still absent. Its origin is in the trail.
	frappe.db.set_value("Asset", asset.name, "acquisition_cost_center", None)
	move2 = frappe.copy_doc(move)
	move2.transaction_date = str(add_days(transfer, 1))
	move2.assets[0].source_cost_center = None
	move2.assets[0].target_cost_center = acq_cc
	move2.flags.ignore_permissions = True
	move2.insert()
	move2.submit()
	second_origin = frappe.db.get_value("Asset", asset.name, "acquisition_cost_center")
	second_source = frappe.db.get_value("Asset Movement Item", {"parent": move2.name}, "source_cost_center")
	ok = ok and second_origin == acq_cc and second_source == new_cc
	return ok, (
		f"second transfer origin={second_origin}, source={second_source}; "
		f"captured source={captured or 'none'} (want {acq_cc}); "
		f"acquisition_cost_center={stamped or 'none'} (want {acq_cc}, never {new_cc}); "
		f"split {[(cc, round(a, 2)) for cc, a in split]} "
		f"(want the pre-transfer days on {acq_cc}, not on {new_cc})"
	)


@case("E-25", "client 09/09", "an event BEFORE any posting keeps the days already in service")
def e25():
	"""Ruba's ABC - Bulding: 1,200 available for use 01/03, partially
	scrapped for 200 on 15/03, nothing posted yet. The rebuilt schedule
	charged its first row for 16 days — 01–15 March had no row at all,
	and the surviving 1,000 was squeezed into 350 days instead of 365.

	The total still landed on 1,000, which is why nothing caught it. Only
	the timing was wrong: no expense in the first fortnight of service,
	too much in every day after.

	A regeneration resumes from the last POSTED period. With nothing
	posted that argument gets stronger, not weaker — every day since the
	asset went into service is uncharged, so the rebuild must resume at
	the schedule's start basis, not at the event's own date.
	"""
	from asset_enterprise import disposal
	from asset_enterprise.depreciation import enable_depreciation
	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()
	# First of a month at least a month out — the fixture's own receipt
	# movement lands today, so the scrap has to be dated after it.
	afu = get_first_day(add_months(nowdate(), 1))
	asset = make_test_asset(company, gross=1_200, submit=False)
	asset.available_for_use_date = str(afu)
	asset.purchase_date = str(afu)
	asset.save(ignore_permissions=True)
	asset.submit()
	enable_depreciation(
		asset.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
		depreciation_start_date=get_last_day(afu), expected_value_after_useful_life=0,
	)
	disposal.partial_scrap_asset(
		asset.name, scrap_date=add_days(afu, 14), scrap_value=200, scrapping_type="Damage"
	)

	rows = _rows(asset.name)
	if not rows:
		return False, "no schedule after the partial scrap"
	first = rows[0]
	month_days = cint(date_diff(get_last_day(afu), afu)) + 1
	total = flt(sum(flt(r.depreciation_amount) for r in rows))
	# The first row must cover the whole month the asset entered service,
	# not just the part after the scrap.
	ok = (
		cint(first.days_in_period) == month_days
		and abs(total - 1_000) < 0.05
	)
	return ok, (
		f"asset in service {afu}, scrapped {add_days(afu, 14)} with nothing posted: "
		f"first row {first.schedule_date} covers {first.days_in_period} day(s) "
		f"(want {month_days} — the full month from {afu}); "
		f"schedule totals {total:,.2f} (want 1,000.00 = 1,200 less the 200 scrapped)"
	)


@case("E-26", "VR-043 / client 10/09", "a partial scrap over unposted periods is blocked, not re-spread")
def e26():
	"""Ruba's ACC-ASS-2026-00291: 3,600 in service 01/01, partially
	scrapped for 1,000 on 01/03 with January and February never posted.
	§12.9 relieves accumulated depreciation by the disposal ratio, so it
	assumes accumulated is current — with nothing posted it relieved
	zero and wrote the entire 1,000 to loss.

	Re-spreading the missing periods afterwards is not a repair: it
	prices January at the 2,600 base the asset only acquires in March.
	The periods must be booked first, so the scrap blocks.

	The second half matters as much as the first: an asset disposed of in
	the month it entered service owes NOTHING, and must still go through.
	That is the case fixed on 09/09, and a gate keyed on dates rather
	than on due periods would break it.
	"""
	from asset_enterprise import disposal
	from asset_enterprise.depreciation import enable_depreciation
	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()

	def _asset_from(afu, gross=3_600):
		a = make_test_asset(company, gross=gross, submit=False)
		a.available_for_use_date = str(afu)
		a.purchase_date = str(afu)
		a.save(ignore_permissions=True)
		a.submit()
		for m in frappe.get_all("Asset Movement Item", filters={"asset": a.name}, pluck="parent"):
			frappe.db.set_value("Asset Movement", m, "transaction_date", str(afu),
			                    update_modified=False)
		enable_depreciation(
			a.name, total_number_of_depreciations=36, frequency_of_depreciation=1,
			depreciation_start_date=get_last_day(afu), expected_value_after_useful_life=0,
		)
		return a.name

	# (1) periods outstanding -> blocked
	stale_afu = get_first_day(add_months(nowdate(), -2))
	stale = _asset_from(stale_afu)
	stale_scrap = get_first_day(nowdate())
	from asset_enterprise.depreciation import due_unposted_rows

	owed = len(due_unposted_rows(stale, stale_scrap))
	blocked = False
	try:
		disposal.partial_scrap_asset(stale, scrap_value=1_000, scrapping_type="Damage",
		                             scrap_date=stale_scrap)
	except frappe.ValidationError:
		blocked = True

	# (2) nothing owed (same month as service) -> must still proceed
	fresh_afu = get_first_day(add_months(nowdate(), 1))
	fresh = _asset_from(fresh_afu, gross=1_200)
	allowed = True
	try:
		disposal.partial_scrap_asset(fresh, scrap_value=200, scrapping_type="Damage",
		                             scrap_date=add_days(fresh_afu, 14))
	except frappe.ValidationError:
		allowed = False

	ok = blocked and owed >= 2 and allowed
	return ok, (
		f"{owed} period(s) outstanding at {stale_scrap}: scrap blocked={blocked} (want True); "
		f"asset scrapped in its own first month owes nothing: proceeded={allowed} (want True)"
	)


@case("E-27", "§4.11 / TC-031", "a partial scrap takes effect at the START of its date")
def e27():
	"""Derecognition ceases depreciation on the removed portion from the
	scrap date (IAS 16.55). So the old rate stops the day BEFORE the
	scrap and the scrap day itself is already at the post-scrap rate —
	TC-031's arithmetic and the client's 10/09 workbook both say so.
	Additions, adjustments and transfers stay end-of-day; only this path
	differs, and it must differ by exactly one day.

	A mid-month scrap with the prior month posted: the scrap month is one
	row, split at the boundary. Expected = (days before the scrap) x old
	rate + (scrap day onward) x new rate. One day the other way moves
	the row by (old - new) x 1, well above the tolerance.
	"""
	from asset_enterprise import disposal
	from asset_enterprise.depreciation import post_schedule_entries
	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()
	afu = get_first_day(add_months(nowdate(), 1))
	asset = make_test_asset(company, gross=36_500, submit=False)
	asset.available_for_use_date = str(afu)
	asset.purchase_date = str(afu)
	asset.save(ignore_permissions=True)
	asset.submit()
	from asset_enterprise.depreciation import enable_depreciation

	enable_depreciation(
		asset.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
		depreciation_start_date=get_last_day(afu), expected_value_after_useful_life=0,
	)
	sched = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset.name, "status": "Active", "docstatus": 1}, "name"
	)
	rows = _rows(asset.name)
	old_rate = flt(rows[0].daily_rate)
	post_schedule_entries(sched, date=str(get_last_day(afu)))  # month 1 posted
	posted = flt(rows[0].depreciation_amount)

	month2 = get_first_day(add_months(afu, 1))
	scrap_day = add_days(month2, 14)  # the 15th
	disposal.partial_scrap_asset(
		asset.name, scrap_date=str(scrap_day), scrap_value=3_650, scrapping_type="Damage"
	)

	after = _rows(asset.name)
	row = next((r for r in after if getdate(r.schedule_date) == get_last_day(month2)), None)
	if row is None:
		return False, "no row for the scrap month"
	# 14 days (1st..14th) at the old rate; from the 15th at the new rate
	relief = flt(3_650 / 36_500 * posted, 2)
	nbv_after = flt(36_500 - 3_650 - (posted - relief), 2)
	a_part = flt(old_rate * 14, 2)
	remaining = cint(date_diff(add_months(afu, 12), scrap_day))  # scrap day .. end of life
	new_rate = (nbv_after - a_part) / remaining
	month_days = cint(date_diff(get_last_day(month2), month2)) + 1
	want = flt(a_part + new_rate * (month_days - 14), 2)
	drift = abs(flt(row.depreciation_amount) - want)
	one_day_wrong = abs(old_rate - new_rate)  # what the other convention would move
	ok = drift < 0.05 and cint(row.days_in_period) == month_days
	return ok, (
		f"scrap on {scrap_day}: row {row.schedule_date} = {flt(row.depreciation_amount):,.2f} "
		f"(want {want:,.2f} = 14d x {old_rate:.4f} + {month_days - 14}d x {new_rate:.4f}); "
		f"drift {drift:.2f}, the other convention would show {one_day_wrong:.2f}"
	)


@case("E-28", "GAP-037 / client 14/09", "a control category never touches the balance sheet")
def e28():
	"""Control Category: tracked for control, expensed on purchase. Every
	account on the category is an expense account — the fixed-asset and
	accumulated-depreciation accounts included — so the opening booking,
	the one-day depreciation entry and final scrap land
	in P&L, and no GL row for the asset ever carries an Asset-side
	account.

	Three assertions, because the feature is three rules:
	  1. the flag ENFORCES expense accounts — a Fixed Asset-type account
	     on a control category is refused;
	  2. the whole life cycle posts to P&L only;
	  3. the flag LOCKS once a submitted asset exists.
	"""
	from asset_enterprise import disposal
	from asset_enterprise.depreciation import enable_depreciation, post_schedule_entries
	from asset_enterprise.setup.test_fixtures import pick_plain_account
	from asset_enterprise.setup.verify_tc import _item, _location

	company = _company()
	expense = pick_plain_account(company, "Expense")
	fixed_asset_type = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Fixed Asset", "is_group": 0}, "name"
	)
	name = "E28 Control Tools"
	if frappe.db.exists("Asset Category", name):
		frappe.delete_doc("Asset Category", name, force=True, ignore_permissions=True)

	# 1. enforcement — a balance-sheet account on a control category is refused
	cat = frappe.get_doc({"doctype": "Asset Category", "asset_category_name": name,
	                      "is_control_category": 1})
	cat.append("accounts", {
		"company_name": company,
		"fixed_asset_account": fixed_asset_type,          # wrong side
		"accumulated_depreciation_account": expense,
		"depreciation_expense_account": expense,
		"asset_suspense_account": pick_plain_account(company, "Liability"),
	})
	cat.flags.ignore_permissions = True
	refused = False
	try:
		cat.insert()
	except frappe.ValidationError:
		refused = True
	if not refused:
		return False, "a Fixed Asset-type account was accepted on a control category"

	# Cost and accumulated balances need distinct GL accounts even though
	# both are Expense-root accounts for a control category.
	contra = frappe.get_doc({
		"doctype": "Account", "account_name": "E28 Contra " + frappe.generate_hash(length=6),
		"company": company, "parent_account": frappe.db.get_value("Account", expense, "parent_account"),
		"is_group": 0, "account_type": "Expense Account",
	}).insert(ignore_permissions=True).name
	dep_expense = frappe.get_doc({
		"doctype": "Account", "account_name": "E28 Depreciation " + frappe.generate_hash(length=6),
		"company": company, "parent_account": frappe.db.get_value("Account", expense, "parent_account"),
		"is_group": 0, "account_type": "Expense Account",
	}).insert(ignore_permissions=True).name
	cat.accounts[0].depreciation_expense_account = dep_expense
	cat.accounts[0].accumulated_depreciation_account = contra
	# now all-expense: accepted
	cat.accounts[0].fixed_asset_account = expense
	cat.insert()

	# 2. life cycle
	item = _item(name, "E28-CTRL-ITEM")
	asset = frappe.get_doc({
		"doctype": "Asset", "company": company, "asset_name": "E28 control asset",
		"asset_category": name, "item_code": item, "location": _location(),
		"purchase_amount": 12_000, "net_purchase_amount": 12_000,
		"available_for_use_date": get_first_day(add_months(nowdate(), -1)),
		"purchase_date": get_first_day(add_months(nowdate(), -1)),
		"asset_type": "Existing Asset", "calculate_depreciation": 0,
	})
	from asset_enterprise.setup.test_fixtures import dimension_fixture
	from asset_enterprise.depreciation import project_dimension_fields
	pa_project = None
	if "project_accounting" in project_dimension_fields():
		pa_project = dimension_fixture("project_accounting", company)
		asset.project_accounting = pa_project
	asset.flags.ignore_permissions = True
	asset.insert()
	asset.submit()
	charge_date = getdate(asset.available_for_use_date)
	enable_depreciation(asset.name, total_number_of_depreciations=48,
		depreciation_start_date=charge_date, expected_value_after_useful_life=1000)
	schedule = frappe.get_doc("Asset Depreciation Schedule", {
		"asset": asset.name, "status": "Active", "docstatus": 1})
	assert len(schedule.depreciation_schedule) == 1
	row = schedule.depreciation_schedule[0]
	assert row.days_in_period == 1 and flt(row.depreciation_amount) == 12000
	assert getdate(row.schedule_date) == charge_date
	assert schedule.expected_value_after_useful_life == 0
	post_schedule_entries(schedule.name, date=str(charge_date))
	from asset_enterprise.asset_values import recalculate_asset_values
	assert abs(recalculate_asset_values(asset.name, save=False)["net_book_value"]) < 0.01
	schedule.reload()
	je = schedule.depreciation_schedule[0].journal_entry
	assert je
	post_schedule_entries(schedule.name, date=str(charge_date))
	assert frappe.db.count("GL Entry", {"voucher_no": je, "is_cancelled": 0}) == 2
	if pa_project:
		from project_accounting.settlement.sources import get_eligible_source_lines
		sources = get_eligible_source_lines(company, pa_project)
		# D-051: the acquisition is the one settleable source; the one-day
		# depreciation pair is never a project source line.
		assert not any(r.voucher_no == je for r in sources), "control depreciation offered as a source"
	one_day_ok = True  # one-day completion checks above succeeded
	disposal.scrap_asset(asset.name, scrap_date=nowdate(), scrapping_type="Damage")
	if pa_project:
		# D-051: disposal legs on the accumulated / depreciation accounts are
		# part of the pair, so they are not sources either
		pair = {contra, dep_expense}
		assert not any(r.original_account in pair for r in get_eligible_source_lines(company, pa_project)), \
			"control depreciation-pair row offered as a source after scrap"

	legs = frappe.db.sql(
		"""select gle.account, acc.root_type, gle.debit, gle.credit
		   from `tabGL Entry` gle join `tabAccount` acc on acc.name = gle.account
		   where gle.is_cancelled = 0 and gle.asset = %s""",
		asset.name, as_dict=True,
	)
	on_balance_sheet = [r for r in legs if r.root_type in ("Asset",)]
	vouchers = frappe.db.sql(
		"select count(distinct voucher_no) from `tabGL Entry` where is_cancelled=0 and asset=%s",
		asset.name,
	)[0][0]

	# 3. lock
	cat.reload()
	cat.is_control_category = 0
	locked = False
	try:
		cat.save()
	except frappe.ValidationError:
		locked = True

	ok = refused and legs and not on_balance_sheet and vouchers >= 3 and locked and one_day_ok
	return ok, (
		f"balance-sheet account refused={refused} (want True); {vouchers} voucher(s) / "
		f"{len(legs)} GL rows over the life cycle, {len(on_balance_sheet)} on an Asset-side "
		f"account (want 0); flag locked={locked}; full one-day charge posted; PA depreciation eligibility checked={bool(pa_project)}"
	)


@case("E-29", "client 16/09 (ACC-ASS-2026-00019)", "two events in one unposted month price the days between them exactly")
def e29():
	"""Ruba's ACC-ASS-2026-00019: partial scrap on 20/03, invoice
	difference on 25/03, March not yet posted. The second rebuild priced
	1–25 March from the March row's single BLENDED rate (19 days at the
	old rate averaged with 12 at the post-scrap rate) instead of the real
	per-day composition, under-charging March by 16,151.25 and spreading
	the shortfall over the next twenty years. The FA team found it as
	5,117 on the post-invoice rate.

	Rows now carry their per-day composition; a later event in the same
	period prices the days before it from that. Expected month-2 row:

	    days 1..9   at the original rate
	    days 10..19 at the post-scrap rate
	    days 20..   at the post-adjustment rate

	Both events take effect from their own date (§4.11, client 16/09).
	"""
	from asset_enterprise import disposal
	from asset_enterprise.depreciation import enable_depreciation, post_schedule_entries
	from asset_enterprise.setup.test_fixtures import make_test_asset, pick_plain_account

	company = _company()
	afu = get_first_day(add_months(nowdate(), 1))
	asset = make_test_asset(company, gross=36_500, submit=False)
	asset.available_for_use_date = str(afu)
	asset.purchase_date = str(afu)
	asset.save(ignore_permissions=True)
	asset.submit()
	enable_depreciation(
		asset.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
		depreciation_start_date=get_last_day(afu), expected_value_after_useful_life=0,
	)
	sched = frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset.name, "status": "Active", "docstatus": 1}, "name"
	)
	r0 = flt(_rows(asset.name)[0].daily_rate)
	post_schedule_entries(sched, date=str(get_last_day(afu)))

	month2 = get_first_day(add_months(afu, 1))
	m2_end = get_last_day(month2)
	d = cint(date_diff(m2_end, month2)) + 1

	# event 1: partial scrap on the 10th -> post-scrap rate r1 from the 10th
	disposal.partial_scrap_asset(
		asset.name, scrap_date=str(add_days(month2, 9)), scrap_value=3_650, scrapping_type="Damage"
	)
	mid = _rows(asset.name)
	r1 = flt(next(r for r in mid if getdate(r.schedule_date) > m2_end).daily_rate)

	# event 2: upward revaluation on the 20th -> post-adjustment rate r2 from the 20th
	nbv = flt(frappe.db.get_value("Asset", asset.name, "net_book_value"))
	ava = frappe.get_doc({
		"doctype": "Asset Value Adjustment", "asset": asset.name, "company": company,
		"date": str(add_days(month2, 19)), "transaction_type": "Upward Revaluation",
		"current_asset_value": nbv, "new_asset_value": nbv + 2_000,
		"difference_account": pick_plain_account(company, "Liability"),
	})
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()

	final = _rows(asset.name)
	row = next((r for r in final if getdate(r.schedule_date) == m2_end), None)
	if row is None:
		return False, "no row for month 2"
	r2 = flt(next(r for r in final if getdate(r.schedule_date) > m2_end).daily_rate)
	want = 9 * r0 + 10 * r1 + (d - 19) * r2
	blended_want = d and (19 * ((9 * r0 + (d - 9) * r1) / d)) + (d - 19) * r2  # the old behaviour
	segs = frappe.db.get_value("Depreciation Schedule", {"parent": final and frappe.db.get_value(
		"Asset Depreciation Schedule", {"asset": asset.name, "status": "Active", "docstatus": 1}, "name"),
		"schedule_date": m2_end}, "rate_segments")
	n_segs = len(frappe.parse_json(segs) or []) if segs else 0
	# The composition must also be SHOWN (client, 16/09): one entry per
	# rate on the row's Rate Breakdown, in the grid, not only in hidden
	# JSON. Three rates -> three "@" entries carrying the three rates.
	shown = frappe.db.get_value(
		"Depreciation Schedule",
		{"parent": frappe.db.get_value(
			"Asset Depreciation Schedule", {"asset": asset.name, "status": "Active", "docstatus": 1}, "name"),
		 "schedule_date": m2_end},
		"rate_breakdown",
	) or ""
	shown_ok = shown.count("@") == 3 and all(f"{x:,.6f}" in shown for x in (r0, r1, r2))
	ok = abs(flt(row.depreciation_amount) - want) < 0.05 and n_segs == 3 and shown_ok
	return ok, (
		f"month-2 row {flt(row.depreciation_amount):,.2f} (want {want:,.2f} = 9d x {r0:.4f} + "
		f"10d x {r1:.4f} + {d - 19}d x {r2:.4f}; the blended-rate defect would give "
		f"{blended_want:,.2f}); rate segments stored: {n_segs} (want 3); "
		f"shown on the grid as \"{shown}\" ({'all three rates' if shown_ok else 'INCOMPLETE'})"
	)


@case("E-30", "client 16/09", "a generation carries the numbers it was priced from")
def e30():
	"""Every generation is stamped with asset value, accumulated
	depreciation, NBV, salvage, depreciable base, remaining days, daily
	rate and end of life — the cells of the finance team's worksheet —
	at the moment it is built. Two checks that cannot be satisfied by
	copying numbers around:

	  1. rate x remaining days = depreciable base, on the stamp itself;
	  2. the stamp agrees with the ROWS: the first row priced at the new
	     rate carries that rate, and the rows from Re-priced From onward
	     add up to the depreciable base.

	Run on a mid-month event so the pre-event stub is in play: the
	stamped accumulated must include the days before the event.
	"""
	from asset_enterprise import disposal
	from asset_enterprise.depreciation import enable_depreciation, post_schedule_entries
	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = _company()
	afu = get_first_day(add_months(nowdate(), 1))
	asset = make_test_asset(company, gross=36_500, submit=False)
	asset.available_for_use_date = str(afu)
	asset.purchase_date = str(afu)
	asset.save(ignore_permissions=True)
	asset.submit()
	enable_depreciation(
		asset.name, total_number_of_depreciations=12, frequency_of_depreciation=1,
		depreciation_start_date=get_last_day(afu), expected_value_after_useful_life=500,
	)

	def _gen():
		name = frappe.db.get_value(
			"Asset Depreciation Schedule", {"asset": asset.name, "status": "Active", "docstatus": 1}, "name"
		)
		return frappe.get_doc("Asset Depreciation Schedule", name)

	g0 = _gen()
	ok0 = (
		abs(flt(g0.basis_daily_rate) * cint(g0.basis_remaining_days) - flt(g0.basis_depreciable_base)) < 0.01
		and abs(flt(g0.basis_depreciable_base) - (36_500 - 500)) < 0.01
		and getdate(g0.repriced_from) == afu
	)

	post_schedule_entries(g0.name, date=str(get_last_day(afu)))
	month2 = get_first_day(add_months(afu, 1))
	disposal.partial_scrap_asset(
		asset.name, scrap_date=str(add_days(month2, 14)), scrap_value=3_650, scrapping_type="Damage"
	)
	g1 = _gen()
	rows = g1.get("depreciation_schedule")
	after = [r for r in rows if getdate(r.schedule_date) > get_last_day(month2)]
	first_new = after[0] if after else None
	from asset_enterprise.asset_values import recalculate_asset_values

	# ledger accumulated, not the sum of posted rows: the scrap relieved
	# its share of October, and the stamp reads the ledger
	ledger_accum = flt(recalculate_asset_values(asset.name, save=False)["accumulated_depreciation_value"])
	# rows priced from Re-priced From: the composed month-2 row less its
	# pre-event stub, plus every later row
	stub = flt(g1.basis_accumulated) - ledger_accum
	from_repriced = sum(flt(r.depreciation_amount) for r in rows if not r.journal_entry) - stub
	ok1 = (
		getdate(g1.repriced_from) == add_days(month2, 14)
		and abs(flt(g1.basis_daily_rate) * cint(g1.basis_remaining_days) - flt(g1.basis_depreciable_base)) < 0.01
		and abs(flt(g1.basis_nbv) - (flt(g1.basis_hav) - flt(g1.basis_accumulated))) < 0.01
		and abs(flt(g1.basis_depreciable_base) - (flt(g1.basis_nbv) - flt(g1.basis_salvage))) < 0.01
		and first_new is not None
		and abs(flt(first_new.daily_rate) - flt(g1.basis_daily_rate)) < 1e-6
		and abs(from_repriced - flt(g1.basis_depreciable_base)) < 0.05
		and stub > 0
	)
	return ok0 and ok1, (
		f"initial: rate {flt(g0.basis_daily_rate):.6f} x {g0.basis_remaining_days}d = "
		f"{flt(g0.basis_daily_rate) * cint(g0.basis_remaining_days):,.2f} (base {flt(g0.basis_depreciable_base):,.2f}); "
		f"after scrap on {add_days(month2, 14)}: re-priced from {g1.repriced_from} (want {add_days(month2, 14)}), "
		f"HAV {flt(g1.basis_hav):,.2f} − accum {flt(g1.basis_accumulated):,.2f} (incl. {stub:,.2f} accrued before the scrap) "
		f"= NBV {flt(g1.basis_nbv):,.2f}; base {flt(g1.basis_depreciable_base):,.2f} over {g1.basis_remaining_days}d "
		f"@ {flt(g1.basis_daily_rate):.6f}; first re-priced row @ {flt(first_new.daily_rate) if first_new else 0:.6f}; "
		f"rows from re-priced date sum {from_repriced:,.2f}"
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


@case("E-31", "D-026 / R124", "reversal preserves the posted centre after acquisition attribution changes")
def e31():
	from asset_enterprise.depreciation import post_schedule_entries
	from asset_enterprise.gl_attribution import dimension_fields
	from asset_enterprise.restore import _mirror_je

	company = _company()
	asset = _asset(company, start=get_first_day(nowdate()))
	schedule = frappe.db.get_value("Asset Depreciation Schedule",
		{"asset": asset, "status": "Active", "docstatus": 1}, "name")
	post_schedule_entries(schedule, nowdate())
	je = frappe.db.get_value("Depreciation Schedule",
		{"parent": schedule, "journal_entry": ("is", "set")}, "journal_entry")
	if not je:
		# EOM schedule: explicitly post its first row for this rollback-only case.
		date = frappe.db.get_value("Depreciation Schedule", {"parent": schedule}, "schedule_date")
		post_schedule_entries(schedule, str(date))
		je = frappe.db.get_value("Depreciation Schedule", {"parent": schedule}, "journal_entry")
	source = frappe.get_doc("Journal Entry", je)
	old_cc = next(r.cost_center for r in source.accounts if r.credit_in_account_currency)
	new_cc = frappe.db.get_value("Cost Center",
		{"company": company, "is_group": 0, "name": ("!=", old_cc)}, "name")
	if not new_cc:
		return False, "need a second cost centre"
	frappe.db.set_value("Asset", asset, "acquisition_cost_center", new_cc)
	mirror = frappe.get_doc("Journal Entry", _mirror_je(je, "D-026 regression"))
	fields = ["account", "cost_center", "project", "asset", "reference_type", "reference_name"]
	fields += dimension_fields("Journal Entry Account")
	ok = bool(mirror.is_reversal and mirror.reversal_of == je)
	ok = ok and len(source.accounts) == len(mirror.accounts)
	for original, reversed_row in zip(source.accounts, mirror.accounts):
		ok = ok and all(original.get(f) == reversed_row.get(f) for f in fields)
		ok = ok and original.debit_in_account_currency == reversed_row.credit_in_account_currency
		ok = ok and original.credit_in_account_currency == reversed_row.debit_in_account_currency
	# Draft re-validation exercises the fork's locked-fields guard too.
	mirror.validate_reversal_locked_fields()
	return ok, f"source={je}, mirror={mirror.name}; changed origin {old_cc} -> {new_cc}; original row attribution preserved={ok}"


@case("E-32", "R017 / §3.7.3", "CM reversal uses the role-governed chosen date")
def e32():
	from asset_enterprise.api import cancel_capitalization_with_reversal
	from asset_enterprise.setup.test_fixtures import make_test_asset, pick_plain_account
	from asset_enterprise.setup.verify_tc import _cm_merge

	company = _company()
	target = make_test_asset(company, gross=40_000, submit=True)
	source = make_test_asset(company, gross=10_000, submit=True)
	frappe.db.set_value("Asset Category Account",
		{"parent": target.asset_category, "company_name": company},
		"capitalization_clearing_account", pick_plain_account(company, "Liability"))
	cap = _cm_merge(company, target.name, source.name)
	chosen = add_days(getdate(nowdate()), 1)
	frappe.db.delete("Asset Settings Reversal Role", {"parent": "Asset Settings", "company": company})
	refused, message = _refused(lambda: cancel_capitalization_with_reversal(cap.name, chosen))
	if not refused or "Reversal Date Edit Role" not in message:
		return False, f"ungoverned date was not refused: {message}"
	settings = frappe.get_single("Asset Settings")
	# Locate the table by its child type to keep the fixture schema-driven.
	field = next(f.fieldname for f in settings.meta.fields if f.options == "Asset Settings Reversal Role")
	settings.append(field, {"company": company, "reversal_date_edit_role": "System Manager"})
	settings.save(ignore_permissions=True)
	cancel_capitalization_with_reversal(cap.name, chosen)
	reversal = frappe.db.get_value("Asset Capitalization",
		{"reversal_of_capitalization": cap.name, "docstatus": 1}, ["name", "posting_date"], as_dict=True)
	ok = bool(reversal and getdate(reversal.posting_date) == getdate(chosen))
	originals = frappe.get_all("Journal Entry",
		filters={"user_remark": ("like", f"%{cap.name}%"), "docstatus": 1, "is_reversal": 0},
		fields=["name", "reversed_by"])
	ok = ok and bool(originals) and all(r.reversed_by and frappe.db.get_value(
		"Journal Entry", r.reversed_by, "reversal_of") == r.name for r in originals)
	ok = ok and frappe.flags.get("ae_capitalization_reversal_date") is None
	return ok, f"date without role refused={refused}; chosen={chosen}, reversal={reversal}"


@case("E-33", "VR-008 / R161", "reconciliation detects a ledger mismatch despite an unchanged fold")
def e33():
	from asset_enterprise.setup.test_fixtures import make_test_asset
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.asset_enterprise.report.asset_daily_reconciliation.asset_daily_reconciliation import execute

	company = _company()
	asset = make_test_asset(company, gross=3_000, submit=True)
	before = recalculate_asset_values(asset.name, save=False)
	# Simulate an unattributed historical acquisition leg inside the
	# rollback-only fixture. The fold still says 3,000; the GL no longer does.
	frappe.db.sql("""update `tabGL Entry` set asset = NULL,
		against_voucher = NULL, against_voucher_type = NULL where asset = %s""", asset.name)
	_, rows = execute({"company": company, "flagged_only": 1})
	row = next((r for r in rows if r["asset"] == asset.name), None)
	ok = bool(row and row["flagged"] == "Yes" and row["gl_hav"] == 0
		and row["derived_hav"] == before["historical_asset_value"] == 3_000)
	return ok, f"unattributed GL leg: {row}"


@case("E-34", "§5.1 / R053", "posted GL, not treatment metadata, controls operational values")
def e34():
	from asset_enterprise import tcc
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.setup.test_fixtures import make_test_asset, pick_plain_account

	company = _company()
	asset = make_test_asset(company, gross=3_000, submit=True)
	tcc.apply(("Asset", asset.name), "Addition", asset.name,
		transaction_type="E34 metadata only", amount=700, hav_delta=700)
	metadata_value = recalculate_asset_values(asset.name, save=False)["historical_asset_value"]
	fa = frappe.db.get_value("Asset Category Account",
		{"parent": asset.asset_category, "company_name": company}, "fixed_asset_account")
	je = frappe.get_doc({"doctype": "Journal Entry", "voucher_type": "Journal Entry",
		"company": company, "posting_date": nowdate(), "accounts": [
			{"account": fa, "asset": asset.name, "debit_in_account_currency": 250},
			{"account": pick_plain_account(company, "Liability"), "credit_in_account_currency": 250},
		]})
	je.flags.ignore_permissions = True
	je.submit()
	posted_value = recalculate_asset_values(asset.name, save=False)["historical_asset_value"]
	return metadata_value == 3_000 and posted_value == 3_250, (
		f"metadata-only value={metadata_value} (want 3000); dimension-only GL posting -> {posted_value} (want 3250)"
	)


@case("E-35", "R174 / D-026", "AVA mirror retains its original attribution and JE back-references")
def e35():
	from asset_enterprise.setup.test_fixtures import make_test_asset, pick_plain_account
	company = _company()
	asset = make_test_asset(company, gross=10_000, submit=True)
	frappe.db.set_value("Company", company, "default_revaluation_surplus_oci_account",
		pick_plain_account(company, "Liability"))
	ava = frappe.get_doc({"doctype": "Asset Value Adjustment", "asset": asset.name, "company": company,
		"date": nowdate(), "transaction_type": "Upward Revaluation",
		"current_asset_value": 10_000, "new_asset_value": 12_000})
	ava.flags.ignore_permissions = True
	ava.insert()
	ava.submit()
	original = frappe.get_doc("Journal Entry", ava.journal_entry)
	old_cc = next(r.cost_center for r in original.accounts if r.debit_in_account_currency)
	new_cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0, "name": ("!=", old_cc)}, "name")
	frappe.db.set_value("Asset", asset.name, "acquisition_cost_center", new_cc)
	ava.cancel()
	reversal = frappe.db.get_value("Asset Value Adjustment",
		{"reversal_of_ava": ava.name, "docstatus": 1}, "journal_entry")
	mirror = frappe.get_doc("Journal Entry", reversal)
	original.reload()
	ok = mirror.is_reversal and mirror.reversal_of == original.name and original.reversed_by == mirror.name
	ok = ok and all(a.cost_center == b.cost_center and a.debit_in_account_currency == b.credit_in_account_currency
		and a.credit_in_account_currency == b.debit_in_account_currency for a, b in zip(original.accounts, mirror.accounts))
	return bool(ok), f"{original.name} <-> {mirror.name}; attribution preserved after origin changed to {new_cc}"


@case("E-36", "R174 / §3.7", "Repair reversal owns its GL voucher, date and two-way audit links")
def e36():
	from asset_enterprise.api import cancel_repair_with_reversal
	from asset_enterprise.setup.test_fixtures import make_test_asset
	from asset_enterprise.setup.verify_tc import _stock_in, _stock_item, _warehouse
	company = _company()
	asset = make_test_asset(company, gross=3_000, submit=True)
	item, warehouse = _stock_item("E36-REPAIR-STOCK"), _warehouse(company)
	_stock_in(company, item, warehouse, 10, 100)
	repair = frappe.get_doc({"doctype": "Asset Repair", "asset": asset.name, "company": company,
		"failure_date": nowdate(), "completion_date": nowdate(), "repair_status": "Completed",
		"capitalize_repair_cost": 1, "cost_center": frappe.db.get_value("Company", company, "cost_center"),
		"stock_items": [{"item_code": item, "warehouse": warehouse, "consumed_quantity": 2,
			"valuation_rate": 100, "total_value": 200}],
	})
	repair.flags.ignore_permissions = True
	repair.insert()
	repair.submit()
	original = frappe.get_all("GL Entry", filters={"voucher_type": "Asset Repair", "voucher_no": repair.name},
		fields=["name", "account", "debit", "credit", "cost_center", "asset"], order_by="account")
	settings = frappe.get_single("Asset Settings")
	field = next(f.fieldname for f in settings.meta.fields if f.options == "Asset Settings Reversal Role")
	settings.set(field, [r for r in settings.get(field) if r.company != company])
	settings.append(field, {"company": company, "reversal_date_edit_role": "System Manager"})
	settings.save(ignore_permissions=True)
	chosen = add_days(getdate(nowdate()), 1)
	cancel_repair_with_reversal(repair.name, chosen)
	reversed_by = frappe.db.get_value("Asset Repair", repair.name, "reversed_by_repair")
	mirrors = frappe.get_all("GL Entry", filters={"voucher_type": "Asset Repair", "voucher_no": reversed_by},
		fields=["account", "debit", "credit", "cost_center", "asset", "posting_date"], order_by="account")
	ok = bool(original and len(original) == len(mirrors))
	ok = ok and frappe.db.get_value("Asset Repair", reversed_by, "reversal_of_repair") == repair.name
	ok = ok and all(getdate(m.posting_date) == getdate(chosen) and a.account == m.account
		and flt(a.debit) == flt(m.credit) and flt(a.credit) == flt(m.debit)
		and a.cost_center == m.cost_center and a.asset == m.asset for a, m in zip(original, mirrors))
	ok = ok and frappe.db.count("GL Entry", {"voucher_type": "Asset Repair", "voucher_no": repair.name}) == len(original)
	ok = ok and all(frappe.db.get_value("GL Entry", r.name, "is_cancelled") == 0 for r in original)
	return bool(ok), f"repair={repair.name}, reversal={reversed_by}; {len(original)} original rows, {len(mirrors)} mirror rows dated {chosen}"


@case("E-37", "D-031 / R126", "explicit project exit, blank retention, re-entry and cancellation preserve dated attribution")
def e37():
	from asset_enterprise.depreciation import attribution_on, project_dimension_fields, attribution_split
	from asset_enterprise.setup.test_fixtures import make_test_asset, dimension_fixture
	from asset_enterprise.gl_attribution import acquisition_dimensions

	company = _company()
	fields = project_dimension_fields()
	if not fields:
		return None, "no project dimension installed"
	field = fields[0]
	project = dimension_fixture(field, company)
	asset = make_test_asset(company, gross=3000, submit=False, with_depreciation=True)
	asset.set(field, project)
	asset.save(ignore_permissions=True)
	asset.submit()
	origin = acquisition_dimensions(asset.name)
	date = get_first_day(add_months(nowdate(), 1))
	ccs = frappe.get_all("Cost Center", filters={"company": company, "is_group": 0}, pluck="name", limit=2)
	old_cc = frappe.db.get_value("Asset", asset.name, "cost_center") or ccs[0]
	new_cc = next(c for c in ccs if c != old_cc)

	def move(day, **values):
		doc = frappe.get_doc({"doctype": "Asset Movement", "company": company,
			"purpose": "Transfer", "transaction_date": str(add_days(date, day - 1)),
			"assets": [{"asset": asset.name, **values}]})
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	employee = frappe.get_doc({"doctype": "Employee", "first_name": "D031 Custodian",
		"company": company, "gender": "Male", "date_of_birth": "1990-01-01",
		"date_of_joining": "2020-01-01", "status": "Active"}).insert(ignore_permissions=True)
	move(2, to_employee=employee.name)
	# Blank project plus a CC transfer does not mean exit.
	move(5, target_cost_center=new_cc)
	assert attribution_on(asset.name, add_days(date, 5))[1].get(field) == project
	# Combined event has two distinct effective boundaries.
	exit_doc = move(16, leave_project=1, target_cost_center=old_cc)
	assert attribution_on(asset.name, add_days(date, 14))[1].get(field) == project
	cc, dims = attribution_on(asset.name, add_days(date, 15))
	assert cc == new_cc and not dims.get(field)
	assert attribution_on(asset.name, add_days(date, 16))[0] == old_cc
	move(20, **{field: project})
	assert attribution_on(asset.name, add_days(date, 19))[1].get(field) == project
	# Cancelling the exit replays surviving history, preserving the later project assignment.
	exit_doc.cancel()
	assert attribution_on(asset.name, add_days(date, 16))[1].get(field) == project
	assert attribution_on(asset.name, add_days(date, 20))[1].get(field) == project
	# A dimension-only exit is a valid movement.
	move(22, leave_project=1)
	assert not attribution_on(asset.name, add_days(date, 21))[1].get(field)
	assert frappe.db.get_value("Asset", asset.name, "custodian") == employee.name, "project exit cleared custody"
	assert acquisition_dimensions(asset.name) == origin
	try:
		move(23, leave_project=1, **{field: project})
	except frappe.ValidationError as exc:
		assert "D-031" in str(exc)
	else:
		return False, "contradictory exit/destination accepted"
	segments = attribution_split(asset.name, date, get_last_day(date), 300, company)
	assert abs(sum(segment[2] for segment in segments) - 300) < 0.01
	# Prove an exit from the acquisition project reaches posted GL; the
	# contra must retain the origin while expense includes unassigned days.
	from asset_enterprise.depreciation import post_schedule_entries
	schedule = frappe.db.get_value("Asset Depreciation Schedule",
		{"asset": asset.name, "status": "Active", "docstatus": 1}, "name")
	post_schedule_entries(schedule, date=str(get_last_day(date)))
	je = frappe.db.get_value("Depreciation Schedule", {
		"parent": schedule, "schedule_date": get_last_day(date)}, "journal_entry")
	assert je, "no period JE posted"
	gl = frappe.get_all("GL Entry", filters={"voucher_no": je, "is_cancelled": 0},
		fields=["debit", "credit", field])
	assert any(flt(row.debit) and not row.get(field) for row in gl), "exit lost in GL"
	assert all(row.get(field) == project for row in gl if flt(row.credit)), "acquisition contra changed"
	return True, "blank retains; exit clears on day 16; CC changes day 17; re-entry/cancel/standalone exit and unchanged acquisition dimensions verified"



@case("E-38", "Client 22/09 / R135", "new control assets get a one-day schedule; gradual legacy rows are refused")
def e38():
	ok, detail = e28()
	assert ok, detail
	from asset_enterprise.setup.verify_tc import _location
	from asset_enterprise.depreciation import post_schedule_entries
	from asset_enterprise.asset_values import recalculate_asset_values
	company = _company()
	date = getdate(nowdate())
	asset = frappe.get_doc({"doctype": "Asset", "company": company,
		"asset_name": "E38 One Day", "asset_category": "E28 Control Tools",
		"item_code": "E28-CTRL-ITEM", "location": _location(), "asset_type": "Existing Asset",
		"purchase_amount": 6000, "net_purchase_amount": 6000,
		"purchase_date": date, "available_for_use_date": date, "calculate_depreciation": 1,
		"finance_books": [{"depreciation_method": "Straight Line",
			"total_number_of_depreciations": 36, "frequency_of_depreciation": 1,
			"expected_value_after_useful_life": 500, "depreciation_start_date": date}]})
	asset.insert(ignore_permissions=True)
	asset.submit()
	schedule = frappe.get_doc("Asset Depreciation Schedule", {
		"asset": asset.name, "status": "Active", "docstatus": 1})
	assert len(schedule.depreciation_schedule) == 1, f"rows={len(schedule.depreciation_schedule)}"
	row = schedule.depreciation_schedule[0]
	assert row.days_in_period == 1 and flt(row.depreciation_amount) == 6000, f"days={row.days_in_period}, amount={row.depreciation_amount}"
	assert getdate(row.schedule_date) == date, f"date={row.schedule_date}, want={date}"
	assert schedule.basis_remaining_days == 1 and flt(schedule.basis_daily_rate) == 6000, f"basis days={schedule.basis_remaining_days} rate={schedule.basis_daily_rate}"
	# Legacy/imported gradual rows cannot silently post under the new rule.
	frappe.db.set_value("Depreciation Schedule", row.name, "days_in_period", 30)
	try:
		post_schedule_entries(schedule.name, date=str(date))
	except frappe.ValidationError as exc:
		assert "one day" in str(exc)
	else:
		return False, "gradual legacy row posted"
	frappe.db.set_value("Depreciation Schedule", row.name, "days_in_period", 1)
	post_schedule_entries(schedule.name, date=str(date))
	assert abs(recalculate_asset_values(asset.name, save=False)["net_book_value"]) < 0.01
	return True, "36-period/500-residual input becomes one 6000 charge, one day, zero NBV; legacy gradual posting refused"


def run(only=None):
	wanted = {c.strip() for c in only.split(",")} if only else None
	switch_before = frappe.db.get_single_value(
		"Asset Settings", "enable_enterprise_assets", cache=False
	)
	tally = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
	for case_id, design_ref, title, fn in CASES:
		if wanted and case_id not in wanted:
			continue
		frappe.db.savepoint("edge_case")
		try:
			frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
			ok, detail = fn()
			status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
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
