import frappe
from frappe import _
from frappe.utils import flt, now, nowdate

from erpnext.assets.doctype.asset_repair.asset_repair import AssetRepair


class EnterpriseAssetRepair(AssetRepair):
	"""GA-0005-01 v2.14 Asset Repair overrides (GAP-033 / VR-038).

	Standard Repair (forward): core posts Dr Fixed Asset / Cr expense
	and reschedules (our supersession wrapper takes the reschedule when
	enabled); we additionally record the impact through the TCC.

	Reverse Mode: cancelling a capitalized Standard Repair never calls
	the destructive core path (make_gl_entries(cancel=True) +
	update_asset_value db_set). Instead a Reversal Asset Repair
	(same doctype, transaction_type=Reversal) is auto-created and
	submitted; its on_submit posts the mirror JE via
	make_reverse_gl_entries, returns consumed stock via a Material
	Receipt Stock Entry (Sales-Return-style, per 2026-07-14 meeting),
	pairs the FTs through tcc.reverse, and supersedes the schedule.

	Gates: reversal prohibited on fully-depreciated assets (VR-038);
	a Reversal Repair itself cannot be cancelled (loop guard).
	"""

	# ------------------------------------------------------------- submit
	def on_submit(self):
		if self._enterprise() and self.get("transaction_type") == "Reversal":
			return self._submit_reversal()

		if self._enterprise():
			frappe.flags.ae_defer_reschedule = self.asset  # regenerate after the TCC
		try:
			super().on_submit()
		finally:
			frappe.flags.ae_defer_reschedule = None

		if self._enterprise() and self.get("capitalize_repair_cost"):
			from asset_enterprise import tcc

			tcc.apply(
				source_doc=self,
				category="Addition",
				transaction_type="Capitalized Repair",
				asset=self.asset,
				posting_date=self.completion_date or nowdate(),
				amount=flt(self.total_repair_cost),
				hav_delta=flt(self.total_repair_cost),
				life_delta_months=flt(self.get("increase_in_asset_life") or 0),
			)
			from asset_enterprise.depreciation import regenerate_after_value_change

			regenerate_after_value_change(
				self.asset, self.completion_date or nowdate(),
				_("Capitalized Repair {0} — prospective recalculation").format(self.name),
				triggered_by=self,
			)

	def _submit_reversal(self):
		"""Reversal Repair on_submit — replaces the core forward path."""
		source_name = self.get("reversal_of_repair")
		if not source_name:
			frappe.throw(_("Reversal Repair requires Reversal Of Repair."))
		source = frappe.get_doc("Asset Repair", source_name)

		self._fully_depreciated_gate(source)

		# 1. Mirror JE of the source repair's GL (original stays posted).
		from erpnext.accounts.general_ledger import make_reverse_gl_entries

		make_reverse_gl_entries(voucher_type="Asset Repair", voucher_no=source.name)

		# 2. Stock return — Material Receipt of the consumed items
		#    (Sales-Return pattern; valuation per Item method, GA-0003).
		se_name = None
		if source.get("stock_items"):
			se = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Receipt",
					"company": self.company or source.company,
					"posting_date": nowdate(),
					"items": [
						{
							"item_code": row.item_code,
							"qty": flt(row.consumed_quantity),
							"t_warehouse": row.warehouse,
							"basic_rate": flt(row.valuation_rate),
							"allow_zero_valuation_rate": 0,
						}
						for row in source.stock_items
						if flt(row.consumed_quantity)
					],
				}
			)
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
			se_name = se.name
			self.add_comment(
				"Comment", _("Consumed stock returned via Stock Entry {0}.").format(se_name)
			)

		# 3. Pair the Financial Treatments.
		from asset_enterprise import tcc

		original_ft = frappe.db.get_value(
			"Financial Treatment",
			{"source_doctype": "Asset Repair", "source_name": source.name, "status": "Posted"},
			"name",
		)
		if original_ft:
			tcc.reverse(original_ft, self, posting_date=nowdate())

		# 4. Prospective schedule from the reduced base — resuming from
		# the last POSTED row, with today (the reversal) as the
		# rate-change boundary (19/08 caller audit).
		from asset_enterprise.depreciation import (
			last_posted_schedule_date,
			supersede_and_regenerate,
		)

		from frappe.utils import getdate

		last_posted = last_posted_schedule_date(self.asset)
		try:
			supersede_and_regenerate(
				self.asset,
				as_of_date=getdate(last_posted) if last_posted else nowdate(),
				rate_change_date=getdate(nowdate()) if last_posted else None,
				reason=_("Reversal Repair {0} of {1}").format(self.name, source.name),
				triggered_by=self,
			)
		except frappe.ValidationError:
			pass  # asset without an Active schedule (no depreciation) — nothing to supersede

		source.db_set("reversed_by_repair", self.name, update_modified=False)

		from asset_enterprise.tcc import add_snapshot_activity

		add_snapshot_activity(
			self.asset,
			_("Repair {0} reversed via Reversal Repair {1}; original JE remains posted.").format(
				source.name, self.name
			),
			transaction_type="Reversal",
		)

	# ------------------------------------------------------------- cancel
	def on_cancel(self):
		if not self._enterprise():
			return super().on_cancel()

		if self.get("transaction_type") == "Reversal":
			frappe.throw(
				_(
					"{0} is a Reversal Repair and cannot be cancelled. "
					"To undo it, submit a fresh Standard Repair."
				).format(self.name)
			)

		if not self.get("capitalize_repair_cost"):
			return super().on_cancel()

		self._fully_depreciated_gate(self)

		# Replace the destructive core path with a Reversal Repair. The
		# reversal + FT/Activity rows deliberately link this doc.
		self.ignore_linked_doctypes = (
			"Asset Depreciation Schedule",  # triggered_by dynamic link
			"GL Entry",
			"Stock Ledger Entry",
			"Stock Entry",  # core's consumption SE + our return SE link this repair
			"Asset Repair",
			"Financial Treatment",
			"Asset Activity",
			"Journal Entry",
		)
		self.cancel_sabb()

		reversal = frappe.get_doc(
			{
				"doctype": "Asset Repair",
				"asset": self.asset,
				"company": self.company,
				"failure_date": self.failure_date,
				"completion_date": now(),
				"repair_status": "Completed",
				"repair_cost": 0,
				"capitalize_repair_cost": 0,  # financials flow via _submit_reversal
				"cost_center": self.get("cost_center"),
				"project": self.get("project"),
				"transaction_type": "Reversal",
				"reversal_of_repair": self.name,
				"description": _("Reversal of {0}").format(self.name),
			}
		)
		reversal.flags.ignore_permissions = True
		# Source is docstatus=2 mid-cancel; the back-link is the audit trail.
		reversal.flags.ignore_links = True
		reversal.insert()
		reversal.submit()

	def _fully_depreciated_gate(self, repair_source):
		"""VR-038: block reversal when the asset is fully depreciated.
		Generalized by VR-042 (2026-07-23 review): the reversal must be
		covered by current NBV — full depreciation is the limiting case."""
		if is_fully_depreciated(repair_source.asset):
			frappe.throw(
				_(
					"Asset {0} is fully depreciated. Repair reversal is not permitted; "
					"handle any correction via Asset Value Adjustment."
				).format(repair_source.asset)
			)
		if repair_source.get("capitalize_repair_cost"):
			from asset_enterprise.asset_values import assert_nbv_covers_reversal

			assert_nbv_covers_reversal(
				repair_source.asset,
				flt(repair_source.total_repair_cost),
				context=_("Repair {0}").format(repair_source.name),
			)

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()


def is_fully_depreciated(asset_name):
	"""NBV at salvage AND no unposted schedule rows left."""
	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset_name, save=False)
	# v16: salvage lives on the finance book (expected_value_after_useful_life).
	salvage = flt(
		frappe.db.get_value(
			"Asset Finance Book", {"parent": asset_name}, "expected_value_after_useful_life"
		)
		or 0
	)
	unposted = frappe.db.sql(
		"""
		select count(*) from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') = ''
		""",
		asset_name,
	)[0][0]
	has_schedule = frappe.db.exists(
		"Asset Depreciation Schedule", {"asset": asset_name, "status": "Active", "docstatus": 1}
	)
	return bool(has_schedule) and unposted == 0 and flt(values["net_book_value"]) <= salvage
