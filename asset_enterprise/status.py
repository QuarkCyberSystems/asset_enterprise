"""Asset statuses the enterprise flows branch on — one list, read everywhere.

Five modules used to keep their own copy and they drifted: Case A.02 left
out "Disposed", so an invoice arriving after a merge was booked as a value
adjustment on the merged-away source instead of being expensed (client,
27/09, ACC-ASS-2026-00043).
"""

# An asset in one of these has left the active register: nothing
# depreciates it, no value adjustment lands on it, an invoice difference
# that arrives afterwards is expensed (GAP-012 Case A.02) and no further
# disposal is permitted (VR-041). "Disposed" is a source merged into a
# composite, which stays submitted (CH-26).
OFF_REGISTER = ("Scrapped", "Sold", "Disposed", "Capitalized", "Cancelled")


def off_register(status):
	return status in OFF_REGISTER


# A submitted asset on the register that is not depreciating (client,
# 28/09, FA-009). Core calls it "Submitted" whether depreciation is still
# to be set up or will never run; the register has to tell the two apart.
# The category decides which: core's own Non Depreciable Category flag
# marks the classes that never depreciate (land, for instance).
PENDING_DEPRECIATION_SETUP = "Pending Depreciation Setup"
NON_DEPRECIABLE = "Non-Depreciable"
NOT_DEPRECIATING = (PENDING_DEPRECIATION_SETUP, NON_DEPRECIABLE)

# Every status of a submitted asset that is still on the register and
# not in maintenance: core's ("Submitted", "Partially Depreciated",
# "Fully Depreciated") plus the two above. Core hard-codes its three in
# the cancel check and the Asset form buttons, which are extended to the
# two new ones. (Core's Sales Invoice asset picker has the same list,
# but on an enterprise site the Asset accounting dimension, fieldname
# `asset`, replaces that query and lists every asset of the company.)
ON_REGISTER = ("Submitted", "Partially Depreciated", "Fully Depreciated") + NOT_DEPRECIATING


def not_depreciating_status(asset_category):
	"""The status of a submitted asset with depreciation off."""
	import frappe
	from frappe.utils import cint

	if asset_category and cint(
		frappe.get_cached_value("Asset Category", asset_category, "non_depreciable_category")
	):
		return NON_DEPRECIABLE
	return PENDING_DEPRECIATION_SETUP


def live_status(asset):
	"""The status an asset returns to when it comes back onto the
	register (scrap restore): depreciating or not, nothing about its
	accumulated depreciation is known to the caller beyond that."""
	if asset.calculate_depreciation:
		return "Partially Depreciated"
	return not_depreciating_status(asset.asset_category)
