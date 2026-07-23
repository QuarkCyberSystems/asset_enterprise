"""Account resolution for GA-0005-01 v2.14 (§3.5).

Two chains:

1. Generic enterprise accounts (suspense, PYA, invoice diff, impairment,
   revaluation OCI, FA count discovery, capitalization clearing):
       Asset Category Account (per company, per category)
         -> Company `default_<field>`
         -> throw

2. Disposal-family accounts (per 2026-07-14 meeting — Scrape Type is
   authoritative):
       Scrapping Type Account row (per reason, per company)
         -> Asset Category Account `disposal_account_override`
         -> Company `disposal_account`
         -> throw
"""

import frappe
from frappe import _

GENERIC_FIELDS = frozenset(
	{
		"asset_suspense_account",
		"pya_expense_account",
		"asset_invoice_difference_account",
		"post_disposal_invoice_diff_account",
		"impairment_loss_account",
		"revaluation_surplus_oci_account",
		"fa_count_discovery_account",
		"capitalization_clearing_account",
	}
)


def get_enterprise_account(fieldname, company, asset_category=None):
	"""Resolve a generic enterprise account via ACA -> Company default."""
	if fieldname not in GENERIC_FIELDS:
		frappe.throw(_("Unknown enterprise account field: {0}").format(fieldname))

	if asset_category:
		account = frappe.db.get_value(
			"Asset Category Account",
			{"parent": asset_category, "company_name": company},
			fieldname,
		)
		if account:
			return account

	account = frappe.db.get_value("Company", company, f"default_{fieldname}")
	if account:
		return account

	frappe.throw(
		_(
			"Please set {0} on Asset Category Account for category {1} (company {2}), "
			"or its Company-level default."
		).format(frappe.unscrub(fieldname), asset_category or _("(none)"), company),
		title=_("Enterprise Asset Account Missing"),
	)


def get_disposal_account(company, scrapping_type=None, asset_category=None):
	"""Resolve the disposal account via Scrape Type -> ACA override -> Company."""
	if scrapping_type:
		account = frappe.db.get_value(
			"Scrapping Type Account",
			{"parent": scrapping_type, "company": company},
			"gl_account",
		)
		if account:
			return account

	if asset_category:
		account = frappe.db.get_value(
			"Asset Category Account",
			{"parent": asset_category, "company_name": company},
			"disposal_account_override",
		)
		if account:
			return account

	account = frappe.db.get_value("Company", company, "disposal_account")
	if account:
		return account

	frappe.throw(
		_(
			"No disposal account configured: set it on Scrapping Type {0}, "
			"or as Disposal Account Override on Asset Category {1}, "
			"or as the Company disposal account."
		).format(scrapping_type or _("(none)"), asset_category or _("(none)")),
		title=_("Disposal Account Missing"),
	)


def get_disposal_cost_center(company, scrapping_type=None):
	"""Optional cost center from the Scrapping Type row (falls back to None)."""
	if scrapping_type:
		return frappe.db.get_value(
			"Scrapping Type Account",
			{"parent": scrapping_type, "company": company},
			"cost_center",
		)
	return None


def get_last_period_tolerance(company):
	"""§4.10 point 4-5: per-company tolerance from Asset Settings.

	Default when unset: 100 x the company currency's smallest fraction
	(e.g. SAR 1.00).
	"""
	tolerance = frappe.db.get_value(
		"Asset Settings Tolerance",
		{"parent": "Asset Settings", "company": company},
		"default_last_period_tolerance_amount",
	)
	if tolerance:
		return tolerance

	currency = frappe.db.get_value("Company", company, "default_currency")
	fraction = (
		frappe.db.get_value("Currency", currency, "smallest_currency_fraction_value") or 0.01
	)
	return 100 * fraction
