"""Scrap Transaction — GA-0005-01 v2.16 CH-09 (2026-07-23 review).

"Scrapping is a transaction": every full or partial scrap is recorded
as a first-class submittable document that posts through the existing
GAP-018/019 disposal engine. For composite assets, the user selects
the Active merged component (from the Composite Merge Log) and its
NBV-at-merge snapshot defaults the scrap value.

A Scrap Transaction cannot be cancelled — undo goes through the
GAP-016 restore paths (same-period restore / cross-period restore /
replacement asset), keeping the ledger immutable.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class ScrapTransaction(Document):
	def validate(self):
		from asset_enterprise.depreciation import enterprise_enabled

		if not enterprise_enabled():
			frappe.throw(_("Scrap Transaction requires Enterprise Assets to be enabled."))

		asset = frappe.db.get_value(
			"Asset", self.asset, ["docstatus", "status", "company"], as_dict=True
		)
		if not asset or asset.docstatus != 1:
			frappe.throw(_("Asset {0} must be submitted.").format(self.asset))

		if self.get("composite_component"):
			self._validate_component()

		if self.scrap_type == "Partial Scrap" and not (
			flt(self.scrap_value) or flt(self.percentage)
		):
			frappe.throw(_("Partial Scrap requires a Scrap Value or a Percentage."))

		self._set_posting_accounts()

	def _set_posting_accounts(self):
		"""Client, 20/08: the transaction SHOWS where it will post.

		The account always comes from the Scrapping Type (§3.5 chain) and
		is never editable. The cost centre comes from the same place, but
		a Scrapping Type may allow it to be changed per transaction —
		"Allow Cost Center Change on Scrap" on the type. Resolving here,
		rather than only inside the posting engine, means the user sees
		the accounts before submitting and a missing configuration is
		reported while the document is still a draft.
		"""
		from asset_enterprise.accounts import get_disposal_account, get_disposal_cost_center

		asset = frappe.db.get_value(
			"Asset", self.asset, ["company", "asset_category", "cost_center"], as_dict=True
		)
		if not asset:
			return
		self.company = self.company or asset.company
		self.disposal_account = get_disposal_account(
			asset.company,
			scrapping_type=self.scrapping_type,
			asset_category=asset.asset_category,
		)
		self.allow_cost_center_override = frappe.db.get_value(
			"Scrapping Type", self.scrapping_type, "allow_cost_center_override"
		)
		default_cc = get_disposal_cost_center(asset.company, self.scrapping_type) or asset.cost_center
		if self.allow_cost_center_override:
			self.cost_center = self.cost_center or default_cc
		else:
			# Locked to the type — a value typed in before the type was
			# chosen (or via the API) must not survive.
			self.cost_center = default_cc

	def _validate_component(self):
		"""Component must be an Active Merge Log row of this asset; its
		NBV-at-merge defaults the scrap value (2026-07-23 review)."""
		# A child table has to be queried with its parent doctype named,
		# or frappe finds nothing — this rejected components the Merge Log
		# plainly listed as Active (client sheet item 8).
		rows = frappe.db.sql(
			"""select net_book_value_at_merge
			   from `tabComposite Merge Log Entry`
			   where parent = %s and parenttype = 'Asset'
			     and merged_source_asset = %s and status = 'Active'
			   limit 1""",
			(self.asset, self.composite_component),
			as_dict=True,
		)
		row = rows[0] if rows else None
		if not row:
			frappe.throw(
				_(
					"{0} is not an Active merged component of {1} — pick a component "
					"from the composite's Merge Log."
				).format(self.composite_component, self.asset)
			)
		if self.scrap_type != "Partial Scrap":
			frappe.throw(_("Component scrap is a Partial Scrap of the composite."))
		if not flt(self.scrap_value) and not flt(self.percentage):
			self.scrap_value = flt(row.net_book_value_at_merge)

	def on_submit(self):
		if self.flags.get("auto_recorded"):
			return  # posting already happened in the engine; this doc is the record

		from asset_enterprise import disposal

		frappe.flags.in_scrap_transaction = True
		try:
			if self.scrap_type == "Full Scrap":
				je = disposal.scrap_asset(
					self.asset, scrap_date=self.transaction_date,
					scrapping_type=self.scrapping_type,
					cost_center=self.get("cost_center"),
				)
			else:
				# Component defaulting + Merge Log marking live in the engine.
				je = disposal.partial_scrap_asset(
					self.asset,
					scrap_value=flt(self.scrap_value) or None,
					percentage=flt(self.percentage) or None,
					scrapping_type=self.scrapping_type,
					scrap_date=self.transaction_date,
					composite_component=self.get("composite_component"),
					cost_center=self.get("cost_center"),
				)
		finally:
			frappe.flags.in_scrap_transaction = False
		self.db_set("journal_entry", je, update_modified=False)

		if self.get("composite_component"):
			self.add_comment(
				"Comment",
				_("Component {0} scrapped out of composite {1}.").format(
					self.composite_component, self.asset
				),
			)

	def on_cancel(self):
		# The three GAP-016 paths are for a FULL scrap. After a partial
		# scrap nothing was disposed — only part of the value left — so
		# naming them sent the user looking for buttons that could never
		# apply (client, 25/08, SCR-2026-01459).
		if self.scrap_type == "Partial Scrap":
			frappe.throw(
				_(
					"A Scrap Transaction cannot be cancelled (immutable ledger). To undo "
					"this partial scrap use <b>Reverse Partial Scrap</b> on the asset — "
					"same period — or <b>Cross-Period Reverse Partial Scrap</b> once that "
					"window has passed. The original entry stays posted either way."
				),
				title=_("Use the Reversal Action"),
			)
		frappe.throw(
			_(
				"A Scrap Transaction cannot be cancelled (immutable ledger). To recover "
				"the asset use Restore (same period), Cross-Period Restore, or Create "
				"Replacement Asset — GAP-016."
			)
		)
