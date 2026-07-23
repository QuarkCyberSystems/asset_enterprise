from erpnext.assets.doctype.asset.asset import Asset


class EnterpriseAsset(Asset):
	"""GA-0005-01 v2.14 Asset overrides.

	Phase 0: pass-through. Behavior lands in later phases:
	- Phase 2: TCC Addition on submit (suspense JE for existing assets, GAP-001)
	- Phase 4: on_cancel block-and-error gate when posted depreciation exists (GAP-027)
	- Phase 6: mandatory location (VR-040), single-disposal-linkage guard (VR-041)
	"""

	pass
