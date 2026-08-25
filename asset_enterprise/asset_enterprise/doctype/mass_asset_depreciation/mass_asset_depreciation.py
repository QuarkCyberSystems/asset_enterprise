import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_last_day, getdate

MONTHS = [
	"January", "February", "March", "April", "May", "June",
	"July", "August", "September", "October", "November", "December",
]


class MassAssetDepreciation(Document):
	def validate(self):
		self._resolve_period()

	def _resolve_period(self):
		"""The run names a PERIOD; the system supplies the date.

		§4.6 posts depreciation on the last day of the period, and since
		11c D3 removed the force_eom_depreciation flag that is the
		standing rule — so a typed posting date could only ever disagree
		with it (client, 25/08: "the posting date should not be an
		editable field, we should have period and system put the date as
		the last day of month").

		A caller that supplies only a posting date — the API, a data
		import, our own suites — has its period read back off that date,
		so nothing that worked before stops working.
		"""
		if not (self.period_month and self.period_year) and self.posting_date:
			existing = getdate(self.posting_date)
			self.period_month = MONTHS[existing.month - 1]
			self.period_year = existing.year

		if not (self.period_month and self.period_year):
			frappe.throw(_("Select the period this depreciation run covers."))
		if self.period_month not in MONTHS:
			frappe.throw(_("{0} is not a month.").format(self.period_month))
		if not (1900 < int(self.period_year) < 3000):
			frappe.throw(_("{0} is not a year.").format(self.period_year))

		self.posting_date = get_last_day(
			getdate(f"{int(self.period_year)}-{MONTHS.index(self.period_month) + 1:02d}-01")
		)

	def on_submit(self):
		from asset_enterprise.mass_depreciation import execute_mass_depreciation

		execute_mass_depreciation(self)
