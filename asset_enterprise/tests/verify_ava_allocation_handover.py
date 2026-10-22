"""Asset Value Adjustment type "Cost Allocation Handover" - Stage B of an
Asset allocation (GA-0002-02 GAP-030, D-079). Run:

    bench --site <site> execute asset_enterprise.tests.verify_ava_allocation_handover.run

Against a stub handover source standing in for whatever app allocates (this
app imports none):

  h1  no source on the site: nothing pending, and a handover is refused
  h2  pending positions of the asset are offered with their account and
      cost centre
  h3  the adjustment is exactly what it takes: the account, cost centre
      and amount come from the positions, not from the form
  h4  submit takes the positions (the source sees the take) and posts
      Dr Fixed Asset / Cr the allocation account on its cost centre; the
      Financial Treatment is an Addition raising the asset's value
  h5  a take beyond pending is the source's refusal
  h6  without lines, refused
  h7  reversed, never cancelled: the Reversal releases the positions before
      its mirror posts; the original stays submitted; the value is restored

Savepoint-rolled-back.
"""

import contextlib
import json
import traceback

import frappe
from frappe.utils import flt, nowdate

from qcs_platform import registry
from qcs_platform.contracts import PendingLine

TYPE = "Cost Allocation Handover"
RESULTS = []


def check(label, ok, detail=""):
	RESULTS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" - {detail}" if detail and not ok else ""))


class StubSource:
	app = "_ae_stub"
	id = "stub.asset"
	receiver_type = "ASSET"

	def __init__(self, company, account, cost_center):
		self.company, self.account, self.cost_center = company, account, cost_center
		self.positions, self.taken, self.released = {}, [], []

	def pending(self, receiver_type, company, filters):
		return [
			PendingLine(receiver_type="ASSET", key=k, company=company, object_doctype="Asset", object_name=asset,
				amount=amount, account=self.account, cost_center=self.cost_center, bucket=bucket)
			for k, (asset, bucket, amount) in self.positions.items()
			if not filters.get("object") or filters["object"] == asset
		]

	def consume(self, document, lines):
		for line in lines:
			pending = self.positions.get(line.key, (None, None, 0))[2]
			if line.amount > pending + 0.005:
				frappe.throw(f"{line.key}: {line.amount} is beyond the pending {pending} (VR-040)")
		self.taken.append((document.name, tuple((l.key, l.amount) for l in lines)))

	def release(self, document):
		self.released.append(document.name)


@contextlib.contextmanager
def source(stub):
	original_one, original_all = registry.handover_source, registry.handover_sources
	registry.handover_source = lambda t: stub if t == "ASSET" else original_one(t)
	registry.handover_sources = lambda: {"ASSET": stub} if stub else {}
	try:
		yield stub
	finally:
		registry.handover_source, registry.handover_sources = original_one, original_all


def run():
	frappe.set_user("Administrator")
	frappe.db.savepoint("ae_alloc")
	try:
		_run()
	except Exception:
		traceback.print_exc()
		raise
	finally:
		frappe.db.rollback(save_point="ae_alloc")
	failed = [r for r in RESULTS if not r[1]]
	print(f"\nallocation handover: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
	if failed:
		raise Exception("allocation handover failures: " + "; ".join(r[0] for r in failed))
	return True


def _allocation_account(company):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"_AE Asset Allocation - {abbr}"
	if not frappe.db.exists("Account", name):
		parent = frappe.db.get_value("Account", {"company": company, "root_type": "Expense", "is_group": 1}, "name", order_by="lft asc")
		frappe.get_doc({"doctype": "Account", "account_name": "_AE Asset Allocation", "company": company,
			"parent_account": parent, "root_type": "Expense"}).insert(ignore_permissions=True)
	return name


def _ava(asset, lines):
	doc = frappe.get_doc({"doctype": "Asset Value Adjustment", "company": asset.company, "asset": asset.name,
		"date": nowdate(), "transaction_type": TYPE, "handover_lines": json.dumps(lines),
		"difference_account": None, "new_asset_value": 0})
	doc.insert(ignore_permissions=True)
	return doc


def _run():
	from asset_enterprise.allocation_handover import get_pending
	from asset_enterprise.setup.test_fixtures import make_test_asset

	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", limit=1, pluck="name")[0]
	asset = make_test_asset(company, gross=100_000, submit=True)
	account = _allocation_account(company)
	cc = frappe.get_cached_value("Company", company, "cost_center")
	stub = StubSource(company, account, cc)
	stub.positions = {"ASSET|ENG": (asset.name, "ENGINEERING", 10_000.0), "ASSET|MAINT": (asset.name, "MAINTENANCE", 5_000.0)}

	with source(None):
		r = get_pending(asset.name, company)
		check("h1 no source: nothing pending, and the form is told so", r["available"] is False and not r["lines"], str(r))
		try:
			_ava(asset, [{"key": "ASSET|ENG", "amount": 10_000.0}])
			check("h1 a handover with no source is refused", False, "saved")
		except frappe.ValidationError as exc:
			check("h1 a handover with no source is refused", "not pending" in str(exc), str(exc)[:150])

	with source(stub):
		r = get_pending(asset.name, company)
		check("h2 pending positions with account and cost centre",
			r["available"] and r["total"] == 15_000.0 and r["account"] == account and r["cost_center"] == cc, str(r))
		try:
			_ava(asset, [])
			check("h6 without lines, refused", False, "saved")
		except frappe.ValidationError as exc:
			check("h6 without lines, refused", "Get Pending" in str(exc), str(exc)[:150])

		before = flt(frappe.db.get_value("Asset", asset.name, "net_purchase_amount"))
		from erpnext.assets.doctype.asset.asset import get_asset_value_after_depreciation

		value_before = flt(get_asset_value_after_depreciation(asset.name))
		doc = _ava(asset, [{"key": k, "amount": p[2]} for k, p in stub.positions.items()])
		check("h3 account, cost centre and amount come from the positions",
			doc.difference_account == account and doc.cost_center == cc
			and flt(doc.new_asset_value) - flt(doc.current_asset_value) == 15_000.0,
			f"{doc.difference_account} {doc.cost_center} {doc.current_asset_value}->{doc.new_asset_value}")
		over = frappe.get_doc("Asset Value Adjustment", _ava(asset, [{"key": "ASSET|ENG", "amount": 12_000.0}]).name)
		try:
			over.submit()
			check("h5 a take beyond pending is the source's refusal", False, "submitted")
		except frappe.ValidationError as exc:
			check("h5 a take beyond pending is the source's refusal", "VR-040" in str(exc), str(exc)[:150])
		frappe.delete_doc("Asset Value Adjustment", over.name, force=True, ignore_permissions=True)
		doc.submit()
		doc.reload()
		check("h4 the source saw the take", stub.taken and stub.taken[-1][0] == doc.name
			and sorted(stub.taken[-1][1]) == [("ASSET|ENG", 10_000.0), ("ASSET|MAINT", 5_000.0)], str(stub.taken))
		check("h4 the document names its source", doc.handover_source == stub.app, doc.handover_source)
		legs = frappe.get_all("GL Entry", filters={"voucher_no": doc.journal_entry, "is_cancelled": 0},
			fields=["account", "debit", "credit", "cost_center"])
		credit = [l for l in legs if l.account == account]
		check("h4 Dr Fixed Asset / Cr the allocation account on its cost centre",
			len(credit) == 1 and flt(credit[0].credit) == 15_000.0 and credit[0].cost_center == cc
			and flt(sum(l.debit for l in legs if l.account != account)) == 15_000.0, str(legs))
		ft = frappe.get_all("Financial Treatment", filters={"source_doctype": "Asset Value Adjustment", "source_name": doc.name},
			fields=["transaction_category", "transaction_type", "status"]) if frappe.db.exists("DocType", "Financial Treatment") else []
		check("h4 the Financial Treatment is an Addition of this type",
			ft and ft[0].transaction_category == "Addition" and ft[0].transaction_type == TYPE, str(ft))
		check("h4 the asset's value rises by 15,000", flt(get_asset_value_after_depreciation(asset.name)) == value_before + 15_000.0,
			f"{value_before} -> {get_asset_value_after_depreciation(asset.name)}")

		frappe.db.savepoint("ae_alloc_cancel")
		try:
			frappe.get_doc("Asset Value Adjustment", doc.name).cancel()
			check("h7 native cancel is refused", False, "cancelled")
		except frappe.ValidationError:
			check("h7 native cancel is refused", True)
		frappe.db.rollback(save_point="ae_alloc_cancel")
		from asset_enterprise.reversal import reverse

		rev = reverse("Asset Value Adjustment", doc.name, posting_date=nowdate())
		rev_name = rev if isinstance(rev, str) else getattr(rev, "name", None) or (rev or {}).get("name")
		check("h7 the Reversal releases the positions", stub.released == [doc.name], str(stub.released))
		check("h7 the original stays submitted", frappe.db.get_value("Asset Value Adjustment", doc.name, "docstatus") == 1)
		check("h7 the value is restored", flt(get_asset_value_after_depreciation(asset.name)) == value_before,
			f"{value_before} vs {get_asset_value_after_depreciation(asset.name)} ({rev_name})")
