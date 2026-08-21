import frappe
from frappe import _
from frappe.utils import date_diff, flt, get_first_day, getdate

from erpnext.assets.doctype.asset_movement.asset_movement import AssetMovement


class EnterpriseAssetMovement(AssetMovement):
	"""GA-0005-01 v2.14 Asset Movement overrides (GAP-020/021/022/028).

	GAP-022 (VR-026): one movement may update any subset of
	{target_location, to_employee, target_cost_center} — at least one.
	Core's per-purpose mandates are relaxed under the master switch:
	location rules apply only when a target_location is given, employee
	rules only when to_employee is given.

	GAP-020: `target_cost_center` reroutes future depreciation; the
	prior CC is kept on the row (`source_cost_center`) for reversal.

	GAP-021: on a mid-period transfer, the covering unposted schedule
	row is split into two CC-tagged rows (day-prorated; the pair sums
	exactly to the original amount).

	GAP-028: reversal restores the prior cost center. No GL either way
	(movements are ledger-neutral).
	"""

	def validate_movement(self, d):
		if not self._enterprise():
			return super().validate_movement(d)

		# The source side is a FACT read from the asset, not user input
		# (client, 20/08): location, custodian and cost centre are filled
		# server-side and shown read-only — the user only picks targets.
		state = frappe.db.get_value(
			"Asset", d.asset, ["location", "custodian", "cost_center"], as_dict=True
		)
		if state:
			if self.purpose != "Receipt" and state.location:
				d.source_location = state.location
			if state.custodian:
				d.from_employee = state.custodian
			if d.get("target_cost_center") and not d.get("source_cost_center"):
				d.source_cost_center = state.cost_center

		if not (d.get("target_location") or d.get("to_employee") or d.get("target_cost_center")):
			frappe.throw(
				_(
					"Row {0}: set at least one of Target Location, To Employee or "
					"Target Cost Center (VR-026)."
				).format(d.idx)
			)

		# VR-010 (Phase 11b): a parent asset with children cannot move as
		# a group — transfer the children individually first.
		if self.purpose in ("Transfer", "Transfer and Issue") and frappe.db.exists(
			"Asset", {"parent_asset": d.asset, "docstatus": 1}
		):
			frappe.throw(
				_(
					"Asset {0} has child assets in its tree — group transfer is not "
					"allowed (VR-010). Transfer the children individually first."
				).format(d.asset)
			)

		# VR-025 (Phase 11b): target cost center must belong to the
		# movement's company and be a leaf node.
		if d.get("target_cost_center"):
			cc = frappe.db.get_value(
				"Cost Center", d.target_cost_center, ["company", "is_group"], as_dict=True
			)
			if not cc or cc.company != self.company:
				frappe.throw(
					_("Row {0}: Cost Center {1} does not belong to company {2} (VR-025).").format(
						d.idx, d.target_cost_center, self.company
					)
				)
			if cc.is_group:
				frappe.throw(
					_("Row {0}: Cost Center {1} is a group — pick a leaf cost center (VR-025).").format(
						d.idx, d.target_cost_center
					)
				)

		if d.get("target_location"):
			current_location = frappe.db.get_value("Asset", d.asset, "location")
			if d.get("source_location") and current_location != d.source_location:
				frappe.throw(
					_("Asset {0} does not belong to the location {1}").format(
						d.asset, d.source_location
					)
				)
			if self.purpose in ("Transfer", "Transfer and Issue"):
				# Only a genuine transfer requires distinct locations —
				# the auto-created Receipt on Asset submit lands the asset
				# at its own location by design.
				d.source_location = d.get("source_location") or current_location
				if d.source_location == d.target_location:
					frappe.throw(_("Source and Target Location cannot be same"))

		if d.get("to_employee") and frappe.db.get_value(
			"Employee", d.to_employee, "company"
		) != self.company:
			frappe.throw(
				_("Employee {0} does not belong to the company {1}").format(
					d.to_employee, self.company
				)
			)

	def get_latest_location_and_custodian(self, asset):
		"""Core orders the movement history by transaction_date alone and
		takes the first row, so two movements on the SAME date resolve
		arbitrarily — and the auto-created Receipt at asset submit always
		shares the date of a same-day transfer. The transfer then appeared
		to do nothing: history recorded the new location while the asset
		kept the old one (TC-037). Break the tie on document order."""
		if not self._enterprise():
			return super().get_latest_location_and_custodian(asset)

		row = frappe.db.sql(
			"""
			select asm_item.target_location, asm_item.to_employee
			from `tabAsset Movement Item` asm_item
			join `tabAsset Movement` asm on asm_item.parent = asm.name
			where asm_item.asset = %(asset)s
			  and asm.company = %(company)s
			  and asm.docstatus = 1
			order by asm.transaction_date desc, asm.creation desc
			limit 1
			""",
			{"asset": asset, "company": self.company},
		)
		return (row[0][0], row[0][1]) if row else ("", "")

	def on_submit(self):
		super().on_submit()
		if not self._enterprise():
			return
		from asset_enterprise.tcc import add_snapshot_activity

		for d in self.assets:
			# GAP-022 / §5.2 (Phase 11b): ONE history row summarising all
			# changes of this movement, with the value snapshot.
			changes = []
			if d.get("target_location"):
				changes.append(_("location → {0}").format(d.target_location))
			if d.get("to_employee"):
				changes.append(_("custodian → {0}").format(d.to_employee))
			if d.get("target_cost_center"):
				changes.append(_("cost center → {0}").format(d.target_cost_center))
			if changes:
				add_snapshot_activity(
					d.asset,
					_("Movement {0}: {1}").format(self.name, "; ".join(changes)),
					transaction_type="Movement",
				)
		for d in self.assets:
			# GAP-022 / TC-037: core assigns the custodian only on the
			# Issue purposes, so a combined Transfer that also names an
			# employee recorded the change in history but left the asset's
			# Custodian blank.
			if d.get("to_employee") and self.purpose == "Transfer":
				frappe.db.set_value(
					"Asset", d.asset, "custodian", d.to_employee, update_modified=False
				)
			if d.get("target_cost_center"):
				prior = frappe.db.get_value("Asset", d.asset, "cost_center")
				if not d.get("source_cost_center"):
					d.db_set("source_cost_center", prior, update_modified=False)
				frappe.db.set_value(
					"Asset", d.asset, "cost_center", d.target_cost_center, update_modified=False
				)
				# The PRIOR cost centre must ride along explicitly — the
				# asset's own field already carries the TARGET by now, and
				# the split's fallback read it, tagging BOTH halves of the
				# transfer month with the new CC (client, 20/08,
				# ACC-ASM-2026-01092: the 01-13/08 portion posted to the
				# new cost centre).
				self._split_current_period(d.asset, d.target_cost_center, prior)

	def on_cancel(self):
		super().on_cancel()
		if not self._enterprise():
			return
		for d in self.assets:
			if d.get("target_cost_center"):
				# GAP-028: restore prior CC (may legitimately be empty)
				# for future depreciation routing.
				frappe.db.set_value(
					"Asset",
					d.asset,
					"cost_center",
					d.get("source_cost_center") or None,
					update_modified=False,
				)

	def _split_current_period(self, asset_name, new_cc, old_cc=None):
		"""GAP-021: split the covering unposted row at the transfer date —
		days BEFORE the transfer stay on the old cost centre, days from
		the transfer on the new one."""
		from asset_enterprise.depreciation import split_period_for_cc_change

		transfer_date = getdate(self.transaction_date)
		row = frappe.db.sql(
			"""
			select ds.name, ds.parent, ds.schedule_date, ds.depreciation_amount,
			       ds.accumulated_depreciation_amount, ds.cost_center, ds.idx,
			       ds.days_in_period, ds.daily_rate
			from `tabDepreciation Schedule` ds
			join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
			  and ifnull(ds.journal_entry, '') = ''
			  and ds.schedule_date >= %s
			order by ds.schedule_date
			limit 1
			""",
			(asset_name, transfer_date),
			as_dict=True,
		)
		if not row:
			return
		row = row[0]
		period_start = get_first_day(row.schedule_date)
		if transfer_date <= period_start or transfer_date >= getdate(row.schedule_date):
			# Transfer on a boundary — no split needed; tag the row for
			# the side of the boundary it lies on. A row that ENDS on the
			# transfer date is wholly pre-transfer and must keep the OLD
			# cost centre (untagged it would fall back to the asset's
			# field, which already carries the target).
			boundary_cc = new_cc if transfer_date <= period_start else (row.cost_center or old_cc)
			frappe.db.set_value(
				"Depreciation Schedule", row.name, "cost_center", boundary_cc, update_modified=False
			)
			return

		days_total = row.days_in_period or (date_diff(row.schedule_date, period_start) + 1)
		days_old = date_diff(transfer_date, period_start)
		old_amount, new_amount = split_period_for_cc_change(
			flt(row.depreciation_amount), days_total, days_old, self.company
		)

		# Shrink the existing row to the old-CC portion. The asset's own
		# cost_center field carries the TARGET by the time this runs, so
		# it must never be the fallback here.
		frappe.db.set_value(
			"Depreciation Schedule",
			row.name,
			{
				"depreciation_amount": old_amount,
				"schedule_date": transfer_date,
				"cost_center": row.cost_center or old_cc,
				"days_in_period": days_old,
				"accumulated_depreciation_amount": flt(row.accumulated_depreciation_amount)
				- new_amount,
			},
			update_modified=False,
		)
		# ...and add the new-CC remainder row after it.
		new_row = frappe.get_doc(
			{
				"doctype": "Depreciation Schedule",
				"parenttype": "Asset Depreciation Schedule",
				"parent": row.parent,
				"parentfield": "depreciation_schedule",
				"idx": row.idx + 1,
				"schedule_date": row.schedule_date,
				"depreciation_amount": new_amount,
				"accumulated_depreciation_amount": row.accumulated_depreciation_amount,
				"cost_center": new_cc,
				"days_in_period": days_total - days_old,
				"daily_rate": row.daily_rate,
			}
		)
		new_row.flags.ignore_permissions = True
		new_row.db_insert()
		frappe.db.sql(
			"""update `tabDepreciation Schedule`
			   set idx = idx + 1
			   where parent = %s and name != %s and idx > %s""",
			(row.parent, new_row.name, row.idx),
		)

	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()
