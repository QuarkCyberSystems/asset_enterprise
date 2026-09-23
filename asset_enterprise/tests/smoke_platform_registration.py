"""This app's registration with qcs_platform, held to the contract -
bench --site <site> execute asset_enterprise.tests.smoke_platform_registration.run

Runs the platform's ledger-adapter contract runner over the four asset
transactions and a capitalization's Material Issue with real documents
(the verify_tc recipes), then the A-cases of Build 0.1 §12.2: the
enterprise switch gates governance, Asset itself is not governed, a
plain Stock Entry is nobody's, the receipt's cascade hook is declared,
and - with periodic_valuation installed - a routed capitalization issue
is reversed by nobody but the capitalization. Rolled back; throwaway
site only (the fixtures post journal entries).
"""

import frappe
from frappe.utils import add_months, flt, get_last_day, nowdate

from asset_enterprise.platform import OWN, AssetLedgerAdapter, get_registration
from asset_enterprise.setup import verify_tc as tc


def _tag():
	return frappe.generate_hash(length=5)


class Fixtures:
	def __init__(self):
		tc._ensure_fiscal_years(2025, 2032)
		self.company = tc._company()
		self.cat = tc._category(self.company, "TC IT Equipment", suspense=tc._plain(self.company, "Liability"))
		self.item = tc._stock_item()
		self.warehouse = tc._warehouse(self.company)
		tc._scrapping_type("Damage", self.company, tc._plain(self.company, "Expense"))

	# ---------------------------------------------------------- pieces
	def depreciating_asset(self, label):
		start = get_last_day(add_months(nowdate(), -1))
		asset = tc._depreciating_asset(self.company, self.cat, f"AE-PLAT {label} {_tag()}", 1_000_000, start, 60, add_months(nowdate(), -2))
		tc._post_through(asset.name, start)  # VR-043: a value event never predates an owed period
		return asset

	def ava(self, submit):
		asset = self.depreciating_asset("AVA")
		nbv = flt(frappe.db.get_value("Asset", asset.name, "net_book_value"))
		doc = frappe.get_doc({
			"doctype": "Asset Value Adjustment", "asset": asset.name, "company": self.company, "date": nowdate(),
			"transaction_type": "Upward Revaluation", "current_asset_value": nbv, "new_asset_value": nbv + 100_000,
			"difference_account": tc._plain(self.company, "Income") or tc._plain(self.company, "Liability"),
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		if submit:
			doc.submit()
		return doc

	def repair(self, submit):
		asset = tc._plain_asset(self.company, self.cat, f"AE-PLAT Repair {_tag()}", 50_000)
		asset.submit()
		doc = frappe.get_doc({
			"doctype": "Asset Repair", "asset": asset.name, "company": self.company, "failure_date": nowdate(),
			"repair_status": "Completed", "completion_date": nowdate(), "capitalize_repair_cost": 1, "repair_cost": 10_000,
			"cost_center": frappe.db.get_value("Asset", asset.name, "cost_center"),
			"capital_work_in_progress_account": tc._plain(self.company, "Liability"),
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		if submit:
			doc.submit()
		return doc

	def capitalization(self, submit, stock_item=None, warehouse=None):
		asset = self.depreciating_asset("CM")
		doc = frappe.get_doc({
			"doctype": "Asset Capitalization", "company": self.company, "transaction_type": "Capitalized Maintenance",
			"target_asset": asset.name, "posting_date": nowdate(), "set_posting_time": 1,
			"service_items": [{"item_code": tc._service_item(), "qty": 1, "rate": 5_000, "expense_account": tc._plain(self.company, "Expense")}],
		})
		if stock_item:
			doc.append("stock_items", {"item_code": stock_item, "warehouse": warehouse, "stock_qty": 1})
		doc.flags.ignore_permissions = True
		doc.insert()
		if submit:
			doc.submit()
		return doc

	def stock_in(self, item, warehouse):
		# dated today, not two months back as tc._stock_in posts: a routed
		# item needs an Inventory Period for its posting date
		doc = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": self.company,
			"items": [{"item_code": item, "qty": 5, "t_warehouse": warehouse, "basic_rate": 200}],
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()

	def capitalization_issue(self, stock_item=None, warehouse=None):
		"""The Material Issue a capitalization with stock rows raises."""
		stock_item = stock_item or self.item
		warehouse = warehouse or self.warehouse
		self.stock_in(stock_item, warehouse)
		cap = self.capitalization(submit=True, stock_item=stock_item, warehouse=warehouse)
		name = frappe.db.get_value("Stock Entry", {"asset_capitalization": cap.name, "docstatus": 1}, "name")
		assert name, f"capitalization {cap.name} raised no Material Issue"
		return frappe.get_doc("Stock Entry", name)

	def scrap(self):
		asset = tc._plain_asset(self.company, self.cat, f"AE-PLAT Scrap {_tag()}", 40_000)
		asset.submit()
		from asset_enterprise import disposal

		disposal.scrap_asset(asset.name, scrapping_type="Damage")
		name = frappe.db.get_value("Scrap Transaction", {"asset": asset.name, "docstatus": 1}, "name")
		assert name, f"scrap of {asset.name} left no Scrap Transaction"
		return frappe.get_doc("Scrap Transaction", name)

	def plain_issue(self, submit=True):
		self.stock_in(self.item, self.warehouse)
		doc = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Issue", "company": self.company,
			"items": [{"item_code": self.item, "qty": 1, "s_warehouse": self.warehouse}],
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		if submit:
			doc.submit()
		return doc

	# --------------------------------------------------------- contract
	def posted(self, doctype):
		if doctype == "Asset Value Adjustment":
			return self.ava(submit=True)
		if doctype == "Asset Repair":
			return self.repair(submit=True)
		if doctype == "Asset Capitalization":
			return self.capitalization(submit=True)
		if doctype == "Scrap Transaction":
			return self.scrap()
		if doctype == "Stock Entry":
			return self.capitalization_issue()
		return None

	def draft(self, doctype):
		if doctype == "Asset Value Adjustment":
			return self.ava(submit=False)
		if doctype == "Asset Repair":
			return self.repair(submit=False)
		if doctype == "Asset Capitalization":
			return self.capitalization(submit=False)
		if doctype == "Stock Entry":
			return self.plain_issue(submit=False)
		if doctype == "Scrap Transaction":
			asset = tc._plain_asset(self.company, self.cat, f"AE-PLAT Scrap Draft {_tag()}", 40_000)
			asset.submit()
			doc = frappe.get_doc({
				"doctype": "Scrap Transaction", "asset": asset.name, "company": self.company, "scrap_date": nowdate(),
				"scrap_type": "Full Scrap", "scrapping_type": "Damage",
			})
			doc.flags.ignore_permissions = True
			doc.insert()
			return doc
		return None

	def ungoverned(self, doctype):
		if doctype == "Stock Entry":
			return self.plain_issue()
		return None

	def with_dependent(self, doctype):
		return None




def _a_cases(fixtures, adapter, checks):
	from qcs_platform import compat
	from qcs_platform.ledger.dispatcher import LedgerRefusal, ui_state
	from qcs_platform.testkit import isolated

	# A-01 the delete refusal names the doctype's reversal route
	with isolated():
		for doctype in OWN:
			posted = fixtures.posted(doctype)
			refusal = adapter.can_delete(posted)
			checks(f"A-01 {doctype}: a posted document is refused delete with its reversal route",
				refusal is not None and doctype in refusal.message and "cannot be deleted" in refusal.message, str(refusal))

	# A-03 the enterprise switch gates governance
	with isolated():
		posted = fixtures.ava(submit=True)
		checks("A-03 governed while enterprise assets are on", adapter.governs(posted))
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 0)
		checks("A-03 not governed once enterprise assets are off", not adapter.governs(posted))
		frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)

	# A-04 Asset is not governed by this app
	checks("A-04 Asset is not in the adapter's doctypes", "Asset" not in adapter.doctypes)

	# A-05 / A-06 a capitalization's issue is refused without a route; a plain issue is nobody's
	with isolated():
		issue = fixtures.capitalization_issue()
		refusal = adapter.can_cancel(issue)
		checks("A-05 the capitalization's issue is refused on its own, naming the capitalization",
			refusal is not None and issue.asset_capitalization in refusal.message and refusal.route is None, str(refusal))
		state = ui_state("Stock Entry", issue.name)
		checks("A-05 ui_state hides Cancel and offers the capitalization",
			state["hide_cancel"] and [a["label"] for a in state["actions"]] == ["Open Asset Capitalization"], str(state["actions"]))
		try:
			issue.cancel()
			checks("A-05 native cancel is refused", False, "cancelled")
		except LedgerRefusal as exc:
			checks("A-05 native cancel is refused with no route", "asset_enterprise" in exc.owners and exc.route is None, str(exc.owners))
		plain = fixtures.plain_issue()
		state = ui_state("Stock Entry", plain.name)
		checks("A-06 a plain Stock Entry is not this app's", "asset_enterprise" not in (state.get("owners") or ()), str(state.get("owners")))

	# A-07 the receipt's cascade hook is declared, so the overlap check passes
	checks("A-07 pr_before_cancel is declared as a legacy hook", ("Purchase Receipt", "before_cancel") in get_registration().legacy_hooks)
	checks("A-07 verify_install passes with it declared", compat.verify_install() is True)
	# and refuses the site the moment the declaration goes (the real path,
	# over the governed union: this app registers nothing for the receipt)
	from qcs_platform import registry
	from qcs_platform.contracts import Registration

	import asset_enterprise.platform as platform_module

	real = platform_module.get_registration
	platform_module.get_registration = lambda: Registration(app="asset_enterprise", api_version=real().api_version, ledger_adapters=real().ledger_adapters)
	registry.clear_cache()
	try:
		compat.verify_install()
		checks("A-07 with the declaration removed verify_install refuses the site", False, "verified")
	except registry.RegistryError as exc:
		checks("A-07 with the declaration removed verify_install refuses the site", "pr_before_cancel" in str(exc), str(exc)[:140])
	finally:
		platform_module.get_registration = real
		registry.clear_cache()

	from qcs_platform.testkit import migrate_guard_fires

	from asset_enterprise.setup.install import after_migrate

	migrate_guard_fires(checks, "after_migrate refuses a site without the platform first", after_migrate)


def _routed_issue_case(fixtures, adapter, checks):
	"""With periodic_valuation installed: a capitalization's issue carrying a
	routed item is governed by both; valuation's Create Cancellation is
	neither offered nor admitted (§5.1: one owner's route is not another
	owner's permission)."""
	if "periodic_valuation" not in frappe.get_installed_apps():
		print("SKIP routed-issue case (periodic_valuation not installed)")
		return
	from periodic_valuation.periodic_moving_average.cancellation import make_cancellation
	from periodic_valuation.tests.smoke_kernel import ITEM as MAP_ITEM, ensure_masters, get_company
	from qcs_platform.ledger.dispatcher import LedgerRefusal, ui_state
	from qcs_platform.testkit import isolated

	ensure_masters()
	checks("X-AE valuation's smoke company is this suite's company", get_company() == fixtures.company, f"{get_company()} != {fixtures.company}")
	if get_company() != fixtures.company:
		return
	with isolated():
		wh = f"_SMK Stores - {frappe.db.get_value('Company', fixtures.company, 'abbr')}"
		issue = fixtures.capitalization_issue(stock_item=MAP_ITEM, warehouse=wh)
		state = ui_state("Stock Entry", issue.name)
		checks("X-AE both owners on the routed issue", {"asset_enterprise", "periodic_valuation"} <= set(state["owners"]), str(state["owners"]))
		labels = [a["label"] for a in state["actions"]]
		checks("X-AE Create Cancellation is not offered beside the capitalization's refusal", "Create Cancellation" not in labels and labels == ["Open Asset Capitalization"], str(labels))
		try:
			issue.cancel()
			checks("X-AE native cancel refused", False, "cancelled")
		except LedgerRefusal as exc:
			checks("X-AE one dialog, no route", exc.route is None and {"asset_enterprise", "periodic_valuation"} <= set(exc.owners), str(exc.route))
		try:
			make_cancellation("Stock Entry", issue.name)
			checks("X-AE valuation's Cancellation document is refused at validate", False, "made")
		except LedgerRefusal as exc:
			checks("X-AE valuation's Cancellation document is refused at validate", "asset_enterprise" in exc.owners, str(exc.owners))


def run():
	from qcs_platform.testkit import Checks, contract, throwaway_site_only

	throwaway_site_only()
	frappe.set_user("Administrator")
	frappe.flags.mute_emails = True
	checks = Checks("asset_enterprise platform registration")
	try:
		checks("registration names this app", get_registration().app == "asset_enterprise")
		if not frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets"):
			frappe.db.set_single_value("Asset Settings", "enable_enterprise_assets", 1)
		fixtures = Fixtures()
		adapter = AssetLedgerAdapter()
		contract.ledger_adapter(adapter, fixtures, checks)
		_a_cases(fixtures, adapter, checks)
		_routed_issue_case(fixtures, adapter, checks)
	finally:
		frappe.db.rollback()
	checks.summary()
	checks.raise_on_failure()
	return True
