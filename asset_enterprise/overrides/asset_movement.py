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

		if not (d.get("target_location") or d.get("to_employee") or d.get("target_cost_center")):
			frappe.throw(
				_(
					"Row {0}: set at least one of Target Location, To Employee or "
					"Target Cost Center (VR-026)."
				).format(d.idx)
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

	def on_submit(self):
		super().on_submit()
		if not self._enterprise():
			return
		for d in self.assets:
			if d.get("target_cost_center"):
				prior = frappe.db.get_value("Asset", d.asset, "cost_center")
				if not d.get("source_cost_center"):
					d.db_set("source_cost_center", prior, update_modified=False)
				frappe.db.set_value(
					"Asset", d.asset, "cost_center", d.target_cost_center, update_modified=False
				)
				self._split_current_period(d.asset, d.target_cost_center)

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

	def _split_current_period(self, asset_name, new_cc):
		"""GAP-021: split the covering unposted row at the transfer date."""
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
			# Transfer on a boundary — no split needed; tag the row.
			frappe.db.set_value(
				"Depreciation Schedule", row.name, "cost_center", new_cc, update_modified=False
			)
			return

		days_total = row.days_in_period or (date_diff(row.schedule_date, period_start) + 1)
		days_old = date_diff(transfer_date, period_start)
		old_amount, new_amount = split_period_for_cc_change(
			flt(row.depreciation_amount), days_total, days_old, self.company
		)

		asset = frappe.db.get_value("Asset", asset_name, "cost_center")
		# Shrink the existing row to the old-CC portion...
		frappe.db.set_value(
			"Depreciation Schedule",
			row.name,
			{
				"depreciation_amount": old_amount,
				"schedule_date": transfer_date,
				"cost_center": row.cost_center or asset,
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
