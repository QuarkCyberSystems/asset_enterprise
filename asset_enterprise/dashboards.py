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


def asset_dashboard(data=None):
	"""Connections on the Asset: every FA document that touched it.

	Core lists Asset Movement alone, so an asset whose value moved —
	scrapped, reversed, revalued, repaired, merged — showed nothing on
	screen explaining it. The trail existed in Financial Treatment and
	Asset Activity, but only if you knew to go looking (client, 25/08:
	"where do i find this trail?").

	Display only. Links come from the Link fields themselves
	(frappe.desk.form.linked_with reads the schema, never a dashboard),
	so this changes nothing about cancellation cascades or link checks —
	those already see these doctypes, and the Asset / Asset Depreciation
	Schedule guards already handle them.

	Deliberately NOT everything that carries an `asset` field: the Asset
	accounting dimension (Phase 11c D5) put one on every GL-bearing
	doctype, and a Sales Invoice merely tagged with the dimension is not
	a document about this asset.
	"""
	out = frappe._dict(data or {})
	out.fieldname = "asset"
	# Asset Capitalization reaches the asset as the merge TARGET; the
	# consumed side is a child table core already resolves.
	non_standard = dict(out.get("non_standard_fieldnames") or {})
	non_standard["Asset Capitalization"] = "target_asset"
	out.non_standard_fieldnames = non_standard

	groups = [
		{"label": _("Scrapping"), "items": ["Scrap Transaction"]},
		{"label": _("Value Changes"), "items": ["Asset Value Adjustment", "Asset Repair"]},
		{"label": _("Capitalization"), "items": ["Asset Capitalization"]},
		{"label": _("Depreciation"), "items": ["Asset Depreciation Schedule"]},
		{"label": _("Financial Impact"), "items": ["Financial Treatment"]},
	]
	existing = list(out.get("transactions") or [])
	listed = {i for g in existing for i in g.get("items", [])}
	for g in groups:
		g["items"] = [i for i in g["items"] if i not in listed]
	out.transactions = existing + [g for g in groups if g["items"]]
	return out


def capitalization_dashboard(data=None):
	"""Connections on an Asset Capitalization: what it actually created.

	The document posts a Material Issue, journal entries and treatments,
	and none of them were reachable from it — core ships no dashboard for
	this doctype (client, 25/08: "there is no links or hints to these
	documents").

	Display only: links come from the fields themselves, so this changes
	nothing about cancellation. The Stock Entry back-link DID widen the
	desk's Cancel-All cascade, but that came from the field added with
	stock consumption, and is handled by exempting Stock Entry and
	refusing a direct cancel of the issue.
	"""
	out = frappe._dict(data or {})
	out.fieldname = "asset_capitalization"
	out.non_standard_fieldnames = dict(out.get("non_standard_fieldnames") or {}, **{
		# the reversal counter-document points back at its source
		"Asset Capitalization": "reversal_of_capitalization",
		"Financial Treatment": "source_name",
		"Asset Depreciation Schedule": "triggered_by",
	})
	out.dynamic_links = dict(out.get("dynamic_links") or {}, **{
		"source_name": ["Asset Capitalization", "source_doctype"],
		"triggered_by": ["Asset Capitalization", "triggered_by_doctype"],
	})
	groups = [
		{"label": _("Materials"), "items": ["Stock Entry"]},
		{"label": _("Financial Impact"), "items": ["Financial Treatment"]},
		{"label": _("Schedule Generations"), "items": ["Asset Depreciation Schedule"]},
		{"label": _("Reversal"), "items": ["Asset Capitalization"]},
	]
	existing = list(out.get("transactions") or [])
	listed = {i for g in existing for i in g.get("items", [])}
	for g in groups:
		g["items"] = [i for i in g["items"] if i not in listed]
	out.transactions = existing + [g for g in groups if g["items"]]
	return out
