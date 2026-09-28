from frappe.model.document import Document


class AssetSettings(Document):
	def on_update(self):
		# Switching Enterprise Assets on gives the submitted assets with
		# depreciation off their FA-009 status at once, not at the next
		# migrate.
		if self.has_value_changed("enable_enterprise_assets") and self.enable_enterprise_assets:
			from asset_enterprise.overrides.asset_category import restate_all_not_depreciating

			restate_all_not_depreciating()
