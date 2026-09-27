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
