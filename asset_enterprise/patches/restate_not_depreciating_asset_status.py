# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Give submitted assets with depreciation off their FA-009 status.

Core stored "Submitted" for them. From this release a submitted asset
that is not depreciating reads "Non-Depreciable" when its category is
flagged Non Depreciable, else "Pending Depreciation Setup". Only core's
"Submitted" (and the two new statuses) are restated, per category, by the
same rule a flag change applies. Idempotent.
"""

import frappe
from frappe.utils import cint


def execute():
	from asset_enterprise.depreciation import enterprise_enabled
	from asset_enterprise.overrides.asset_category import restate_not_depreciating_assets

	if not enterprise_enabled():
		return
	for category in frappe.get_all(
		"Asset Category", fields=["name", "non_depreciable_category"]
	):
		restate_not_depreciating_assets(category.name, cint(category.non_depreciable_category))
