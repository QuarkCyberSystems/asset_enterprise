import frappe
from frappe import _

from erpnext.assets.doctype.asset.asset import Asset


class EnterpriseAsset(Asset):
	"""GA-0005-01 v2.14 Asset overrides.

	Phase 4 (GAP-027 / VR-031): reversal of an Asset with posted
	depreciation is blocked with an error — the user reverses each
	depreciation JE via GA-0001-01 Reversal Journal Entry first, then
	retries. No auto-cascade, per the 2026-07-14 meeting. When no
	posted depreciation exists, the existing core path runs (it already
	uses make_reverse_gl_entries for the booking GL — compliant).

	Later phases: TCC Addition on submit / suspense JE (GAP-001,
	Phase 7 PR-flow), mandatory location VR-040 + single-disposal
	VR-041 (Phase 6).
	"""

	def on_submit(self):
		super().on_submit()
		if self.get("replacement_of_asset"):
			# GAP-016 Path 2: two-way link once the replacement goes live.
			frappe.db.set_value(
				"Asset",
				self.replacement_of_asset,
				"replaced_by_asset",
				self.name,
				update_modified=False,
			)

	def on_cancel(self):
		if self._enterprise():
			self._block_when_depreciation_posted()
		super().on_cancel()

	def _block_when_depreciation_posted(self):
		posted = frappe.db.sql(
			"""
			select count(*) from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.docstatus = 1
			  and ifnull(ds.journal_entry, '') != ''
			""",
			self.name,
		)[0][0]
		if posted:
			frappe.throw(
				_(
					"Asset has {0} posted depreciation entries. Asset reversal under the "
					"immutable ledger requires reversal of these depreciation entries first. "
					"Reverse the linked depreciation JEs (via Reversal Journal Entry per "
					"GA-0001-01) before retrying asset reversal, OR contact accounts to "
					"handle the cleanup."
				).format(posted)
			)

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()
