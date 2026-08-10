import frappe
from frappe import _
from frappe.utils import flt, getdate

from erpnext.assets.doctype.asset.asset import Asset


class EnterpriseAsset(Asset):
	"""GA-0005-01 v2.14 Asset overrides.

	- GAP-001: suspense JE on submit of an existing asset (TCC Addition,
	  Existing-Asset Opening) — DR FA cost / CR Accum opening / CR Asset
	  Suspense NBV.
	- GAP-002: available-for-use date required only when depreciation is
	  on AND the category is not on the receiving-date basis.
	- GAP-003: category flag `calculate_from_receiving_date` makes the
	  linked PR posting date the depreciation start basis.
	- GAP-009: parent_asset tree must stay acyclic (VR-009).
	- GAP-016 Path 2: two-way replacement link on submit.
	- GAP-027 / VR-031: reversal of an Asset with posted depreciation is
	  blocked — the user reverses each depreciation JE via GA-0001-01
	  first. No auto-cascade, per the 2026-07-14 meeting.
	"""

	def validate(self):
		if self._enterprise():
			self._apply_receiving_date_basis()
			# Depreciation without a start basis: core's draft-schedule
			# build crashes on an empty AFU, so the VR-002 error fires
			# here. AFU stays optional in the two designed cases (no
			# depreciation; receiving-date basis, derived above).
			if (
				self.calculate_depreciation
				and not self.available_for_use_date
				and self.asset_type != "Composite Component"
				and not self._receiving_date_basis()
			):
				frappe.throw(_("Available for use date is required (VR-002)."))
			self._validate_tree_acyclic()
			self._validate_pr_row_allocation()
		super().validate()

	def _validate_pr_row_allocation(self):
		"""VR-004 (Phase 11b): amount-side over-allocation check at the
		Asset level — covers assets linked or revalued after PR submit
		(core validates only the qty sum)."""
		row_name = self.get("purchase_receipt_item")
		if not row_name or self.docstatus == 2:
			return
		row = frappe.db.get_value(
			"Purchase Receipt Item", row_name, ["base_net_amount", "idx"], as_dict=True
		)
		if not row or not flt(row.base_net_amount):
			return
		others = flt(
			frappe.db.sql(
				"""select coalesce(sum(net_purchase_amount), 0) from `tabAsset`
				   where purchase_receipt_item = %s and docstatus < 2 and name != %s""",
				(row_name, self.name or ""),
			)[0][0]
		)
		if others + flt(self.net_purchase_amount) > flt(row.base_net_amount) + 0.01:
			frappe.throw(
				_(
					"Total value of assets linked to Purchase Receipt row {0} "
					"({1}) exceeds the row amount {2} (VR-004)."
				).format(row.idx, others + flt(self.net_purchase_amount), row.base_net_amount)
			)

	def validate_update_after_submit(self):
		# Submitted-doc saves skip validate() — re-run the tree check so
		# a parent_asset edit after submit cannot create a cycle (VR-009).
		super().validate_update_after_submit()
		if self._enterprise():
			self._validate_tree_acyclic()
			self._guard_merge_log()

	def on_update(self):
		super().on_update()
		self._sync_asset_tree()

	def on_update_after_submit(self):
		parent_hook = getattr(super(), "on_update_after_submit", None)
		if parent_hook:
			parent_hook()
		self._sync_asset_tree()

	def _sync_asset_tree(self):
		"""GAP-009: the Asset Tree doctype mirrors parent_asset links
		for the native Tree View."""
		if not self._enterprise() or not (
			self.get("parent_asset")
			or frappe.db.exists("Asset Tree", {"child_asset": self.name})
		):
			return
		try:
			from asset_enterprise.asset_enterprise.doctype.asset_tree.asset_tree import (
				sync_asset_tree,
			)

			sync_asset_tree(self.name)
		except Exception:
			frappe.log_error(
				title=f"asset_tree sync failed: {self.name}", message=frappe.get_traceback()
			)

	def _guard_merge_log(self):
		"""VR-039 (Phase 11b): Merge Log rows change only through
		Capitalization submit/reverse and component scrap — direct edits
		via UI/API are rejected server-side."""
		if self.flags.get("via_capitalization"):
			return
		db_rows = {
			r.name: (r.merged_source_asset, r.merged_via_capitalization, r.status)
			for r in frappe.get_all(
				"Composite Merge Log Entry",
				filters={"parent": self.name},
				fields=["name", "merged_source_asset", "merged_via_capitalization", "status"],
			)
		}
		doc_rows = {
			r.name: (r.merged_source_asset, r.merged_via_capitalization, r.status)
			for r in (self.get("merge_log") or [])
			if r.name
		}
		added = [r for r in (self.get("merge_log") or []) if not r.name]
		if added or db_rows != doc_rows:
			frappe.throw(
				_(
					"Composite Merge Log rows are system-maintained (VR-039) — they "
					"change only via Asset Capitalization submit/reversal or component "
					"scrap, never by direct edit."
				)
			)

	def validate_in_use_date(self):
		"""GAP-002: optional on save; on submit required only when
		calculate_depreciation=1 AND not receiving-date basis."""
		if not self._enterprise():
			return super().validate_in_use_date()

		if not self.available_for_use_date:
			if (
				self.calculate_depreciation
				and self.asset_type != "Composite Component"
				and not self._receiving_date_basis()
			):
				frappe.throw(_("Available for use date is required (VR-002)."))
			return

		for d in self.finance_books:
			if getdate(d.depreciation_start_date) < getdate(self.available_for_use_date):
				frappe.throw(
					_(
						"Depreciation Row {0}: Depreciation Posting Date cannot be before "
						"Available-for-use Date"
					).format(d.idx),
					title=_("Incorrect Date"),
				)

	def _receiving_date_basis(self):
		return self.get("asset_category") and frappe.db.get_value(
			"Asset Category", self.asset_category, "calculate_from_receiving_date"
		)

	def _apply_receiving_date_basis(self):
		"""GAP-003: PR posting date becomes the start basis when the
		category flag is on and the user left the AFU date empty."""
		if (
			self.available_for_use_date
			or self.get("is_existing_asset")
			or not self.get("purchase_receipt")
			or not self._receiving_date_basis()
		):
			return
		pr = frappe.db.get_value(
			"Purchase Receipt", self.purchase_receipt, ["posting_date", "docstatus"], as_dict=True
		)
		# VR-003 (Phase 11b): only a SUBMITTED receipt provides the basis.
		if pr and pr.docstatus != 1:
			frappe.throw(
				_(
					"Receiving-date depreciation basis requires the linked Purchase "
					"Receipt {0} to be submitted (VR-003)."
				).format(self.purchase_receipt)
			)
		if pr and pr.posting_date:
			self.available_for_use_date = pr.posting_date

	def _validate_tree_acyclic(self):
		"""GAP-009 / VR-009: parent_asset must not point into the asset's
		own descendant chain."""
		parent = self.get("parent_asset")
		seen = {self.name}
		while parent:
			if parent in seen:
				frappe.throw(
					_(
						"Parent Asset {0} is a descendant of {1} — the asset tree must "
						"stay acyclic (VR-009)."
					).format(self.parent_asset, self.name)
				)
			seen.add(parent)
			parent = frappe.db.get_value("Asset", parent, "parent_asset")

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
			# §5.2 (Phase 11b): both sides of the recovery trail get a
			# history row.
			from asset_enterprise.tcc import add_snapshot_activity

			add_snapshot_activity(
				self.name,
				_("Created as replacement of disposed Asset {0} (GAP-016 Path 2).").format(
					self.replacement_of_asset
				),
				transaction_type="Replacement",
			)
			add_snapshot_activity(
				self.replacement_of_asset,
				_("Replaced by Asset {0} (GAP-016 Path 2).").format(self.name),
				transaction_type="Replacement",
			)
		if self._enterprise():
			self._post_existing_asset_opening()

	def _post_existing_asset_opening(self):
		"""GAP-001: auto-JE via TCC Addition (Existing-Asset Opening).

		Only for existing assets brought in without purchase documents —
		purchased assets get their booking GL from the PR/PI flow.
		"""
		if (
			not self.get("is_existing_asset")
			or self.get("purchase_receipt")
			or self.get("purchase_invoice")
			or self.get("reclassified_from")  # booking came via the reclassification JE
		):
			return

		from asset_enterprise import tcc
		from asset_enterprise.accounts import get_enterprise_account
		from asset_enterprise.rounding import fa_module_round

		gross = flt(self.net_purchase_amount)
		if gross <= 0:
			return
		opening_accum = flt(self.opening_accumulated_depreciation)
		nbv = fa_module_round(gross - opening_accum, self.company)

		aca = frappe.db.get_value(
			"Asset Category Account",
			{"parent": self.asset_category, "company_name": self.company},
			["fixed_asset_account", "accumulated_depreciation_account"],
			as_dict=True,
		)
		if not aca:
			frappe.throw(
				_("Asset Category Account missing for {0} / {1}").format(
					self.asset_category, self.company
				)
			)
		# Throws VR-001-style when unconfigured (TC-002).
		suspense = get_enterprise_account("asset_suspense_account", self.company, self.asset_category)

		accounts = [
			{
				"account": aca.fixed_asset_account,
				"debit_in_account_currency": gross,
				"cost_center": self.get("cost_center"),
				"reference_type": "Asset",
				"reference_name": self.name,
			}
		]
		if opening_accum:
			accounts.append(
				{
					"account": aca.accumulated_depreciation_account,
					"credit_in_account_currency": opening_accum,
					"reference_type": "Asset",
					"reference_name": self.name,
				}
			)
		if nbv:
			accounts.append(
				{
					"account": suspense,
					"credit_in_account_currency": nbv,
					"cost_center": self.get("cost_center"),
					"reference_type": "Asset",
					"reference_name": self.name,
				}
			)

		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": self.company,
				"posting_date": self.get("available_for_use_date") or frappe.utils.nowdate(),
				"user_remark": _("Existing-Asset Opening for {0} (GAP-001)").format(self.name),
				"accounts": accounts,
			}
		)
		je.flags.ignore_permissions = True
		je.submit()

		# Values already carry net_purchase_amount and the opening accum —
		# the FT is the audit record + JE link, so deltas stay zero.
		tcc.apply(
			source_doc=("Asset", self.name),
			category="Addition",
			transaction_type="Existing-Asset Opening",
			asset=self.name,
			posting_date=je.posting_date,
			amount=gross,
			journal_entry=je.name,
		)

	def on_cancel(self):
		if self._enterprise():
			self._block_when_depreciation_posted()
			self._reverse_existing_asset_opening()
		super().on_cancel()
		if self._enterprise():
			# Core just overwrote ignore_linked_doctypes with its own
			# tuple — re-extend it so the mirror JE / FT / Activity rows
			# created above don't trip the post-cancel link check.
			self.ignore_linked_doctypes = tuple(
				set(tuple(self.get("ignore_linked_doctypes") or ()))
				| {"GL Entry", "Journal Entry", "Financial Treatment", "Asset Activity",
				   "Scrap Transaction"}
			)
			# VR-005 (Phase 11b): clear the PR row flag once no live
			# assets remain against it.
			row_name = self.get("purchase_receipt_item")
			if row_name and not frappe.db.exists(
				"Asset", {"purchase_receipt_item": row_name, "docstatus": ("<", 2)}
			):
				frappe.db.set_value(
					"Purchase Receipt Item", row_name, "asset_linked", 0, update_modified=False
				)

	def _reverse_existing_asset_opening(self):
		"""§12.2 reversal (Phase 11 F4): the GAP-001 opening JE is a
		standalone Journal Entry that core's asset-cancel reversal never
		touches — mirror it and pair the Addition FT."""
		if not self.get("is_existing_asset"):
			return
		ft = frappe.db.get_value(
			"Financial Treatment",
			{
				"asset": self.name,
				"transaction_type": "Existing-Asset Opening",
				"status": "Posted",
			},
			["name", "journal_entry"],
			as_dict=True,
		)
		if not ft or not ft.journal_entry:
			return

		from asset_enterprise import tcc
		from asset_enterprise.restore import _mirror_je

		self.ignore_linked_doctypes = tuple(
			set(tuple(self.get("ignore_linked_doctypes") or ()))
			| {"GL Entry", "Journal Entry", "Financial Treatment", "Asset Activity"}
		)
		mirror = _mirror_je(
			ft.journal_entry,
			_("Reversal of Existing-Asset Opening for {0} (asset reversal)").format(self.name),
		)
		tcc.reverse(ft.name, ("Asset", self.name), journal_entry=mirror)
		self.add_comment(
			"Comment",
			_("Existing-Asset Opening JE {0} reversed via {1}; original stays posted.").format(
				ft.journal_entry, mirror
			),
		)

	def _block_when_depreciation_posted(self):
		"""GAP-027 / VR-031: block reversal while LIVE depreciation
		exists — schedule-linked JEs AND manual depreciation JEs
		(Phase 11 F3). A JE already reversed per GA-0001-01 (a live JE
		points back via reversal_of) no longer counts (TC-042c)."""
		je_names = [
			r[0]
			for r in frappe.db.sql(
				"""
				select ds.journal_entry from `tabDepreciation Schedule` ds
				join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
				where ads.asset = %s and ads.docstatus = 1
				  and ifnull(ds.journal_entry, '') != ''
				""",
				self.name,
			)
		]
		je_names += [d.name for d in self.get_manual_depreciation_entries()]

		live = [je for je in set(je_names) if not self._je_is_reversed(je)]
		if live:
			frappe.throw(
				_(
					"Asset has {0} posted depreciation entries. Asset reversal under the "
					"immutable ledger requires reversal of these depreciation entries first. "
					"Reverse the linked depreciation JEs (via Reversal Journal Entry per "
					"GA-0001-01) before retrying asset reversal, OR contact accounts to "
					"handle the cleanup."
				).format(len(live))
			)

	@staticmethod
	def _je_is_reversed(je_name):
		"""True when a live GA-0001-01 Reversal JE points at this JE.
		Sites without the reversal_of field treat every JE as live."""
		if not frappe.get_meta("Journal Entry").has_field("reversal_of"):
			return False
		return bool(
			frappe.db.exists("Journal Entry", {"reversal_of": je_name, "docstatus": 1})
		)

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()
