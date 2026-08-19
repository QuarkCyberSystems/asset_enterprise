"""Doctype dashboards (Connections tab) — GA-0005-01.

The hard link fields carry the causal one-to-one relations (journal
entries, reversal pair); the dashboard surfaces the one-to-many side:
every Financial Treatment sourced from this document, the reversal
counterpart, and the schedule generations it triggered.
"""
import frappe
from frappe import _


def ava_dashboard(data=None):
	out = frappe._dict(data or {})
	out.fieldname = "reversal_of_ava"
	out.dynamic_links = {
		"source_name": ["Asset Value Adjustment", "source_doctype"],
		"triggered_by": ["Asset Value Adjustment", "triggered_by_doctype"],
	}
	out.non_standard_fieldnames = {
		"Financial Treatment": "source_name",
		"Asset Depreciation Schedule": "triggered_by",
	}
	out.transactions = [
		{"label": _("Financial Impact"), "items": ["Financial Treatment"]},
		{"label": _("Schedule Generations"), "items": ["Asset Depreciation Schedule"]},
		{"label": _("Reversal"), "items": ["Asset Value Adjustment"]},
	]
	return out
