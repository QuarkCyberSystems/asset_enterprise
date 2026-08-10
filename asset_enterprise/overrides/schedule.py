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

	def validate(self):
		super().validate()
		self._protect_posted_rows()

	def validate_update_after_submit(self):
		# Submitted-schedule saves skip validate() — the posted-row
		# protection must run here (VR-036, Phase 11b).
		super().validate_update_after_submit()
		self._protect_posted_rows()

	def _protect_posted_rows(self):
		"""VR-036 (Phase 11b): a save that would DROP posted rows (rows
		carrying a Journal Entry) is rejected — posted history is
		immutable; reschedules go through supersession."""
		from asset_enterprise.depreciation import enterprise_enabled

		if self.is_new() or not enterprise_enabled():
			return
		posted_in_db = set(
			frappe.get_all(
				"Depreciation Schedule",
				filters={"parent": self.name, "journal_entry": ("!=", "")},
				pluck="journal_entry",
			)
		)
		current = {
			row.journal_entry for row in (self.get("depreciation_schedule") or []) if row.journal_entry
		}
		missing = posted_in_db - current
		if missing:
			frappe.throw(
				frappe._(
					"This change would drop {0} posted depreciation row(s) ({1}) — posted "
					"rows are immutable (VR-036). Reschedule via supersession instead."
				).format(len(missing), ", ".join(sorted(missing)[:3]))
			)

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
