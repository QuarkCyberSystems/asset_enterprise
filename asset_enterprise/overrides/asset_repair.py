import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, flt, getdate, now, nowdate

from erpnext.assets.doctype.asset_repair.asset_repair import AssetRepair


class EnterpriseAssetRepair(AssetRepair):
	"""GA-0005-01 v2.14 Asset Repair overrides (GAP-033 / VR-038).

	Standard Repair (forward): core posts Dr Fixed Asset / Cr expense
	and reschedules (our supersession wrapper takes the reschedule when
	enabled); we additionally record the impact through the TCC.

	Reverse Mode: cancelling a capitalized Standard Repair never calls
	the destructive core path (make_gl_entries(cancel=True) +
	update_asset_value db_set). Instead a Reversal Asset Repair
	(same doctype, transaction_type=Reversal) is auto-created and
	submitted; its on_submit posts mirrored GL under its own reversal
	voucher, returns consumed stock via a Material
	Receipt Stock Entry (Sales-Return-style, per 2026-07-14 meeting),
	pairs the FTs through tcc.reverse, and supersedes the schedule.

	Gates: reversal prohibited on fully-depreciated assets (VR-038);
	a Reversal Repair itself cannot be cancelled (loop guard).
	"""

	# ------------------------------------------------------------- submit
	def on_submit(self):
		if self._enterprise() and self.get("transaction_type") == "Reversal":
			return self._submit_reversal()

		if self._enterprise():
			frappe.flags.ae_defer_reschedule = self.asset  # regenerate after the TCC
		try:
			super().on_submit()
		finally:
			frappe.flags.ae_defer_reschedule = None

		if self._enterprise() and self.get("capitalize_repair_cost"):
			from asset_enterprise import tcc

			tcc.apply(
				source_doc=self,
				category="Addition",
				transaction_type="Capitalized Repair",
				asset=self.asset,
				posting_date=self.completion_date or nowdate(),
				amount=flt(self.total_repair_cost),
				hav_delta=flt(self.total_repair_cost),
				life_delta_months=flt(self.get("increase_in_asset_life") or 0),
				# The repair posts its GL under its OWN voucher (core
				# make_gl_entries), not a Journal Entry. Link it so the
				# values fold's counted-voucher set recognises the GL and
				# the manual sweep never double-counts the capitalized
				# cost (client, 2026-08-25: HAV was net + FT delta + GL
				# sweep = 33,000 instead of 18,000).
				voucher_type="Asset Repair",
				voucher_no=self.name,
			)
			from asset_enterprise.depreciation import regenerate_after_value_change

			regenerate_after_value_change(
				self.asset, self.completion_date or nowdate(),
				_("Capitalized Repair {0} — prospective recalculation").format(self.name),
				end_of_life_override=self._extended_horizon(self),
				triggered_by=self,
			)

	def _extended_horizon(self, grant_doc, sign=1):
		"""Move the end of life by a repair's life extension.

		Core grants the extension by bumping `Asset Finance Book.
		increase_in_asset_life`, which only its OWN schedule builder
		reads — under supersession (GAP-031) that builder never runs, so
		the months were recorded and then ignored: the schedule kept its
		original horizon and the repair value simply re-spread over the
		unchanged remaining life. The horizon is moved here instead,
		with days on top per the AVA rule (client, 21/08).

		`sign=-1` retracts the grant. A Reversal Repair is a NEW document
		rather than a cancel, so core never decrements its counter and
		both counters are taken back here.
		"""
		from asset_enterprise.depreciation import (
			active_schedule_horizon,
			bump_useful_life_periods,
		)

		months = cint(grant_doc.get("increase_in_asset_life") or 0)
		days = cint(grant_doc.get("increase_in_asset_life_days") or 0)
		if not (months or days):
			return None
		horizon = active_schedule_horizon(self.asset)
		if not horizon:
			return None
		if sign < 0:
			fb = frappe.db.get_value(
				"Asset Finance Book", {"parent": self.asset},
				["name", "increase_in_asset_life", "life_extension_days"], as_dict=True,
			)
			if fb:
				frappe.db.set_value(
					"Asset Finance Book", fb.name,
					{
						"increase_in_asset_life": max(0, cint(fb.increase_in_asset_life) - months),
						"life_extension_days": cint(fb.life_extension_days) - days,
					},
					update_modified=False,
				)
		elif days:
			# Months are already on core's counter (which
			# schedule_horizon_from_life now reads); only days need ours.
			bump_useful_life_periods(self.asset, 0, days)
		if sign < 0:
			# Undo the day grant before the month grant (reverse composition).
			return add_months(add_days(getdate(horizon), -days), -months)
		return add_days(add_months(getdate(horizon), months), days)

	def validate(self):
		# GAP-036 / N1: capitalizing a repair posts value onto the asset
		# (DR Fixed Asset / CR Clearing) — a grouping container holds none.
		if (
			self.get("capitalize_repair_cost")
			and self.get("asset")
			and frappe.db.get_value("Asset", self.asset, "is_group_node")
		):
			frappe.throw(
				_(
					"{0} is a Grouping Asset and holds no value — a capitalized repair "
					"would post value onto a container. Capitalize the repair on the "
					"physical asset instead (GAP-036)."
				).format(self.asset),
				title=_("Grouping Asset Has No Value"),
			)
		if self._enterprise() and self.get("reversal_of_repair"):
			self._fully_depreciated_gate(frappe.get_doc("Asset Repair", self.reversal_of_repair))
		super().validate()
		# Same rule as the capitalization reversal (client, 25/08): a
		# Reversal Repair is created when a capitalized repair is
		# cancelled, which sets the read-only back-link before inserting.
		# Without one, this was chosen by hand.
		if (
			self._enterprise()
			and self.get("transaction_type") == "Reversal"
			and self.is_new()
			and not self.get("reversal_of_repair")
		):
			frappe.throw(
				_(
					"A Reversal Repair is raised automatically when a capitalized Asset "
					"Repair is cancelled — it cannot be created by hand. Open the repair "
					"you want to undo and cancel it."
				),
				title=_("Not a Manual Transaction Type"),
			)

	# ---------------------------------------------- consumed cost lines
	def before_submit(self):
		parent = getattr(super(), "before_submit", None)
		if parent:
			parent()
		self._allocate_consumed_lines()

	def _allocate_consumed_lines(self):
		"""D-054 (chief r4 B-1): the invoice cost lines this repair
		capitalizes, decided once and recorded (asset_enterprise.consumption)
		- an invoice row names the invoice and account only."""
		from asset_enterprise import consumption

		if not self.meta.has_field(consumption.TABLE):
			return
		if self.get("transaction_type") == "Reversal" or not self.get("capitalize_repair_cost"):
			self.set(consumption.TABLE, [])
			return
		dims = consumption.header_dimensions(self)
		consumption.allocate(
			self,
			[
				(row.name, row.purchase_invoice, row.expense_account, flt(row.repair_cost), dims, self.cost_center)
				for row in self.get("invoices") or []
				if row.purchase_invoice and flt(row.repair_cost)
			],
		)

	def get_gl_entries_for_repair_cost(self, gl_entries, fixed_asset_account):
		"""One capitalizing credit per consumed invoice line (its voucher row
		is the line's record), on the consumed line's dimensions; core's one
		credit per invoice row, merged by account, named no line. The asset
		debit is core's."""
		from asset_enterprise import consumption

		lines = self.get(consumption.TABLE) or []
		if not lines or flt(self.repair_cost) <= 0:
			return super().get_gl_entries_for_repair_cost(gl_entries, fixed_asset_account)
		for line in lines:
			gl_entries.append(
				self.get_gl_dict(
					{
						"account": line.expense_account,
						"credit": flt(line.amount),
						"credit_in_account_currency": flt(line.amount),
						"against": fixed_asset_account,
						"voucher_type": self.doctype,
						"voucher_no": self.name,
						"voucher_detail_no": line.name,
						"cost_center": self.cost_center,
						"posting_date": self.completion_date,
						"company": self.company,
						**consumption.line_dimensions(line),
					},
					item=self,
				)
			)
			if line.gl_voucher_no != self.name:
				consumption.mark_voucher(line, self.doctype, self.name, line.name)
		gl_entries.append(
			self.get_gl_dict(
				{
					"account": fixed_asset_account,
					"debit": self.repair_cost,
					"debit_in_account_currency": self.repair_cost,
					"against": ", ".join({line.expense_account for line in lines}),
					"voucher_type": self.doctype,
					"voucher_no": self.name,
					"cost_center": self.cost_center,
					"posting_date": self.completion_date,
					"against_voucher_type": "Asset",
					"against_voucher": self.asset,
					"company": self.company,
				},
				item=self,
			)
		)

	def _submit_reversal(self):
		"""Reversal Repair on_submit — replaces the core forward path."""
		source_name = self.get("reversal_of_repair")
		if not source_name:
			frappe.throw(_("Reversal Repair requires Reversal Of Repair."))
		source = frappe.get_doc("Asset Repair", source_name)

		self._fully_depreciated_gate(source)

		# Repair posts under its own voucher, not a Journal Entry. Preserve
		# the original rows and post their mirror under THIS reversal voucher.
		from erpnext.accounts.general_ledger import make_gl_entries

		reversal_date = getdate(self.completion_date or nowdate())
		# the dimension list is read once per reversal, not once per row
		kept = _mirror_kept_fields()
		original_gl = frappe.get_all("GL Entry",
			filters={"voucher_type": "Asset Repair", "voucher_no": source.name, "is_cancelled": 0},
			fields=_mirror_source_fields(kept), order_by="creation, name")
		gl_map = [
			_mirror_gl_row(original, self.name, reversal_date,
				_("Reversal Repair {0} of {1}").format(self.name, source.name), kept)
			for original in original_gl
		]
		make_gl_entries(gl_map, merge_entries=False)

		# 2. Stock return — Material Receipt of the consumed items
		#    (Sales-Return pattern; valuation per Item method, GA-0003).
		se_name = None
		if source.get("stock_items"):
			se = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Receipt",
					"company": self.company or source.company,
					"posting_date": reversal_date,
					"set_posting_time": 1,
					"items": [
						{
							"item_code": row.item_code,
							"qty": flt(row.consumed_quantity),
							"t_warehouse": row.warehouse,
							"basic_rate": flt(row.valuation_rate),
							"allow_zero_valuation_rate": 0,
						}
						for row in source.stock_items
						if flt(row.consumed_quantity)
					],
				}
			)
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
			se_name = se.name
			self.add_comment(
				"Comment", _("Consumed stock returned via Stock Entry {0}.").format(se_name)
			)

		# 3. Pair the Financial Treatments.
		from asset_enterprise import tcc

		original_ft = frappe.db.get_value(
			"Financial Treatment",
			{"source_doctype": "Asset Repair", "source_name": source.name, "status": "Posted"},
			"name",
		)
		if original_ft:
			tcc.reverse(
				original_ft, self,
				# C3: the reversal's own completion date (chosen in the
				# dialog, default today) anchors the FT pairing.
				posting_date=getdate(self.completion_date or nowdate()),
				# The Reversal Repair's GL is posted under ITS voucher;
				# link it so the mirror FT
				# points at the right voucher and the fold's counted set
				# covers the reversal GL too.
				voucher_type="Asset Repair",
				voucher_no=self.name,
			)

		# 4. Prospective schedule from the reduced base — resuming from
		# the last POSTED row, with today (the reversal) as the
		# rate-change boundary (19/08 caller audit).
		from asset_enterprise.depreciation import (
			last_posted_schedule_date,
			supersede_and_regenerate,
		)

		last_posted = last_posted_schedule_date(self.asset)
		reversal_date = getdate(self.completion_date or nowdate())
		try:
			supersede_and_regenerate(
				self.asset,
				as_of_date=getdate(last_posted) if last_posted else None,
				rate_change_date=reversal_date if last_posted else None,
				# The life the source repair granted goes back with the
				# value — otherwise the cost was reversed but the asset
				# kept the extra months and days (client, 21/08).
				end_of_life_override=self._extended_horizon(source, sign=-1),
				reason=_("Reversal Repair {0} of {1}").format(self.name, source.name),
				triggered_by=self,
			)
		except frappe.ValidationError:
			pass  # asset without an Active schedule (no depreciation) — nothing to supersede

		source.db_set("reversed_by_repair", self.name, update_modified=False)

		from asset_enterprise.tcc import add_snapshot_activity

		add_snapshot_activity(
			self.asset,
			_("Repair {0} reversed via Reversal Repair {1}; original ledger entries remain posted.").format(
				source.name, self.name
			),
			transaction_type="Reversal",
		)

	# ------------------------------------------------------------- cancel
	def on_cancel(self):
		if not self._enterprise():
			return super().on_cancel()

		if self.get("transaction_type") == "Reversal":
			frappe.throw(
				_(
					"{0} is a Reversal Repair and cannot be cancelled. "
					"To undo it, submit a fresh Standard Repair."
				).format(self.name)
			)

		if not self.get("capitalize_repair_cost"):
			return super().on_cancel()

		self._fully_depreciated_gate(self)

		# Replace the destructive core path with a Reversal Repair. The
		# reversal + FT/Activity rows deliberately link this doc.
		self.ignore_linked_doctypes = (
			"Asset Depreciation Schedule",  # triggered_by dynamic link
			"GL Entry",
			"Stock Ledger Entry",
			"Stock Entry",  # core's consumption SE + our return SE link this repair
			"Asset Repair",
			"Financial Treatment",
			"Asset Activity",
			"Journal Entry",
		)
		self.cancel_sabb()

		reversal = frappe.get_doc(
			{
				"doctype": "Asset Repair",
				"asset": self.asset,
				"company": self.company,
				"failure_date": self.failure_date,
				"completion_date": getdate(
					frappe.flags.get("ae_repair_reversal_date") or nowdate()
				),
				"repair_status": "Completed",
				"repair_cost": 0,
				"capitalize_repair_cost": 0,  # financials flow via _submit_reversal
				"cost_center": self.get("cost_center"),
				"project": self.get("project"),
				"transaction_type": "Reversal",
				"reversal_of_repair": self.name,
				"description": _("Reversal of {0}").format(self.name),
			}
		)
		reversal.flags.ignore_permissions = True
		# Source is docstatus=2 mid-cancel; the back-link is the audit trail.
		reversal.flags.ignore_links = True
		reversal.insert()
		reversal.submit()

	def _fully_depreciated_gate(self, repair_source):
		"""VR-038: block reversal when the asset is fully depreciated.
		Generalized by VR-042 (2026-07-23 review): the reversal must be
		covered by current NBV — full depreciation is the limiting case."""
		if is_fully_depreciated(repair_source.asset):
			frappe.throw(
				_(
					"Asset {0} is fully depreciated. Repair reversal is not permitted; "
					"handle any correction via Asset Value Adjustment."
				).format(repair_source.asset)
			)
		if repair_source.get("capitalize_repair_cost"):
			from asset_enterprise.asset_values import assert_nbv_covers_reversal

			assert_nbv_covers_reversal(
				repair_source.asset,
				flt(repair_source.total_repair_cost),
				context=_("Repair {0}").format(repair_source.name),
			)

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()


def is_fully_depreciated(asset_name):
	"""NBV at salvage AND no unposted schedule rows left."""
	from asset_enterprise.asset_values import recalculate_asset_values

	values = recalculate_asset_values(asset_name, save=False)
	# v16: salvage lives on the finance book (expected_value_after_useful_life).
	salvage = flt(
		frappe.db.get_value(
			"Asset Finance Book", {"parent": asset_name}, "expected_value_after_useful_life"
		)
		or 0
	)
	unposted = frappe.db.sql(
		"""
		select count(*) from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') = ''
		""",
		asset_name,
	)[0][0]
	has_schedule = frappe.db.exists(
		"Asset Depreciation Schedule", {"asset": asset_name, "status": "Active", "docstatus": 1}
	)
	return bool(has_schedule) and unposted == 0 and flt(values["net_book_value"]) <= salvage


# A mirror row keeps who and what the original booked against; everything
# a GL row derives from its own date (fiscal year, reporting-currency rate
# and amounts, due and transaction dates) or from its insert (name, audit
# stamps, app-stamped keys) is left blank so core and the stamping hooks
# derive it again for the reversal's posting date. Copying the whole row
# carried the original fiscal year across a year end (GLEntry fills a
# blank fiscal year only).
_MIRROR_KEEP = (
	"company",
	"account",
	"account_currency",
	"party_type",
	"party",
	"cost_center",
	"project",
	"finance_book",
	"against",
	"against_voucher_type",
	"against_voucher",
	"voucher_type",
	"voucher_subtype",
	"voucher_detail_no",
	"is_opening",
	"is_advance",
	"transaction_currency",
	"transaction_exchange_rate",
)

_MIRROR_SWAP = (
	("debit", "credit"),
	("debit_in_account_currency", "credit_in_account_currency"),
	("debit_in_transaction_currency", "credit_in_transaction_currency"),
)


def _mirror_kept_fields():
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		get_accounting_dimensions,
	)

	meta = frappe.get_meta("GL Entry")
	return list(_MIRROR_KEEP) + [
		fieldname for fieldname in (get_accounting_dimensions() or []) if meta.has_field(fieldname)
	]


def _mirror_source_fields(kept=None):
	"""Every column the mirror reads from the original row — one query."""
	return list(kept or _mirror_kept_fields()) + [field for pair in _MIRROR_SWAP for field in pair]


def _mirror_gl_row(original, voucher_no, posting_date, remarks, kept=None):
	"""The reversing GL map row for `original` under `voucher_no`; `kept`
	is `_mirror_kept_fields()`, computed once by the caller."""
	row = frappe._dict({field: original.get(field) for field in (kept or _mirror_kept_fields())})
	for debit, credit in _MIRROR_SWAP:
		row[debit], row[credit] = original.get(credit), original.get(debit)
	row.update(voucher_no=voucher_no, posting_date=posting_date, is_cancelled=0, remarks=remarks)
	return row
