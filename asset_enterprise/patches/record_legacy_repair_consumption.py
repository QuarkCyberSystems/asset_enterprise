# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""Record the invoice cost lines of capitalized repairs submitted before
the Consumed Cost Lines record existed.

Such a repair posted core's credit: one per invoice row, merged per
account, naming no line. The record is written by the same rule a new
repair uses (project first, each line capped at its live net, oldest
repair first), marked `recorded_at_upgrade`, and names no GL row - the GL
is not touched. From then on:

- a new repair's or capitalization's capacity sees what the legacy repair
  took (chief r5 S-1), and
- a reader of the project ledger places the legacy credit on the lines the
  record names instead of guessing (fresh-install r3).

A legacy repair whose amount its invoice's lines cannot cover is recorded
as far as the lines go and printed. Idempotent: a recorded repair is no
longer legacy.
"""

import frappe


def execute():
	from asset_enterprise import consumption

	if not frappe.db.table_exists(consumption.LINE_DOCTYPE):
		return
	allocation = consumption.record_legacy_repairs()
	multi = [name for name, plan in allocation.items() if len(plan["rows"]) > 1]
	short = {name: plan["short"] for name, plan in allocation.items() if plan["short"]}
	print(
		"asset_enterprise: legacy capitalized repairs recorded {0} (rows {1}); on two or more lines: {2}; "
		"not covered by their invoices' lines: {3}".format(
			len(allocation),
			sum(len(plan["rows"]) for plan in allocation.values()),
			", ".join(multi[:50]) or "none",
			"; ".join(f"{name} {items}" for name, items in list(short.items())[:50]) or "none",
		)
	)
