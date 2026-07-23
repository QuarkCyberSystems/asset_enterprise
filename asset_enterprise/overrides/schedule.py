import frappe
from frappe import _
from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	AssetDepreciationSchedule,
)


class EnterpriseSchedule(AssetDepreciationSchedule):
	"""GA-0005-01 v2.14 Asset Depreciation Schedule overrides.

	GAP-031/032 (Phase 3): supersession support. Core forbids a second
	schedule per asset+finance-book with docstatus < 2 regardless of
	status; under the supersession model the OLD schedule stays
	submitted with status "Superseded" while the NEW Active one is
	inserted, so the duplicate check must ignore Superseded schedules.
	Reschedule flow itself is wrapped in overrides/patches.py
	(supersede_and_regenerate — db_set, never .cancel()).
	"""

	def validate_another_asset_depr_schedule_does_not_exist(self):
		finance_book_filter = ["finance_book", "is", "not set"]
		if self.finance_book:
			finance_book_filter = ["finance_book", "=", self.finance_book]

		asset_depr_schedule = frappe.db.exists(
			"Asset Depreciation Schedule",
			[
				["asset", "=", self.asset],
				finance_book_filter,
				["docstatus", "<", 2],
				["status", "!=", "Superseded"],  # GAP-031
			],
		)

		if asset_depr_schedule and asset_depr_schedule != self.name:
			if self.finance_book:
				frappe.throw(
					_(
						"Asset Depreciation Schedule {0} for Asset {1} and Finance Book {2} already exists."
					).format(asset_depr_schedule, self.asset, self.finance_book)
				)
			else:
				frappe.throw(
					_("Asset Depreciation Schedule {0} for Asset {1} already exists.").format(
						asset_depr_schedule, self.asset
					)
				)
