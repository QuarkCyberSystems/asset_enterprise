import frappe
from frappe import _
from frappe.utils import flt, nowdate

from erpnext.assets.doctype.asset_value_adjustment.asset_value_adjustment import (
	AssetValueAdjustment,
)


class EnterpriseAVA(AssetValueAdjustment):
	"""GA-0005-01 v2.14 AVA overrides (GAP-013 / GAP-030 / VR-034).

	Forward: core posts the revaluation JE and reschedules (our
	supersession wrapper takes the reschedule when enabled); we record
	the impact through the TCC with signed deltas per transaction_type.
	No Downward Revaluation type exists — negative differences are
	Impairment (per client, IAS 16).

	Reverse Mode: cancelling a Standard AVA never touches the original
	JE. Instead a Reversal AVA is auto-created with SWAPPED
	current/new values, so core's own on_submit posts the mirror JE.
	The TCC pairs the FTs (original -> Reversed, mirror carries
	reversal_reference). Framework note: the source doc still moves to
	docstatus=2 on cancel (frappe semantics) — the LEDGER stays
	untouched, which is what immutability governs.

	A Reversal AVA itself cannot be cancelled (loop guard).
	"""

	def validate(self):
		# §3.5 / §12.14 / §12.15 (Phase 11b T8): default the difference
		# account from the account-resolution chain by transaction type;
		# the user can still override.
		if self._enterprise() and not self.get("difference_account"):
			field_by_type = {
				"Initial Impairment": "impairment_loss_account",
				"Upward Revaluation": "revaluation_surplus_oci_account",
				"Invoice Adjustment": "asset_invoice_difference_account",
			}
			field = field_by_type.get(self.get("transaction_type"))
			if field:
				try:
					from asset_enterprise.accounts import get_enterprise_account

					self.difference_account = get_enterprise_account(
						field,
						self.company,
						frappe.db.get_value("Asset", self.asset, "asset_category"),
					)
				except frappe.ValidationError:
					pass  # unconfigured — core's mandatory check guides the user
		super().validate()

	# ------------------------------------------------------------- submit
	def on_submit(self):
		# UL-only adjustments (difference = 0) post no revaluation JE —
		# core's make_asset_revaluation_entry crashes on a zero
		# difference (credit_entry unbound), and the reschedule is ours
		# via _apply_life_adjustment anyway (Phase 11 F2).
		ul_only = (
			self._enterprise()
			and not flt(self.difference_amount)
			and flt(self.get("adjusted_life_months") or 0)
		)
		if not ul_only:
			super().on_submit()
		if not self._enterprise():
			return

		from asset_enterprise import tcc

		if self.get("reversal_of_ava"):
			# Mirror leg of a reversal: pair the FTs instead of a new apply.
			original_ft = frappe.db.get_value(
				"Financial Treatment",
				{"source_doctype": "Asset Value Adjustment", "source_name": self.reversal_of_ava,
				 "status": "Posted"},
				"name",
			)
			if original_ft:
				tcc.reverse(
					original_ft,
					self,
					posting_date=self.date,
					journal_entry=self.get("journal_entry"),
				)
			frappe.db.set_value(
				"Asset Value Adjustment", self.reversal_of_ava,
				"reversed_by_ava", self.name, update_modified=False,
			)
			# A reversed life adjustment must swing the horizon back too.
			if flt(self.get("adjusted_life_months") or 0):
				self._apply_life_adjustment(flt(self.adjusted_life_months))
			return

		category, hav_delta, life_delta = self._classify()
		tcc.apply(
			source_doc=self,
			category=category,
			transaction_type=self.get("transaction_type") or category,
			asset=self.asset,
			posting_date=self.date,
			amount=abs(flt(self.difference_amount)) or abs(flt(self.get("adjusted_life_months") or 0)),
			hav_delta=hav_delta,
			life_delta_months=life_delta,
			journal_entry=self.get("journal_entry"),
		)

		# GAP-013 (Phase 11 F2): a life delta genuinely moves the
		# schedule horizon and the finance-book period count; RUL
		# exhausted -> immediate depreciation of the remaining base.
		if life_delta:
			self._apply_life_adjustment(life_delta)

	def _apply_life_adjustment(self, life_delta):
		from frappe.utils import add_months, cint, getdate

		from asset_enterprise.depreciation import (
			active_schedule_horizon,
			depreciate_remaining_base_now,
			supersede_and_regenerate,
		)

		horizon = active_schedule_horizon(self.asset)
		if not horizon:
			return  # no Active schedule — nothing to reshape
		new_end = add_months(getdate(horizon), cint(round(life_delta)))

		fb = frappe.db.get_value(
			"Asset Finance Book",
			{"parent": self.asset},
			["name", "total_number_of_depreciations", "frequency_of_depreciation"],
			as_dict=True,
		)
		if fb:
			periods_delta = cint(round(flt(life_delta) / flt(fb.frequency_of_depreciation or 1)))
			frappe.db.set_value(
				"Asset Finance Book", fb.name, "total_number_of_depreciations",
				max(0, cint(fb.total_number_of_depreciations) + periods_delta),
				update_modified=False,
			)

		if getdate(new_end) <= getdate(self.date):
			# VR-018: shortening below the adjustment date exhausts RUL —
			# post the full remaining base now, then freeze the schedule.
			depreciate_remaining_base_now(
				self.asset, self.date, self,
				transaction_type=_("Immediate Depreciation — RUL Exhausted via {0}").format(self.name),
			)
			supersede_and_regenerate(
				self.asset, as_of_date=self.date, end_of_life_override=self.date,
				reason=_("Useful life exhausted via {0}").format(self.name),
			)
		else:
			supersede_and_regenerate(
				self.asset, as_of_date=self.date, end_of_life_override=new_end,
				reason=_("Useful Life Adjustment via {0} ({1:+} months)").format(
					self.name, cint(round(life_delta))
				),
			)

	def _classify(self):
		"""Map transaction_type -> (category, hav_delta, life_delta)."""
		diff = flt(self.difference_amount)
		life = flt(self.get("adjusted_life_months") or 0)
		ttype = self.get("transaction_type")

		if ttype == "Useful Life Adjustment":
			return "Useful Life Adjustment", 0, life
		if ttype == "Initial Impairment":
			return "Impairment", diff, 0
		if ttype == "Upward Revaluation":
			return "Revaluation", diff, 0
		if ttype == "Invoice Adjustment":
			return "Addition", diff, 0
		if ttype == "Value + Life Adjustment":
			# §3.4 multi-treatment collapses into one FT carrying both deltas.
			category = "Revaluation" if diff > 0 else "Impairment"
			return category, diff, life
		# No explicit type: sign decides (no Downward Revaluation).
		return ("Revaluation" if diff >= 0 else "Impairment"), diff, life

	# ------------------------------------------------------------- cancel
	def on_cancel(self):
		if not self._enterprise():
			return super().on_cancel()

		if self.get("reversal_of_ava"):
			frappe.throw(
				_(
					"{0} is a Reversal AVA and cannot be cancelled. "
					"To undo it, submit a fresh Asset Value Adjustment."
				).format(self.name)
			)

		# VR-042: a reversal that removes value must be covered by NBV.
		value_added = flt(self.new_asset_value) - flt(self.current_asset_value)
		if value_added > 0:
			from asset_enterprise.asset_values import assert_nbv_covers_reversal

			assert_nbv_covers_reversal(self.asset, value_added, context=_("AVA {0}").format(self.name))

		# Never touch the original JE. Post the mirror via a Reversal AVA.
		# The reversal + FT/Activity rows deliberately link this doc —
		# exempt them from frappe's cancel-time link check.
		self.ignore_linked_doctypes = (
			"GL Entry",
			"Asset Value Adjustment",
			"Financial Treatment",
			"Asset Activity",
		)
		reversal = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment",
				"asset": self.asset,
				"company": self.company,
				"finance_book": self.get("finance_book"),
				"date": nowdate(),
				"transaction_type": self.get("transaction_type"),
				"current_asset_value": self.new_asset_value,
				"new_asset_value": self.current_asset_value,  # swap -> core posts mirror JE
				"adjusted_life_months": -flt(self.get("adjusted_life_months") or 0) or None,
				"difference_account": self.get("difference_account"),
				"cost_center": self.get("cost_center"),
				"reversal_of_ava": self.name,
			}
		)
		reversal.flags.ignore_permissions = True
		# Source is docstatus=2 mid-cancel; the back-link is the audit
		# trail we WANT — skip the cancelled-link validation.
		reversal.flags.ignore_links = True
		reversal.insert()
		reversal.submit()

		from asset_enterprise.tcc import add_snapshot_activity

		add_snapshot_activity(
			self.asset,
			_("AVA {0} reversed via Reversal AVA {1}; original JE remains posted.").format(
				self.name, reversal.name
			),
			transaction_type="Reversal",
		)

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()
