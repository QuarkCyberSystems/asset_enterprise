from frappe.model.document import Document


class MassAssetDepreciation(Document):
	def on_submit(self):
		from asset_enterprise.mass_depreciation import execute_mass_depreciation

		execute_mass_depreciation(self)
