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
	seed_masters()
	seed_setting_defaults()
	register_asset_accounting_dimension()


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
