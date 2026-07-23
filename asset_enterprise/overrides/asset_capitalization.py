import frappe
from frappe import _

from erpnext.assets.doctype.asset_capitalization.asset_capitalization import (
	AssetCapitalization,
)


class EnterpriseAssetCapitalization(AssetCapitalization):
	"""GA-0005-01 v2.14 Asset Capitalization overrides
	(GAP-014 / 015 / 017 / 026 / 035).

	transaction_type routing:
	- "Standard Capitalization" — untouched core behavior.
	- "Capitalized Maintenance" — allows a SUBMITTED composite target;
	  asset_items are merged via the two-leg Capitalization Clearing GL
	  (merge.py). Scope note: CM handles asset merges only — stock /
	  service additions to an existing asset flow through Asset Repair
	  (Capitalized Repair, GAP-033) or Standard Capitalization.
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
		if not self._enterprise() or ttype == "Standard Capitalization":
			return super().validate()

		# CM / Reversal validations (core validate assumes the standard
		# consume-items pipeline, which CM does not use).
		if ttype == "Capitalized Maintenance":
			self._validate_cm()
		elif ttype == "Reversal of Capitalized Maintenance":
			if not self.get("reversal_of_capitalization"):
				frappe.throw(_("Reversal Of Capitalization is required."))

	def _validate_cm(self):
		if not self.get("target_asset"):
			frappe.throw(_("Capitalized Maintenance requires a target Asset."))
		target = frappe.get_doc("Asset", self.target_asset)
		if target.docstatus != 1:
			frappe.throw(_("Capitalized Maintenance target must be a submitted Asset."))
		if target.status in ("Sold", "Scrapped", "Capitalized"):
			frappe.throw(
				_("Target Asset {0} is {1} — not eligible for Capitalized Maintenance.").format(
					target.name, target.status
				)
			)
		if self.get("stock_items") or self.get("service_items"):
			frappe.throw(
				_(
					"Capitalized Maintenance merges existing Assets only. For stock or "
					"service additions use Asset Repair (Capitalized Repair) or "
					"Standard Capitalization."
				)
			)
		if not self.get("asset_items"):
			frappe.throw(_("Capitalized Maintenance requires at least one source Asset row."))

		if self.get("transaction_sub_type") == "Reclassification / Asset Category Transfer":
			self._validate_reclassification(target)

		self._validate_fully_depreciated_choice(target)

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
			je = merge.merge_sources_into_composite(self)
			self.add_comment("Comment", _("Merge posted via Journal Entry {0}.").format(je))
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

		# Cancel of a CM -> auto-create the dedicated reversal doc.
		self.ignore_linked_doctypes = (
			"GL Entry",
			"Asset",
			"Asset Capitalization",
			"Financial Treatment",
			"Asset Activity",
			"Journal Entry",
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
