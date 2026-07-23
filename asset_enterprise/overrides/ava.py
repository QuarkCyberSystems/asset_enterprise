from erpnext.assets.doctype.asset_value_adjustment.asset_value_adjustment import (
	AssetValueAdjustment,
)


class EnterpriseAVA(AssetValueAdjustment):
	"""GA-0005-01 v2.14 Asset Value Adjustment overrides.

	Phase 0: pass-through. Phase 4 lands:
	- on_cancel -> create_reversal_ava() (always-reverse, GAP-030 / VR-034);
	  retires cancel_asset_revaluation_entry() + ignore_permissions bypass
	- transaction_type routing (Initial Impairment / Upward Revaluation /
	  Invoice Adjustment / Useful Life Adjustment / Value + Life) — no
	  Downward Revaluation (per client, IAS 16)
	- adjusted_life_months (Float) UL handling (GAP-013)
	"""

	pass
