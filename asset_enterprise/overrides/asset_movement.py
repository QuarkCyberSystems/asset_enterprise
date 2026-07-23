from erpnext.assets.doctype.asset_movement.asset_movement import AssetMovement


class EnterpriseAssetMovement(AssetMovement):
	"""GA-0005-01 v2.14 Asset Movement overrides.

	Phase 0: pass-through. Phase 8 lands:
	- mutual-exclusion removal: to_employee + target_location +
	  target_cost_center in one movement (GAP-022 / VR-026)
	- cost center transfer for future depreciation routing (GAP-020/021)
	- on_cancel restores prior cost_center; no GL (GAP-028)
	"""

	pass
