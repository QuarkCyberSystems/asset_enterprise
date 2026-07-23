from erpnext.assets.doctype.asset_repair.asset_repair import AssetRepair


class EnterpriseAssetRepair(AssetRepair):
	"""GA-0005-01 v2.14 Asset Repair overrides.

	Phase 0: pass-through. Phase 4 lands:
	- on_cancel -> create_reversal_repair() instead of make_gl_entries(cancel=True)
	  + update_asset_value() field mutation (GAP-033)
	- fully-depreciated gate: reversal prohibited when NBV == salvage and all
	  schedule rows posted (VR-038)
	- Reversal Repair on_submit: mirror JE via make_reverse_gl_entries + Stock
	  Entry (Material Receipt) return of consumed items
	"""

	pass
