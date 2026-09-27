import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_first_day, get_last_day, getdate

from asset_enterprise.asset_enterprise.doctype.mass_asset_depreciation.mass_asset_depreciation import (
	MONTHS,
)


class MassDepreciationReversal(Document):
	"""Reverses the depreciation booked for one period across a scope of
	assets (client, 27/09) — the counterpart of Mass Asset Depreciation.
	Each entry is reversed by a Reversal Journal Entry; the reversed
	periods become due again and post on the next depreciation run."""

	def validate(self):
		if self.period_month not in MONTHS:
			frappe.throw(_("{0} is not a month.").format(self.period_month))
		if not (1900 < int(self.period_year or 0) < 3000):
			frappe.throw(_("{0} is not a year.").format(self.period_year))
		if self.mode == "Mass Depreciation Run":
			run = frappe.db.get_value(
				"Mass Asset Depreciation", self.mass_asset_depreciation,
				["docstatus", "company"], as_dict=True,
			)
			if not run or run.docstatus != 1 or run.company != self.company:
				frappe.throw(
					_("{0} is not a submitted Mass Asset Depreciation of {1}.").format(
						self.mass_asset_depreciation, self.company
					)
				)

	@property
	def period_start(self):
		return get_first_day(
			getdate(f"{int(self.period_year)}-{MONTHS.index(self.period_month) + 1:02d}-01")
		)

	@property
	def period_end(self):
		return get_last_day(self.period_start)

	def on_submit(self):
		from asset_enterprise.depreciation_reversal import execute_mass_reversal

		execute_mass_reversal(self)

	def before_cancel(self):
		frappe.throw(
			_(
				"{0} cannot be cancelled: its reversals are posted and stay on the "
				"ledger. The reversed periods are due again — post them with Mass "
				"Asset Depreciation."
			).format(self.name),
			title=_("Not Cancellable"),
		)
