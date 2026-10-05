"""Asset Value Adjustment type "Project Settlement" (PA-008, client option A,
02/10/2026): an addition to an existing asset capitalized by a Project
Settlement Run posts as a real AVA. Run:

    bench --site <site> execute asset_enterprise.tests.verify_ava_project_settlement.run

  p1  created by hand -> refused
  p2  raised under the run's flag -> accepted; the account the run
      supplies is kept (never replaced from the Asset Category)
  p3  life fields on it -> refused (value-only type)
  p4  submit -> JE Dr Fixed Asset / Cr the supplied account. The
      settlement leg carries every dimension set on the AVA (project, WBS,
      cost component, tracking item) although none is mandatory for the
      company; the Fixed Asset leg carries the asset's ACQUISITION
      dimensions (V-08 / gl_attribution.apply_asset_cost_centre_policy:
      cost and accumulated depreciation net at one attribution), not the
      settling project's
  p5  the Financial Treatment is an Addition of type Project Settlement
      raising the asset value
  p7  on an asset with no Active depreciation schedule (a shell not
      depreciating yet) the submit and the cancel leave no "nothing to
      supersede" message in the response
  p6  cancel -> a Reversal AVA of the same type; the original JE stays
      posted and the mirror JE carries each leg's dimensions unchanged

Savepoint-rolled-back.
"""

import traceback

import frappe
from frappe.utils import flt, nowdate

DIMENSIONS = ("project_accounting", "wbs_element", "cost_component", "cost_tracking_item")
OK = {True: "OK", False: "FAIL"}


def run():
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise


def _dimension_values(company):
	"""One existing record per PA dimension the AVA carries; a dimension
	with no record on this site is reported as not exercised."""
	meta = frappe.get_meta("Asset Value Adjustment")
	values, missing = {}, []
	for fieldname in DIMENSIONS:
		df = meta.get_field(fieldname)
		if not df:
			missing.append(fieldname)
			continue
		target = frappe.get_meta(df.options)
		filters = {"company": company} if target.has_field("company") else {}
		if target.is_submittable:
			filters["docstatus"] = ("!=", 2)  # a cancelled record cannot carry a posting
		order = "docstatus desc, creation desc" if target.is_submittable else "creation desc"
		name = frappe.db.get_value(df.options, filters, "name", order_by=order)
		if not name:
			filters.pop("company", None)
			name = frappe.db.get_value(df.options, filters, "name", order_by=order)
		if name:
			values[fieldname] = name
		else:
			missing.append(fieldname)
	# the WBS Element must belong to the row's project (PA VR-040): take a
	# WBS under a submitted project and that project with it
	if "wbs_element" in values and "project_accounting" in values:
		pair = frappe.db.sql(
			"""select w.name, w.project_accounting from `tabWBS Element` w
			   join `tabProject Accounting` p on p.name = w.project_accounting
			   where p.docstatus = 1 and p.company = %s
			     and w.status in ('Active', 'Technically Complete')
			   order by w.creation desc limit 1""",
			company,
		)
		if pair:
			values["wbs_element"], values["project_accounting"] = pair[0]
	return values, missing


def _ava(asset, company, account, **extra):
	doc = frappe.get_doc({
		"doctype": "Asset Value Adjustment", "asset": asset, "company": company,
		"date": nowdate(), "transaction_type": "Project Settlement",
		"current_asset_value": 30_000, "new_asset_value": 31_250,
		"difference_account": account, **extra,
	})
	doc.flags.ignore_permissions = True
	return doc


def _je_rows(je):
	return frappe.get_all("Journal Entry Account", filters={"parent": je},
		fields=["account", "debit", "credit", *DIMENSIONS])


def _run():
	from asset_enterprise.setup.test_fixtures import make_test_asset, pick_company, pick_plain_account

	company = pick_company()
	results = []

	def check(label, ok, detail=""):
		results.append(bool(ok))
		print(f"{label} {OK[bool(ok)]}" + (f" - {detail}" if detail and not ok else ""))

	switch_before = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets", cache=False)
	frappe.db.savepoint("ava_project_settlement")
	try:
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		account = pick_plain_account(company, "Asset")
		asset = make_test_asset(company, gross=30_000, submit=True)
		fixed_asset_account = frappe.db.get_value(
			"Asset Category Account",
			{"parent": asset.asset_category, "company_name": company}, "fixed_asset_account")
		dims, missing = _dimension_values(company)
		if missing:
			print(f"p4/p6  dimensions not exercised (no record on this site): {', '.join(missing)}")
		# the asset was acquired under a DIFFERENT project than the one
		# settling into it, so the two attributions are distinguishable
		other = frappe.db.get_value("Project Accounting",
			{"name": ("!=", dims.get("project_accounting")), "docstatus": 1}, "name")
		acquired = {f: None for f in DIMENSIONS}
		if other and frappe.get_meta("Asset").has_field("project_accounting"):
			frappe.db.set_value("Asset", asset.name, "project_accounting", other, update_modified=False)
			acquired["project_accounting"] = other
		else:
			print("p4     acquisition project not exercised (no second Project Accounting record)")

		# p1: by hand
		try:
			_ava(asset.name, company, account).insert()
			check("p1     manual Project Settlement accepted", False)
		except frappe.ValidationError as e:
			check("p1     manual Project Settlement refused", "cannot be created by hand" in str(e), str(e)[:120])
			frappe.clear_last_message()

		# p3: life fields on a value-only type
		frappe.flags.ae_project_settlement = True
		try:
			try:
				_ava(asset.name, company, account, adjusted_life_months=6).insert()
				check("p3     life fields on Project Settlement accepted", False)
			except frappe.ValidationError as e:
				check("p3     life fields on Project Settlement refused", "changes value, not useful life" in str(e),
					str(e)[:120])
				frappe.clear_last_message()

			# p2: raised by the run
			ava = _ava(asset.name, company, account, **dims)
			frappe.local.message_log = []
			ava.insert()
			check("p2     raised under the run's flag; the supplied account is kept",
				ava.difference_account == account, f"{ava.difference_account} vs {account}")
			ava.submit()
		finally:
			frappe.flags.ae_project_settlement = False

		def _supersede_noise():
			return [m for m in frappe.local.message_log if "nothing to supersede" in str(m)]

		has_schedule = frappe.db.exists("Asset Depreciation Schedule",
			{"asset": asset.name, "status": "Active", "docstatus": 1})
		check("p7     submit on an asset with no schedule: no 'nothing to supersede' message",
			not has_schedule and not _supersede_noise(), str(_supersede_noise()) or "asset has a schedule")

		# p4: the JE
		rows = _je_rows(ava.journal_entry)
		dr = [r for r in rows if flt(r.debit)]
		cr = [r for r in rows if flt(r.credit)]
		legs_ok = (len(dr) == 1 and len(cr) == 1 and dr[0].account == fixed_asset_account
			and cr[0].account == account and flt(dr[0].debit) == 1_250 and flt(cr[0].credit) == 1_250)
		check("p4     JE Dr Fixed Asset 1,250 / Cr supplied account 1,250", legs_ok, str(rows))
		check(f"p4     settlement leg carries the AVA's dimensions ({len(dims)} of {len(DIMENSIONS)})",
			dims and cr and all(cr[0].get(f) == v for f, v in dims.items()), str(cr))
		check("p4     Fixed Asset leg carries the asset's acquisition dimensions",
			dr and all(dr[0].get(f) == v for f, v in acquired.items()), f"{dr} want {acquired}")

		# p5: the treatment
		ft = frappe.db.get_value("Financial Treatment",
			{"source_doctype": "Asset Value Adjustment", "source_name": ava.name},
			["transaction_category", "transaction_type", "status", "hav_delta"], as_dict=True)
		check("p5     Financial Treatment: Addition / Project Settlement / Posted, value +1,250",
			ft and ft.transaction_category == "Addition" and ft.transaction_type == "Project Settlement"
			and ft.status == "Posted" and flt(ft.hav_delta) == 1_250, str(ft))

		# p6: cancel -> Reversal AVA
		from asset_enterprise.api import cancel_ava_with_reversal

		frappe.local.message_log = []
		cancel_ava_with_reversal(ava.name, nowdate())
		check("p7     cancel on an asset with no schedule: no 'nothing to supersede' message",
			not _supersede_noise(), str(_supersede_noise()))
		rev = frappe.db.get_value("Asset Value Adjustment", {"reversal_of_ava": ava.name, "docstatus": 1},
			["name", "transaction_type", "journal_entry"], as_dict=True)
		check("p6     cancel raised a Reversal AVA of type Project Settlement",
			rev and rev.transaction_type == "Project Settlement" and rev.journal_entry, str(rev))
		check("p6     the original JE stays posted",
			frappe.db.get_value("Journal Entry", ava.journal_entry, "docstatus") == 1)
		if rev and rev.journal_entry:
			mrows = _je_rows(rev.journal_entry)
			orig = {r.account: {f: r.get(f) for f in DIMENSIONS} for r in rows}
			mirror = {r.account: {f: r.get(f) for f in DIMENSIONS} for r in mrows}
			mirror_ok = (sum(flt(r.credit) for r in mrows if r.account == fixed_asset_account) == 1_250
				and mirror == orig)
			check("p6     mirror JE Cr Fixed Asset 1,250, each leg's dimensions unchanged", mirror_ok,
				f"{mrows} vs {orig}")
	finally:
		frappe.db.rollback(save_point="ava_project_settlement")
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", switch_before)

	print(f"\n{sum(results)}/{len(results)} checks passed")
	if not all(results):
		raise Exception("Project Settlement AVA checks failed")
