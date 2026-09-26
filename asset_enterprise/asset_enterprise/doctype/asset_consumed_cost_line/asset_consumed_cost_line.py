from frappe.model.document import Document


class AssetConsumedCostLine(Document):
	"""One invoice cost line an Asset Repair or Asset Capitalization
	capitalizes, and the GL credit that takes it (asset_enterprise.consumption)."""

	pass
