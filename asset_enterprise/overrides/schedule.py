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
		from asset_enterprise.control_category import normalize_schedule
		normalize_schedule(self)
		self._protect_posted_rows()
		self._render_rate_breakdowns()

	def _render_rate_breakdowns(self):
		"""Rate Breakdown is derived, never typed: one line per rate the
		engine used inside the period, read from rate_segments. Rendered
		here, at the one point every generation passes through, so no
		writer can store a composition without also showing it (client,
		16/09 — "save the 19-day rate … make it visible")."""
		from asset_enterprise.depreciation import rate_breakdown_text

		for row in self.get("depreciation_schedule") or []:
			if row.meta.has_field("rate_breakdown"):
				row.rate_breakdown = rate_breakdown_text(row.get("rate_segments"))

	def before_cancel(self):
		# GAP-031: a schedule is never cancelled — it is superseded. The
		# one legitimate cancel is the GA-0001-01 asset reversal, where
		# core cancels the asset's schedules as part of unwinding the
		# asset itself (flag set by EnterpriseAsset.on_cancel). Anything
		# else — the desk's Cancel All dialog, a manual cancel on the
		# form — is refused (client, 19/08).
		from asset_enterprise.depreciation import enterprise_enabled

		if not enterprise_enabled():
			return
		if frappe.flags.get("ae_asset_reversal") == self.asset:
			# The superseded generations stay submitted and point at this
			# one (`superseded_by`) and the reversal that caused it
			# (`triggered_by`); they are the asset's history, not
			# dependants. Since a depreciation reversal supersedes the
			# schedule (§4.9.1), the GAP-027 sequence — reverse the
			# depreciation, then cancel the asset — always meets them.
			self.ignore_linked_doctypes = tuple(
				set(tuple(self.get("ignore_linked_doctypes") or ()))
				| {"Asset Depreciation Schedule", "Journal Entry"}
			)
			return
		frappe.throw(
			frappe._(
				"Depreciation schedules are never cancelled under the immutable model — "
				"they are superseded by the transaction that changes them. To reverse the "
				"underlying change, cancel THAT document (adjustment, capitalization, "
				"repair); to unwind the asset itself, cancel the Asset."
			)
		)

	def cancel_depreciation_entries(self):
		"""Core cancels every depreciation JE of a schedule the asset reversal
		cancels. Under the immutable model the asset reversal is refused
		until each posted JE has been undone by a GA-0001-01 Reversal JE
		(`EnterpriseAsset._block_when_depreciation_posted`), and cancelling a
		JE that a live reversal names fails frappe's link check - so a
		reversed JE stays posted, paired with its reversal, exactly as the
		asset's own guard already counts it. A JE with no live reversal keeps
		core's behaviour (reached only with the enterprise switch off). One
		query for the schedule."""
		from asset_enterprise.depreciation import enterprise_enabled

		if not enterprise_enabled() or not frappe.get_meta("Journal Entry").has_field("reversal_of"):
			return super().cancel_depreciation_entries()
		jes = [d.journal_entry for d in self.get("depreciation_schedule") if d.journal_entry]
		reversed_jes = set(frappe.get_all("Journal Entry",
			filters={"reversal_of": ("in", jes), "docstatus": 1}, pluck="reversal_of")) if jes else set()
		for d in self.get("depreciation_schedule"):
			if not d.journal_entry or d.journal_entry in reversed_jes:
				continue
			if frappe.db.get_value("Journal Entry", d.journal_entry, "docstatus") == 0:
				frappe.throw(
					_("Cannot cancel Asset Depreciation Schedule {0} as it has a draft journal entry {1}.").format(
						self.name, d.journal_entry)
				)
			frappe.get_doc("Journal Entry", d.journal_entry).cancel()

	def validate_update_after_submit(self):
		# Submitted-schedule saves skip validate() — the posted-row
		# protection must run here (VR-036, Phase 11b).
		super().validate_update_after_submit()
		self._protect_posted_rows()
		self._render_rate_breakdowns()

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

		# supersede_and_regenerate inserts the new generation as a draft
		# while the schedule it replaces is still Active; only that one
		# schedule may coexist with it.
		allowed = {n for n in (self.name, self.flags.get("superseding")) if n}
		asset_depr_schedule = next(
			(
				n
				for n in frappe.get_all(
					"Asset Depreciation Schedule",
					filters=[
						["asset", "=", self.asset],
						finance_book_filter,
						["docstatus", "<", 2],
						["status", "!=", "Superseded"],  # GAP-031
					],
					pluck="name",
				)
				if n not in allowed
			),
			None,
		)
		if asset_depr_schedule:
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
