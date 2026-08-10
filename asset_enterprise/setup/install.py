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


def seed_setting_defaults():
	"""Defaults that must hold at DB level (Singles store no schema
	default until the form is saved). Option B (v2.16 CH-05) ships ON."""
	if frappe.db.get_single_value("Asset Settings", "warn_invoice_below_receipt") is None:
		frappe.db.set_single_value("Asset Settings", "warn_invoice_below_receipt", 1)


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
