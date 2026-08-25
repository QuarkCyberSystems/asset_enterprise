import frappe
from frappe import _
from frappe.utils import flt

from erpnext.assets.doctype.asset_capitalization.asset_capitalization import (
	AssetCapitalization,
)


class EnterpriseAssetCapitalization(AssetCapitalization):
	"""GA-0005-01 v2.14 Asset Capitalization overrides
	(GAP-014 / 015 / 017 / 026 / 035).

	transaction_type routing:
	- "Standard Capitalization" — untouched core behavior.
	- "Capitalized Maintenance" — allows a SUBMITTED target of any type:
	  asset_items are merged via the two-leg Capitalization Clearing GL,
	  and service_items are capitalized onto the target per §12.3
	  (DR Fixed Asset / CR Service Expense — TC-027 / TC-046). Both may
	  appear on one document. Stock rows consume inventory and stay on
	  the Asset Repair (Capitalized Repair, GAP-033) route.
	- "Reversal of Capitalized Maintenance" — dedicated counter-doc
	  (per 2026-07-14 meeting): mirrors both legs, pairs FTs, marks
	  Merge Log rows Reversed. Cannot itself be cancelled.

	Reclassification sub-type: readonly source/target categories must
	differ (VR-020); GL is standard Disposal + Addition in one JE — no
	clearing account (§3.6).
	"""

	# ---------------------------------------------------------- validation
	def validate(self):
		ttype = self.get("transaction_type") or "Standard Capitalization"

		# GAP-036 / N1: a grouping asset holds no value and consumes none —
		# it can be neither a Capitalized Maintenance TARGET (it would
		# absorb NBV through the addition leg) nor a merge / reclass
		# SOURCE (it would be disposed into a composite). Reversals are
		# exempt: they unwind history, and a group node could never have
		# been a valid source or target once the guard exists.
		if ttype == "Capitalized Maintenance":
			if self.get("target_asset") and frappe.db.get_value(
				"Asset", self.target_asset, "is_group_node"
			):
				frappe.throw(
					_(
						"{0} is a Grouping Asset and holds no value — it cannot be the "
						"target of Capitalized Maintenance. Capitalize onto the physical "
						"asset instead (GAP-036)."
					).format(self.target_asset),
					title=_("Grouping Asset Has No Value"),
				)
			for row in self.get("asset_items") or []:
				if row.get("asset") and frappe.db.get_value(
					"Asset", row.asset, "is_group_node"
				):
					frappe.throw(
						_(
							"{0} is a Grouping Asset and holds no value — it cannot be "
							"merged or reclassified. Move the physical assets grouped "
							"under it instead (GAP-036)."
						).format(row.asset),
						title=_("Grouping Asset Has No Value"),
					)

		# GAP-010 / VR-011: sources leaving via capitalization must be
		# fully invoiced first (reversals exempt — sources already left).
		if (
			self._enterprise()
			and ttype != "Reversal of Capitalized Maintenance"
			and self.get("asset_items")
		):
			from asset_enterprise.disposal import assert_fully_invoiced

			for row in self.asset_items:
				if row.get("asset"):
					assert_fully_invoiced(frappe.get_doc("Asset", row.asset))

		if not self._enterprise() or ttype == "Standard Capitalization":
			return super().validate()

		# Category display fields (GAP-014, Phase 11): fetched
		# server-side from the linked assets, both read-only.
		if self.get("asset_items"):
			self.source_asset_category = frappe.db.get_value(
				"Asset", self.asset_items[0].asset, "asset_category"
			)
		if self.get("target_asset"):
			self.target_asset_category = frappe.db.get_value(
				"Asset", self.target_asset, "asset_category"
			)

		# CM / Reversal validations (core validate assumes the standard
		# consume-items pipeline, which CM does not use).
		if ttype == "Capitalized Maintenance":
			self._validate_cm()
		elif ttype == "Reversal of Capitalized Maintenance":
			if not self.get("reversal_of_capitalization"):
				# Raised BY cancelling a Capitalized Maintenance, which sets
				# the back-link before inserting. The field is read-only, so
				# a document reaching here without one was typed by hand
				# (client, 25/08). The old message named the missing field
				# and invited the user to fill in something they cannot.
				frappe.throw(
					_(
						"A Reversal of Capitalized Maintenance is raised automatically when "
						"the original capitalization is cancelled — it cannot be created by "
						"hand. Open the capitalization you want to undo and cancel it."
					),
					title=_("Not a Manual Transaction Type"),
				)

	def _validate_cm(self):
		reclass = (
			self.get("transaction_sub_type") == "Reclassification / Asset Category Transfer"
		)
		# Reclassification may name the NEW MATERIAL instead of a
		# pre-created asset — the transaction creates the asset, like
		# purchasing, with available-for-use = posting date (client,
		# 19/08). Everything else needs a target Asset.
		if reclass and self.get("target_item"):
			# The Target Item is the input on this sub-type and the Target
			# Asset picker is hidden, so a value left behind by a sub-type
			# switch must not silently win — it did on ACC-ASC-2026-00035,
			# where the named item was ignored in favour of an unrelated
			# asset picked earlier (client, 24/08).
			self.target_asset = None
			self.target_asset_name = None
			return self._validate_reclassification_item()
		if not self.get("target_asset"):
			if reclass:
				frappe.throw(
					_(
						"Reclassification requires either a Target Item (the new-category "
						"material — the asset is created by this transaction) or a "
						"pre-created draft Target Asset."
					)
				)
			frappe.throw(_("Capitalized Maintenance requires a target Asset."))
		target = frappe.get_doc("Asset", self.target_asset)
		# Reclassification pre-creates the new-category asset as a DRAFT
		# (Phase 11 F5); every other CM needs a submitted target.
		if target.docstatus == 2 or (target.docstatus != 1 and not reclass):
			frappe.throw(_("Capitalized Maintenance target must be a submitted Asset."))
		# VR-037 (v2.16): "Capitalized" no longer blocks — a composite that
		# was itself capitalized stays a valid CM target (2026-07-23 review).
		if target.status in ("Sold", "Scrapped"):
			frappe.throw(
				_("Target Asset {0} is {1} — not eligible for Capitalized Maintenance.").format(
					target.name, target.status
				)
			)
		# Stock rows are IN SCOPE: §3.4 describes this transaction as
		# "Add item/service/asset", and nothing in GAP-014 excludes stock.
		# The refusal that stood here was invented, exactly as the one that
		# used to reject service rows was (client, 25/08). Consumption goes
		# through a Material Issue in merge.consume_stock_items() — core's
		# own capitalization writes raw Stock Ledger Entries, which the
		# periodic-valuation engine does not route, so a kernel-valued item
		# consumed that way would never be valued.
		# Core computes the service row's amount in client script only, so
		# a row created any other way (API, import, test) reached submit
		# with amount 0 and capitalized nothing at all.
		for row in self.get("service_items") or []:
			if not flt(row.get("amount")) and flt(row.get("qty")) and flt(row.get("rate")):
				row.amount = flt(row.qty) * flt(row.rate)

		# Same trap one grid along: core fills a consumed row's value
		# columns in set_asset_values(), which lives in the core validate
		# this branch deliberately does not call. In the form the JS fetch
		# hides it; a document built any other way stored 0.00 against a
		# 35,000 asset (client, 25/08). Resolved through the modules so the
		# ledger-derived wrappers apply (GAP-006).
		import erpnext.assets.doctype.asset.asset as core_asset
		import erpnext.assets.doctype.asset.depreciation as core_depr

		from asset_enterprise.asset_values import recalculate_asset_values

		for row in self.get("asset_items") or []:
			if not row.get("asset"):
				continue
			book = row.get("finance_book") or self.get("finance_book")
			# The ledger is the fallback for both. Core's value-at-a-date
			# path builds a temporary schedule and refuses outright for
			# some states ("This asset already has 0 depreciation periods
			# booked"), which must not stop a document being saved — the
			# merge itself reads the ledger anyway.
			derived = flt(recalculate_asset_values(row.asset, save=False)["net_book_value"])
			try:
				row.current_asset_value = flt(
					core_asset.get_asset_value_after_depreciation(row.asset, finance_book=book)
				)
			except Exception:
				row.current_asset_value = derived
			try:
				row.asset_value = flt(
					core_depr.get_value_after_depreciation_on_disposal_date(
						row.asset, self.posting_date, finance_book=book
					)
				)
			except Exception:
				row.asset_value = derived

		# GAP-017 + living-target extension (client, 19/08): Extended Life
		# months on a LIVING target extend the current end of life — a
		# real overhaul scenario — handled in merge._resupersede. The
		# fully-depreciated TREATMENT select stays scoped to a target
		# already down to salvage (its options are meaningless on a
		# living asset); shortening a life goes through the Useful Life
		# Adjustment, which owns the exhaustion mechanics.
		months = flt(self.get("extended_life_months") or 0)
		days = flt(self.get("extended_life_days") or 0)
		if months < 0 or days < 0:
			frappe.throw(
				_(
					"Extended Life cannot be negative on a Capitalized Maintenance. To "
					"shorten an asset's useful life, post a Useful Life Adjustment."
				)
			)
		if target.docstatus == 1 and self.get("fully_depreciated_treatment"):
			from asset_enterprise.asset_values import recalculate_asset_values

			nbv = flt(recalculate_asset_values(target.name, save=False)["net_book_value"])
			salvage = flt(
				frappe.db.get_value(
					"Asset Finance Book", {"parent": target.name},
					"expected_value_after_useful_life",
				)
				or 0
			)
			if nbv > salvage + 0.005:
				frappe.throw(
					_(
						"Fully-Depreciated Target Treatment applies only when the target's "
						"NBV is already down to salvage — {0} still carries NBV {1}. To "
						"extend this asset's life with the merge, just set Extended Life "
						"(Months) and/or (Days); the current end of life moves by that much."
					).format(target.name, frappe.format_value(nbv, {"fieldtype": "Currency"}))
				)

		# §12.3 / TC-027 / TC-046: service costs may be capitalized onto a
		# submitted asset; asset rows merge components. At least one.
		if not any(
			self.get(table) for table in ("asset_items", "service_items", "stock_items")
		):
			frappe.throw(
				_(
					"Capitalized Maintenance requires at least one row: a source Asset "
					"(component merge), a Service (cost capitalization), or a Stock item "
					"(materials consumed)."
				)
			)

		if self.get("transaction_sub_type") == "Reclassification / Asset Category Transfer":
			self._validate_reclassification(target)

		self._validate_fully_depreciated_choice(target)

	def _validate_reclassification_item(self):
		"""Target named as a material: the item must be a fixed-asset
		item whose category DIFFERS from the source's (VR-020)."""
		item = frappe.db.get_value(
			"Item", self.target_item, ["is_fixed_asset", "asset_category", "disabled"], as_dict=True
		)
		if not item:
			frappe.throw(_("Target Item {0} does not exist.").format(self.target_item))
		if item.disabled or not item.is_fixed_asset:
			frappe.throw(_("Target Item {0} must be an enabled fixed-asset item.").format(self.target_item))
		if not item.asset_category:
			frappe.throw(_("Target Item {0} has no Asset Category.").format(self.target_item))
		self.target_asset_category = item.asset_category
		if not self.get("asset_items"):
			frappe.throw(_("Reclassification requires the source Asset row."))
		for row in self.asset_items:
			src_cat = frappe.db.get_value("Asset", row.asset, "asset_category")
			if src_cat == item.asset_category:
				frappe.throw(
					_(
						"Reclassification requires the source category ({0}) to differ "
						"from the target item's category ({1})."
					).format(src_cat, item.asset_category)
				)

	def _validate_reclassification(self, target):
		# VR-020: both category fields are read-only, fetched from the
		# linked Assets; block a same-category no-op.
		for row in self.asset_items:
			src_cat = frappe.db.get_value("Asset", row.asset, "asset_category")
			if src_cat == target.asset_category:
				frappe.throw(
					_(
						"Reclassification requires the source category ({0}) to differ "
						"from the target category ({1})."
					).format(src_cat, target.asset_category)
				)

	def _validate_fully_depreciated_choice(self, target):
		from asset_enterprise.overrides.asset_repair import is_fully_depreciated

		if is_fully_depreciated(target.name) and not self.get("fully_depreciated_treatment"):
			frappe.throw(
				_(
					"Target composite {0} is fully depreciated. Choose a Fully "
					"Depreciated Target Treatment: 'Expense Immediately' or "
					"'Add Value and Extend Life' (GAP-014, per 2026-07-14 meeting)."
				).format(target.name)
			)

	# -------------------------------------------------------------- submit
	def before_submit(self):
		if (
			self._enterprise()
			and self.get("transaction_type") == "Reversal of Capitalized Maintenance"
		):
			return  # a reversal carries no consumed items by design
		super().before_submit()

	def on_submit(self):
		ttype = self.get("transaction_type") or "Standard Capitalization"
		if not self._enterprise() or ttype == "Standard Capitalization":
			return super().on_submit()

		from asset_enterprise import merge

		if ttype == "Capitalized Maintenance":
			if self.get("transaction_sub_type") == "Reclassification / Asset Category Transfer":
				# Phase 11 F5: category transfer posts its own model —
				# no clearing, gross+accum re-established, no Merge Log.
				je = merge.reclassify(self)
				self.add_comment(
					"Comment", _("Reclassification posted via Journal Entry {0}.").format(je)
				)
				return
			if self.get("asset_items"):
				je = merge.merge_sources_into_composite(self)
				self.add_comment("Comment", _("Merge posted via Journal Entry {0}.").format(je))
			# §3.4 "Add item/service/asset": materials go out through a
			# Material Issue and are capitalized onto the target.
			se, stock_je = merge.consume_stock_items(self)
			if se:
				self.add_comment(
					"Comment",
					_("Materials issued on {0}{1}.").format(
						se, _(" and capitalized via {0}").format(stock_je) if stock_je else ""
					),
				)
			# §12.3 / TC-027 / TC-046: service costs capitalized onto the
			# submitted target (may accompany a component merge).
			svc_je = merge.capitalize_service_costs(self)
			if svc_je:
				self.add_comment(
					"Comment",
					_("Service capitalization posted via Journal Entry {0}.").format(svc_je),
				)
		else:  # Reversal of Capitalized Maintenance
			je = merge.reverse_merge(self)
			self.add_comment("Comment", _("Reversal posted via Journal Entry {0}.").format(je))

	# -------------------------------------------------------------- cancel
	def on_cancel(self):
		ttype = self.get("transaction_type") or "Standard Capitalization"
		if not self._enterprise() or ttype == "Standard Capitalization":
			return super().on_cancel()

		if ttype == "Reversal of Capitalized Maintenance":
			frappe.throw(
				_(
					"{0} is a Reversal of Capitalized Maintenance and cannot be "
					"cancelled. To undo it, submit a fresh Capitalized Maintenance."
				).format(self.name)
			)

		# VR-042: the composite must be able to give the merged value back.
		merged_nbv = sum(
			frappe.utils.flt(r.net_book_value_at_merge)
			for r in frappe.get_all(
				"Composite Merge Log Entry",
				filters={
					"parent": self.target_asset,
					"merged_via_capitalization": self.name,
					"status": "Active",
				},
				fields=["net_book_value_at_merge"],
			)
		)
		# service capitalizations add value to the target too (§12.3)
		service_value = sum(
			frappe.utils.flt(r.amount)
			for r in frappe.get_all(
				"Financial Treatment",
				filters={
					"source_doctype": "Asset Capitalization",
					"source_name": self.name,
					"transaction_type": "Capitalized Maintenance — Service",
					"status": "Posted",
				},
				fields=["amount"],
			)
		)
		reversal_value = merged_nbv + service_value
		if reversal_value > 0:
			from asset_enterprise.asset_values import assert_nbv_covers_reversal

			assert_nbv_covers_reversal(
				self.target_asset, reversal_value,
				context=_("Capitalized Maintenance {0}").format(self.name),
			)

		# Cancel of a CM -> auto-create the dedicated reversal doc.
		self.ignore_linked_doctypes = (
			"Asset Depreciation Schedule",  # triggered_by dynamic link
			"GL Entry",
			"Asset",
			"Asset Capitalization",
			"Financial Treatment",
			"Asset Activity",
			"Journal Entry",
			# the Material Issue that consumed the stock links back here,
			# and the reversal's Material Receipt links to the reversal —
			# neither may block the counter-document being raised (the same
			# exemption Asset Repair carries for its own stock entries)
			"Stock Entry",
			"Stock Ledger Entry",
		)
		reversal = frappe.get_doc(
			{
				"doctype": "Asset Capitalization",
				"transaction_type": "Reversal of Capitalized Maintenance",
				"reversal_of_capitalization": self.name,
				"target_asset": self.target_asset,
				"target_item_code": self.get("target_item_code"),
				"company": self.company,
				"posting_date": frappe.utils.nowdate(),
				"posting_time": frappe.utils.nowtime(),
				"entry_type": self.get("entry_type"),
			}
		)
		reversal.flags.ignore_permissions = True
		reversal.flags.ignore_links = True
		reversal.flags.ignore_mandatory = True
		reversal.insert()
		reversal.submit()

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()
