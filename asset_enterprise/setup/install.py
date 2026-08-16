"""Install / migrate sync for asset_enterprise — GA-0005-01 Phase 1.

Everything here is idempotent: safe to run on every after_install and
after_migrate.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

from asset_enterprise.setup.custom_fields import CUSTOM_FIELDS

TRANSACTION_CATEGORIES = [
	("Addition", "Creates or increases asset value (purchase, capitalization, invoice adjustment, capitalized maintenance, discovery)."),
	("Disposal", "Removes asset value in full or part (sale, scrapping, partial disposal, merge-out to composite)."),
	("Impairment", "Value reduction below carrying value (IAS 36)."),
	("Revaluation", "Upward fair-value revaluation to OCI (IAS 16). No downward variant — that is Impairment."),
	("Useful Life Adjustment", "Extends or shortens useful life; prospective schedule recalc, no direct value GL."),
	("Depreciation", "Periodic consumption of the depreciable base via the daily-rate engine."),
]

SCRAPPING_TYPES = [
	"Damage",
	"Donation",
	"Obsolescence",
	"Reduced Value",
	"Insurance Claim",
	"Partial Disposal",
	"Missing Asset",
	"Physically Scrapped",
	"Stolen Asset",
]


def after_install():
	sync_customizations()


def after_migrate():
	sync_customizations()


def sync_customizations():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	apply_property_setters()
	_ava_property_setters()
	seed_masters()
	seed_setting_defaults()
	register_asset_accounting_dimension()
	rebuild_asset_tree_nodes()
	extend_assets_sidebar()


ENTERPRISE_SIDEBAR_GROUP = "Enterprise Assets"
ENTERPRISE_SIDEBAR_ITEMS = [
	{"label": "Scrap Transaction", "link_type": "DocType", "link_to": "Scrap Transaction", "icon": "delete"},
	{"label": "Mass Asset Depreciation", "link_type": "DocType", "link_to": "Mass Asset Depreciation", "icon": "stack"},
	{"label": "Financial Treatment", "link_type": "DocType", "link_to": "Financial Treatment", "icon": "file"},
	{"label": "Asset Settings", "link_type": "DocType", "link_to": "Asset Settings", "icon": "setting-gear"},
	{"label": "Scrapping Type", "link_type": "DocType", "link_to": "Scrapping Type", "icon": "tag"},
	{"label": "Transaction Category", "link_type": "DocType", "link_to": "Transaction Category", "icon": "folder-normal"},
]
ENTERPRISE_SIDEBAR_REPORTS = [
	"Asset Tree",
	"Composite Merge Log Report",
	"Replacement Chain",
	"Asset Daily Reconciliation",
]


def extend_assets_sidebar():
	"""v16 desk: the app sidebar is a Workspace Sidebar doc synced from
	erpnext's workspace_sidebar/assets.json. Extend it in place — an
	'Enterprise Assets' group after Asset Movement, and our reports in
	the existing Reports group. Idempotent; our app migrates after
	erpnext, so this self-heals whenever erpnext resyncs its sidebar."""
	try:
		if not frappe.db.exists("Workspace Sidebar", "Assets"):
			return
		# the old Workspace-page approach is superseded by this
		frappe.delete_doc("Workspace", "Asset Enterprise", force=1, ignore_missing=True)

		sidebar = frappe.get_doc("Workspace Sidebar", "Assets")
		rows = [
			{
				"label": i.label,
				"type": i.type,
				"link_type": i.link_type,
				"link_to": i.link_to,
				"icon": i.icon,
				"child": i.child,
				"indent": i.indent,
				"collapsible": i.collapsible,
				"keep_closed": i.keep_closed,
				"show_arrow": i.show_arrow,
				"url": i.url,
			}
			for i in sidebar.items
		]
		labels = [r["label"] for r in rows]
		changed = False

		if ENTERPRISE_SIDEBAR_GROUP not in labels:
			anchor = labels.index("Asset Movement") + 1 if "Asset Movement" in labels else len(rows)
			group = [
				{
					"label": ENTERPRISE_SIDEBAR_GROUP,
					"type": "Section Break",
					"link_type": "DocType",
					"indent": 1,
					"collapsible": 1,
				}
			] + [
				{
					"label": item["label"],
					"type": "Link",
					"link_type": item["link_type"],
					"link_to": item["link_to"],
					"icon": item.get("icon"),
					"child": 1,
					"collapsible": 1,
				}
				for item in ENTERPRISE_SIDEBAR_ITEMS
			]
			rows[anchor:anchor] = group
			changed = True

		labels = [r["label"] for r in rows]
		if "Reports" in labels:
			start = labels.index("Reports")
			end = start + 1
			while end < len(rows) and rows[end].get("child"):
				end += 1
			existing = {rows[i].get("link_to") for i in range(start + 1, end)}
			additions = [
				{
					"label": report,
					"type": "Link",
					"link_type": "Report",
					"link_to": report,
					"child": 1,
					"collapsible": 1,
				}
				for report in ENTERPRISE_SIDEBAR_REPORTS
				if report not in existing
			]
			if additions:
				rows[end:end] = additions
				changed = True

		if changed:
			sidebar.set("items", rows)
			sidebar.flags.ignore_permissions = True
			sidebar.save()
			frappe.clear_cache()
			print("asset_enterprise: Assets sidebar extended (Enterprise Assets group + reports)")
	except Exception:
		frappe.log_error(
			title="asset_enterprise: sidebar extension failed", message=frappe.get_traceback()
		)


def rebuild_asset_tree_nodes():
	"""GAP-009: backfill Asset Tree nodes from Asset.parent_asset links
	(covers links set via db_set / imports that bypass hooks)."""
	try:
		from asset_enterprise.asset_enterprise.doctype.asset_tree.asset_tree import rebuild_all

		rebuild_all()
	except Exception:
		frappe.log_error(
			title="asset_enterprise: asset tree rebuild failed", message=frappe.get_traceback()
		)


def register_asset_accounting_dimension():
	"""GAP-023 (Phase 11c D5, recommended option): ship "Asset" as an
	Accounting Dimension so GL entries can be filtered/grouped by
	asset. Idempotent; dimension creation adds the `asset` dimension
	field across GL-mapped doctypes."""
	try:
		if frappe.db.exists("Accounting Dimension", {"document_type": "Asset"}):
			return
		dim = frappe.get_doc(
			{"doctype": "Accounting Dimension", "document_type": "Asset"}
		)
		dim.flags.ignore_permissions = True
		dim.insert()
		frappe.db.commit()
		print("asset_enterprise: registered 'Asset' as Accounting Dimension (GAP-023)")
	except Exception:
		frappe.log_error(
			title="asset_enterprise: Accounting Dimension registration failed",
			message=frappe.get_traceback(),
		)


def seed_setting_defaults():
	"""Defaults that must hold at DB level (Singles store no schema
	default until the form is saved). Option B (v2.16 CH-05) ships ON."""
	if frappe.db.get_single_value("Asset Settings", "warn_invoice_below_receipt") is None:
		frappe.db.set_single_value("Asset Settings", "warn_invoice_below_receipt", 1)
	_warn_if_immutable_ledger_off()


def _warn_if_immutable_ledger_off():
	"""Go-live prerequisite (audit D1): with Accounts Settings
	`enable_immutable_ledger` OFF, core make_reverse_gl_entries flags
	the ORIGINAL GL entries is_cancelled=1 — breaking the
	"original stays posted" invariant of every mirror reversal
	(Repair / Capitalized Maintenance / restore paths)."""
	try:
		enterprise_on = frappe.db.get_single_value("Asset Settings", "enable_enterprise_assets")
		immutable_on = frappe.db.get_single_value("Accounts Settings", "enable_immutable_ledger")
		if enterprise_on and not immutable_on:
			message = (
				"asset_enterprise WARNING: Accounts Settings 'Enable Immutable Ledger' is OFF. "
				"Mirror reversals (Asset Repair / Capitalized Maintenance / restore) will "
				"flag original GL entries as cancelled instead of preserving them. "
				"Turn the flag ON before go-live (GA-0001-01 prerequisite)."
			)
			print(message)
			frappe.log_error(title="asset_enterprise: immutable ledger OFF", message=message)
	except Exception:
		pass  # settings not migrated yet on fresh installs


def apply_property_setters():
	# GAP-031: "Superseded" status — reschedule marks the old schedule
	# Superseded instead of cancelling it (immutable ledger).
	make_property_setter(
		"Asset Depreciation Schedule",
		"status",
		"options",
		"Draft\nActive\nSuperseded\nCancelled",
		"Text",
		validate_fields_for_doctype=False,
	)
	# Core hides the status field, which leaves nothing on screen saying
	# which schedule is the live one — unreadable once an asset carries a
	# superseded schedule. Show it, list it, and let users filter on it.
	for prop, value, prop_type in (
		("hidden", "0", "Check"),
		("in_list_view", "1", "Check"),
		("in_standard_filter", "1", "Check"),
		("bold", "1", "Check"),
	):
		make_property_setter(
			"Asset Depreciation Schedule",
			"status",
			prop,
			value,
			prop_type,
			validate_fields_for_doctype=False,
		)


def _ava_property_setters():
	"""A Useful Life Adjustment moves no money, so core's mandatory
	Difference Account has nothing to point at — TC-025/TC-026 say the
	user only sets adjusted_life_months."""
	make_property_setter(
		"Asset Value Adjustment", "difference_account", "reqd", "0", "Check",
		validate_fields_for_doctype=False,
	)
	make_property_setter(
		"Asset Value Adjustment",
		"difference_account",
		"mandatory_depends_on",
		"eval:doc.transaction_type != 'Useful Life Adjustment'",
		"Data",
		validate_fields_for_doctype=False,
	)


def seed_masters():
	for name, description in TRANSACTION_CATEGORIES:
		if not frappe.db.exists("Transaction Category", name):
			frappe.get_doc(
				{
					"doctype": "Transaction Category",
					"category_name": name,
					"description": description,
					"supports_reverse_mode": 1,
				}
			).insert(ignore_permissions=True)

	for name in SCRAPPING_TYPES:
		if not frappe.db.exists("Scrapping Type", name):
			frappe.get_doc(
				{
					"doctype": "Scrapping Type",
					"scrapping_type_name": name,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)

	frappe.db.commit()
