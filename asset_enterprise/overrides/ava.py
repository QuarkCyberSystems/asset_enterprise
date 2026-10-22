import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from asset_enterprise.reversal import REVERSAL_LINKS, CounterDocumentReversal
from erpnext.assets.doctype.asset_value_adjustment.asset_value_adjustment import (
	AssetValueAdjustment,
)


class EnterpriseAVA(CounterDocumentReversal, AssetValueAdjustment):
	"""GA-0005-01 v2.14 AVA overrides (GAP-013 / GAP-030 / VR-034).

	Forward: core posts the revaluation JE and reschedules (our
	supersession wrapper takes the reschedule when enabled); we record
	the impact through the TCC with signed deltas per transaction_type.
	No Downward Revaluation type exists — negative differences are
	Impairment (per client, IAS 16).

	Reverse Mode (GAP-030, reversal.CounterDocumentReversal): an AVA is
	never cancelled. reverse() raises a Reversal AVA with SWAPPED
	current/new values, so core's own on_submit posts the mirror JE, and
	the TCC pairs the FTs (original -> Reversed, mirror carries
	reversal_reference). The original stays submitted, marked Reversed;
	its JE stays posted.
	"""

	reversal_link_field = REVERSAL_LINKS["Asset Value Adjustment"]
	reversal_of_field = "reversal_of_ava"
	reversal_label = "Asset Value Adjustment"

	def validate(self):
		self._enforce_type_contract()
		# GAP-036 / N1: a grouping asset holds no value — an AVA against
		# it would post value (Revaluation) or loss (Impairment) onto a
		# container. The value belongs to the physical assets underneath.
		if self.get("asset") and frappe.db.get_value("Asset", self.asset, "is_group_node"):
			frappe.throw(
				_(
					"{0} is a Grouping Asset and holds no value — adjust the physical "
					"assets grouped under it instead (GAP-036)."
				).format(self.asset),
				title=_("Grouping Asset Has No Value"),
			)
		# §3.5 / §12.14 / §12.15 (Phase 11b T8): default the difference
		# account from the account-resolution chain by transaction type;
		# the user can still override.
		if self._enterprise():
			from asset_enterprise import allocation_handover

			# GAP-030 (GA-0002-02): the account, cost centre and amount of a
			# Cost Allocation Handover are the positions it takes
			allocation_handover.validate(self)
			self._refuse_manual_invoice_adjustment()
			self._refuse_manual_project_settlement()
			self._refuse_forbidden_without_depreciation()
			# Same rule as everywhere else: the entry belongs to the centre
			# that held the asset on the adjustment date. Core otherwise
			# falls back to the company's depreciation cost centre
			# (`self.cost_center or depreciation_cost_center`), so an
			# impairment of an asset sitting in Head Office was booked to
			# the company default. Only filled when the user has not
			# chosen one — a deliberate choice still stands.
			if not self.get("cost_center") and self.get("asset"):
				from asset_enterprise.depreciation import cost_centre_on

				self.cost_center = cost_centre_on(
					self.asset, self.get("date") or nowdate()
				) or frappe.db.get_value("Asset", self.asset, "cost_center")
			# Client, 24/08: for impairment and revaluation the account is
			# NOT the user's to pick — it comes from the Asset Category
			# (§3.5 chain) and the field is read-only. Resolved here even
			# when something was typed in, so the form and the ledger can
			# never disagree. Invoice Adjustment keeps fill-if-empty: the
			# invoice flow supplies its own clearing account (D1). A
			# Project Settlement is never filled here: the run always
			# supplies the account it settles from (its offset account, or
			# the source's own account in Original Account mode).
			forced = {
				"Initial Impairment": "impairment_loss_account",
				"Upward Revaluation": "revaluation_surplus_oci_account",
			}
			field = forced.get(self.get("transaction_type"))
			if not field and not self.get("difference_account"):
				field = {"Invoice Adjustment": "asset_invoice_difference_account"}.get(
					self.get("transaction_type")
				)
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
		if self.get("transaction_type") == "Useful Life Adjustment":
			# Re-assert after core's validate: set_difference_amount runs
			# in super() and would recompute a difference from whatever
			# core auto-filled into current_asset_value.
			self.new_asset_value = self.current_asset_value
			self.difference_amount = 0

	def _refuse_forbidden_without_depreciation(self):
		"""CH-47 / CH-53: a manual adjustment of an asset that is not
		depreciating is allowed unless its category forbids it. A reversal
		gives back what was posted, and Invoice Adjustment / Project
		Settlement are raised by their own flows - none of them is refused."""
		if (
			not self.get("asset")
			or self.get("reversal_of_ava")
			or self.get("transaction_type") in SYSTEM_RAISED_TYPES
		):
			return
		reason = value_adjustment_refusal(self.asset)
		if reason:
			frappe.throw(reason, title=_("Value Adjustment Forbidden"))
		if self.get("transaction_type") in LIFE_TYPES and not frappe.db.get_value(
			"Asset", self.asset, "calculate_depreciation"
		):
			# no useful life runs before depreciation is set up, so a life
			# adjustment would post and change nothing
			frappe.throw(
				_(
					"{0} is not depreciating, so it has no useful life to adjust. Set the life when "
					"depreciation is enabled; to change only its value, use Upward Revaluation or "
					"Initial Impairment."
				).format(self.asset),
				title=_("No Useful Life Yet"),
			)

	def _refuse_manual_invoice_adjustment(self):
		"""Client, 24/08: "Invoice adjustment in select should not be
		selected manual by user".

		An Invoice Adjustment is raised BY the Purchase Invoice flow
		(§12.4/§12.5) with the delta and clearing account it computed;
		one typed by hand would book an invoice difference no invoice
		asked for. Its own reversal carries the type across and is
		allowed."""
		if (
			self.get("transaction_type") != "Invoice Adjustment"
			or not self.is_new()
			or self.get("reversal_of_ava")
			or frappe.flags.get("ae_invoice_adjustment")
		):
			return
		frappe.throw(
			_(
				"Invoice Adjustment is raised automatically by the Purchase Invoice "
				"that carries the difference — it cannot be created by hand. To change "
				"an asset's value directly, use Initial Impairment, Upward Revaluation "
				"or Value + Life Adjustment."
			),
			title=_("Not a Manual Transaction Type"),
		)

	def _refuse_manual_project_settlement(self):
		"""PA-008 (client, 02/10, option A): an addition to an existing
		asset capitalized by a Project Settlement Run posts as an AVA of
		type Project Settlement, raised by the run with the amount, account
		and project dimensions it settles. One typed by hand would
		capitalize cost no settlement released. Its own reversal carries
		the type across and is allowed."""
		if (
			self.get("transaction_type") != "Project Settlement"
			or not self.is_new()
			or self.get("reversal_of_ava")
			or frappe.flags.get("ae_project_settlement")
		):
			return
		frappe.throw(
			_(
				"Project Settlement is raised by the Project Settlement Run that "
				"capitalizes the cost — it cannot be created by hand. To change an "
				"asset's value directly, use Initial Impairment, Upward Revaluation "
				"or Value + Life Adjustment."
			),
			title=_("Not a Manual Transaction Type"),
		)

	def _enforce_type_contract(self):
		"""§3.2/§3.4: each transaction type has ONE meaning — the type
		governs the fields, not the other way round (client, 19/08:
		ACC-JV-2026-00661 — a Useful Life Adjustment carrying a value
		difference made core post a write-down on top of the GAP-013
		exhaustion depreciation; 499.70 hit the P&L twice).

		- Useful Life Adjustment changes TIME only. Value fields are
		  neutralised; an explicit conflicting pair is refused. When the
		  shortened life is exhausted, the engine posts the remainder as
		  DR Depreciation / CR Accumulated Depreciation (GAP-013).
		- Value-only types refuse life fields.
		- Both at once = "Value + Life Adjustment" (§3.4).
		"""
		if not self._enterprise():
			return
		ttype = self.get("transaction_type")
		months = flt(self.get("adjusted_life_months") or 0)
		days = flt(self.get("adjusted_life_days") or 0)

		if ttype == "Useful Life Adjustment":
			cur, new = flt(self.current_asset_value), flt(self.new_asset_value)
			if cur and new and abs(cur - new) > 0.005:
				frappe.throw(
					_(
						"A Useful Life Adjustment changes time, not value — Current and New "
						"Asset Value must stay equal. When the shortened life is already "
						"exhausted, the remaining base posts automatically as depreciation. "
						"To change value and life together, use 'Value + Life Adjustment'."
					)
				)
			# untouched / hidden fields: mirror so core's difference is
			# zero (flt-coerced — core subtracts these before we run again)
			self.new_asset_value = flt(self.current_asset_value)
			self.current_asset_value = flt(self.current_asset_value)
			self.difference_amount = 0
		elif ttype in ("Initial Impairment", "Upward Revaluation", "Invoice Adjustment",
				"Project Settlement", "Cost Allocation Handover") and (
			months or days
		):
			frappe.throw(
				_(
					"{0} changes value, not useful life — clear the Adjusted Life fields. "
					"To change value and life together, use 'Value + Life Adjustment'."
				).format(_(ttype))
			)

	def update_accounting_dimensions(self, credit_entry, debit_entry):
		"""Core copies a dimension onto the revaluation JE only when the
		company marks it mandatory for P&L (debit leg) or balance sheet
		(credit leg), so a dimension set on the AVA is otherwise dropped
		(PA-008: a Project Settlement carries project / WBS / cost
		component / tracking item). Every dimension the AVA carries goes
		on both legs; core's own defaults still fill the rest."""
		super().update_accounting_dimensions(credit_entry, debit_entry)
		if not self._enterprise():
			return
		from asset_enterprise.gl_attribution import dimension_fields

		for fieldname in dimension_fields(self.doctype):
			value = self.get(fieldname)
			if value:
				credit_entry[fieldname] = debit_entry[fieldname] = value

	def make_asset_revaluation_entry(self):
		if not (self._enterprise() and self.get("reversal_of_ava")):
			return super().make_asset_revaluation_entry()
		from asset_enterprise.restore import _mirror_je

		original = frappe.db.get_value("Asset Value Adjustment", self.reversal_of_ava, "journal_entry")
		if original:
			mirror = _mirror_je(original,
				_("Reversal AVA {0} of {1}").format(self.name, self.reversal_of_ava),
				posting_date=self.date)
			self.db_set("journal_entry", mirror)

	# ------------------------------------------------------------- submit
	def before_submit(self):
		if not self._enterprise():
			return
		from asset_enterprise import allocation_handover

		# take the positions (and admit this document's entry), or give a
		# reversed handover's positions back before its mirror posts
		allocation_handover.consume(self)
		allocation_handover.release_for_reversal(self)

	def on_submit(self):
		# VR-043 (client, 10/09): an impairment or revaluation is measured
		# FROM the carrying amount, so periods left unposted make it a
		# measurement against a value the asset stopped having months ago.
		# Unlike a full scrap there is no proration step to make it
		# current, so the periods have to be booked first.
		#
		# A REVERSAL is exempt: its amount is fixed by the document it
		# mirrors, not derived from today's carrying amount, so stale
		# periods cannot misstate it — and blocking one would trap a user
		# who needs to undo an entry before posting anything further
		# (TC-044b).
		if (
			self._enterprise()
			and not self.get("reversal_of_ava")
			and frappe.db.get_value("Asset", self.asset, "calculate_depreciation")
		):
			from asset_enterprise.depreciation import assert_depreciation_current

			assert_depreciation_current(self.asset, getdate(self.date), _("this adjustment"))
		# UL-only adjustments (difference = 0) post no revaluation JE —
		# core's make_asset_revaluation_entry crashes on a zero
		# difference (credit_entry unbound), and the reschedule is ours
		# via _apply_life_adjustment anyway (Phase 11 F2).
		ul_only = (
			self._enterprise()
			and not flt(self.difference_amount)
			and (
				flt(self.get("adjusted_life_months") or 0)
				or flt(self.get("adjusted_life_days") or 0)
			)
		)
		if self._enterprise():
			# core reschedules inside on_submit, BEFORE our TCC records the
			# value change — defer it and regenerate below (§4.3).
			frappe.flags.ae_defer_reschedule = self.asset
		try:
			if not ul_only:
				super().on_submit()
		finally:
			frappe.flags.ae_defer_reschedule = None
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
			if flt(self.get("adjusted_life_months") or 0) or flt(self.get("adjusted_life_days") or 0):
				self._apply_life_adjustment(
					flt(self.get("adjusted_life_months") or 0),
					flt(self.get("adjusted_life_days") or 0),
				)
			elif flt(self.difference_amount):
				from asset_enterprise.depreciation import regenerate_after_value_change

				regenerate_after_value_change(
					self.asset, self.date,
					_("Reversal AVA {0} — prospective recalculation").format(self.name),
					triggered_by=self,
				)
			return

		category, hav_delta, life_delta = self._classify()
		tcc.apply(
			source_doc=self,
			category=category,
			transaction_type=self.get("transaction_type") or category,
			asset=self.asset,
			posting_date=self.date,
			amount=abs(flt(self.difference_amount))
			or abs(flt(self.get("adjusted_life_months") or 0))
			or abs(flt(self.get("adjusted_life_days") or 0)),
			hav_delta=hav_delta,
			life_delta_months=life_delta,
			journal_entry=self.get("journal_entry"),
		)

		# GAP-013 (Phase 11 F2): a life delta genuinely moves the
		# schedule horizon and the finance-book period count; RUL
		# exhausted -> immediate depreciation of the remaining base.
		# Days combine with months (client, 18/08): +3 months +15 days.
		day_delta = flt(self.get("adjusted_life_days") or 0)
		if life_delta or day_delta:
			self._apply_life_adjustment(life_delta, day_delta)
		elif hav_delta:
			# §4.3 prospective recalc — runs HERE (not inside core's
			# on_submit) so the new NBV is already recorded.
			from asset_enterprise.depreciation import regenerate_after_value_change

			regenerate_after_value_change(
				self.asset, self.date,
				_("{0} via {1} — prospective recalculation").format(
					self.get("transaction_type") or "Value adjustment", self.name
				),
				triggered_by=self,
			)

	def _apply_life_adjustment(self, life_delta, day_delta=0):
		from frappe.utils import add_days, add_months, cint, getdate

		from asset_enterprise.depreciation import (
			active_schedule_horizon,
			depreciate_remaining_base_now,
			supersede_and_regenerate,
		)

		horizon = active_schedule_horizon(self.asset)
		if not horizon:
			return  # no Active schedule — nothing to reshape
		# Months move the horizon calendar-wise; days shift it on top —
		# the daily-rate engine handles any end date, the final stub row
		# simply covers the extra days (client, 18/08).
		new_end = add_days(
			add_months(getdate(horizon), cint(round(life_delta))), cint(round(day_delta))
		)

		from asset_enterprise.depreciation import bump_useful_life_periods

		# Days go to the finance book's day counter, not the period count,
		# so a later rebuild-from-life keeps them (client, 21/08).
		bump_useful_life_periods(self.asset, life_delta, day_delta)

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
				triggered_by=self,
			)
		else:
			# Resume from the last POSTED row with the adjustment date as
			# the rate-change boundary — regenerating from the adjustment
			# date itself left the days since the last posting with no
			# row of their own (a 13-day August, the same short-row shape
			# as client sheet item 4).
			from asset_enterprise.depreciation import last_posted_schedule_date

			last_posted = last_posted_schedule_date(self.asset)
			supersede_and_regenerate(
				self.asset,
				as_of_date=getdate(last_posted) if last_posted else None,
				end_of_life_override=new_end,
				rate_change_date=getdate(self.date) if last_posted else None,
				reason=_("Useful Life Adjustment via {0} ({1:+} months, {2:+} days)").format(
					self.name, cint(round(life_delta)), cint(round(day_delta))
				),
				triggered_by=self,
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
		if ttype in ("Invoice Adjustment", "Project Settlement", "Cost Allocation Handover"):
			return "Addition", diff, 0
		if ttype == "Value + Life Adjustment":
			# §3.4 multi-treatment collapses into one FT carrying both deltas.
			category = "Revaluation" if diff > 0 else "Impairment"
			return category, diff, life
		# No explicit type: sign decides (no Downward Revaluation).
		return ("Revaluation" if diff >= 0 else "Impairment"), diff, life

	# ------------------------------------------------------------- cancel
	# --------------------------------------------------------- reversal
	def reversal_eligible(self):
		if not self._enterprise():
			return _("Enterprise asset features are off: this adjustment is cancelled, not reversed.")
		return None

	def reversal_note(self):
		if not self.get("journal_entry"):
			return _("Depreciation recalculates from that date.")
		return _("The mirror of journal entry {0} posts on that date and depreciation recalculates from it.").format(
			self.journal_entry
		)

	def reversal_gates(self):
		# VR-042: a reversal that removes value must be covered by NBV.
		value_added = flt(self.new_asset_value) - flt(self.current_asset_value)
		if value_added > 0:
			from asset_enterprise.asset_values import assert_nbv_covers_reversal

			assert_nbv_covers_reversal(self.asset, value_added, context=_("AVA {0}").format(self.name))

	def build_reversal(self, posting_date):
		reversal = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment",
				"asset": self.asset,
				"company": self.company,
				"finance_book": self.get("finance_book"),
				"date": getdate(posting_date),
				"transaction_type": self.get("transaction_type"),
				"current_asset_value": self.new_asset_value,
				"new_asset_value": self.current_asset_value,  # swap -> core posts mirror JE
				"adjusted_life_months": -flt(self.get("adjusted_life_months") or 0) or None,
				"adjusted_life_days": -flt(self.get("adjusted_life_days") or 0) or None,
				"difference_account": self.get("difference_account"),
				"cost_center": self.get("cost_center"),
				"reversal_of_ava": self.name,
			}
		)
		reversal.flags.ignore_links = True
		return reversal

	# Cancel is refused by the module's platform adapter, with Reverse as
	# the route (platform.AssetLedgerAdapter.can_cancel) — a document that
	# is not reversed this way (reversal_eligible) cancels natively.

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()


# AVA types raised by another flow, never typed by hand (the two refusals
# above); a category's value-adjustment rule does not apply to them
SYSTEM_RAISED_TYPES = ("Invoice Adjustment", "Project Settlement")
# the types that change the useful life (§3.4)
LIFE_TYPES = ("Useful Life Adjustment", "Value + Life Adjustment")


def value_adjustment_refusal(asset):
	"""Why a manual Asset Value Adjustment may not adjust `asset` (CH-53),
	or None. A depreciating asset is always adjustable; one that is not
	(Pending Depreciation Setup, Non-Depreciable - CH-47) is, unless its
	category is flagged Forbid Value Adjustment Without Depreciation."""
	row = frappe.db.get_value("Asset", asset, ["calculate_depreciation", "asset_category"], as_dict=True)
	if not row or row.calculate_depreciation or not row.asset_category:
		return None
	if not frappe.db.get_value("Asset Category", row.asset_category, "forbid_value_adjustment_without_depreciation"):
		return None
	return _(
		"{0} is not depreciating, and its category {1} forbids a value adjustment until "
		"depreciation is set up (Asset Category > Forbid Value Adjustment Without Depreciation)."
	).format(asset, row.asset_category)


@frappe.whitelist()
def value_of_accounting_dimensions(asset_name):
	"""Override of core's get_value_of_accounting_dimensions, which the
	Asset Value Adjustment form calls when an asset is picked and whose
	answer frm.call writes straight back onto the form. Core reads every
	dimension off the Asset record; this app's Asset dimension is named
	`asset` - on the adjustment that field IS the asset - and an Asset
	record's own asset dimension is empty, so core set the picked asset
	back to empty on every pick (client 21/10/2026, ACC-ASS-2026-00369).
	A dimension whose document type is Asset answers the asset itself."""
	from erpnext.assets.doctype.asset_value_adjustment.asset_value_adjustment import (
		get_value_of_accounting_dimensions,
	)

	values = get_value_of_accounting_dimensions(asset_name) or {}
	for fieldname in frappe.get_all("Accounting Dimension", filters={"document_type": "Asset"}, pluck="fieldname"):
		values[fieldname] = asset_name
	return values
