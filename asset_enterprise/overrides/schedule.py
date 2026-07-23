from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	AssetDepreciationSchedule,
)


class EnterpriseSchedule(AssetDepreciationSchedule):
	"""GA-0005-01 v2.14 Asset Depreciation Schedule overrides.

	Phase 0: pass-through. Phase 3 lands:
	- "Superseded" status flow: reschedule never calls .cancel() on the
	  active schedule (GAP-031); posted rows preserved (GAP-032)
	- daily-rate prospective engine fields per row (cost_center,
	  is_pya_entry, days_in_period, daily_rate)
	- final-row drift absorption per §4.10 (fa_module_round)
	"""

	pass
