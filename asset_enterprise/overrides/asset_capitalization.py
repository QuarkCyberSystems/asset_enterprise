from erpnext.assets.doctype.asset_capitalization.asset_capitalization import (
	AssetCapitalization,
)


class EnterpriseAssetCapitalization(AssetCapitalization):
	"""GA-0005-01 v2.14 Asset Capitalization overrides.

	Phase 0: pass-through. Phase 5 lands:
	- transaction_type branching: Standard Capitalization / Capitalized
	  Maintenance / Reversal of Capitalized Maintenance (GAP-014)
	- two-leg merge GL via Capitalization Clearing (source Disposal +
	  composite Addition; clearing nets to zero per merge)
	- Composite Merge Log population with value snapshot (GAP-035)
	- fully_depreciated_treatment: Expense Immediately / Add Value and
	  Extend Life
	- Reclassification sub-type: readonly source/target categories,
	  standard Disposal + Addition GL (no clearing account for reclass)
	"""

	pass
