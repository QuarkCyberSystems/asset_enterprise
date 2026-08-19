"""Custom fields on erpnext doctypes — GA-0005-01 v2.14 build plan §2.4.

Applied idempotently on after_install and after_migrate via
frappe.custom.doctype.custom_field.custom_field.create_custom_fields.
"""

ACCOUNT = {"fieldtype": "Link", "options": "Account"}


def _acc(fieldname, label, insert_after, description=None):
	f = dict(ACCOUNT, fieldname=fieldname, label=label, insert_after=insert_after)
	if description:
		f["description"] = description
	return f


CUSTOM_FIELDS = {
	# ------------------------------------------------------------------ Asset
	"Asset": [
		# GAP-036 (client request 17/08/2026): a container that groups
		# physical assets without being one itself. Carries no value, never
		# depreciates, cannot be disposed — it exists to give the tree a
		# parent that is not a machine.
		{
			"fieldname": "is_group_node",
			"fieldtype": "Check",
			"label": "Grouping Asset (Non-Physical)",
			"insert_after": "asset_type",
			"description": "Groups physical assets in the Asset Tree. Holds no value, is never depreciated and cannot be disposed.",
		},
		{
			"fieldname": "enterprise_tab",
			"fieldtype": "Tab Break",
			"label": "Enterprise",
			"insert_after": "column_break_yeds",
		},
		{
			"fieldname": "ledger_values_section",
			"fieldtype": "Section Break",
			"label": "Ledger-Derived Values",
			"insert_after": "enterprise_tab",
		},
		{
			"fieldname": "historical_asset_value",
			"fieldtype": "Currency",
			"label": "Historical Asset Value",
			"read_only": 1,
			"insert_after": "ledger_values_section",
		},
		{
			"fieldname": "accumulated_depreciation_value",
			"fieldtype": "Currency",
			"label": "Accumulated Depreciation Value",
			"read_only": 1,
			"insert_after": "historical_asset_value",
		},
		{
			"fieldname": "net_book_value",
			"fieldtype": "Currency",
			"label": "Net Book Value",
			"read_only": 1,
			"insert_after": "accumulated_depreciation_value",
		},
		{
			"fieldname": "ledger_values_cb",
			"fieldtype": "Column Break",
			"insert_after": "net_book_value",
		},
		{
			"fieldname": "remaining_useful_life_months",
			"fieldtype": "Float",
			"label": "Remaining Useful Life (Months)",
			"precision": "2",
			"read_only": 1,
			"insert_after": "ledger_values_cb",
		},
		{
			"fieldname": "remaining_useful_life_years",
			"fieldtype": "Float",
			"label": "Remaining Useful Life (Years)",
			"precision": "2",
			"read_only": 1,
			"insert_after": "remaining_useful_life_months",
		},
		{
			"fieldname": "traceability_section",
			"fieldtype": "Section Break",
			"label": "Traceability",
			"insert_after": "remaining_useful_life_years",
		},
		{
			"fieldname": "parent_asset",
			"fieldtype": "Link",
			"label": "Parent Asset",
			"options": "Asset",
			"insert_after": "traceability_section",
			"description": "Groups this asset under a parent in the Asset Tree. No GL impact.",
		},
		{
			"fieldname": "replacement_of_asset",
			"fieldtype": "Link",
			"label": "Replacement Of Asset",
			"options": "Asset",
			"read_only": 1,
			"insert_after": "parent_asset",
			"description": "Set when this Asset was created via Create Replacement Asset on a disposed Asset.",
		},
		{
			"fieldname": "replaced_by_asset",
			"fieldtype": "Link",
			"label": "Replaced By Asset",
			"options": "Asset",
			"read_only": 1,
			"insert_after": "replacement_of_asset",
		},
		{
			"fieldname": "traceability_cb",
			"fieldtype": "Column Break",
			"insert_after": "replaced_by_asset",
		},
		{
			"fieldname": "scrap_reversal_journal_entry",
			"fieldtype": "Link",
			"label": "Scrap Reversal Journal Entry",
			"options": "Journal Entry",
			"read_only": 1,
			"insert_after": "traceability_cb",
			"description": "Mirror journal entry posted when this asset was restored after disposal.",
		},
		{
			"fieldname": "merged_into_asset",
			"fieldtype": "Link",
			"label": "Merged Into Asset",
			"options": "Asset",
			"read_only": 1,
			"insert_after": "scrap_reversal_journal_entry",
			"description": "Composite this Asset was merged into via Capitalized Maintenance.",
		},
		{
			"fieldname": "reclassified_from",
			"fieldtype": "Link",
			"label": "Reclassified From",
			"options": "Asset",
			"read_only": 1,
			"insert_after": "merged_into_asset",
			# Suppresses the GAP-001 opening JE — the reclassification JE
			# carries the booking.
			"description": "Source asset this one was created from on category transfer.",
		},
		{
			"fieldname": "merge_log_section",
			"fieldtype": "Section Break",
			"label": "Composite Merge Log",
			"insert_after": "merged_into_asset",
			# GAP-035 visibility: CM targets ANY submitted asset (asset_type
			# agnostic) — show the parts whenever merges exist.
			"depends_on": "eval:doc.asset_type==='Composite Asset' || (doc.merge_log && doc.merge_log.length)",
		},
		{
			"fieldname": "merge_log",
			"fieldtype": "Table",
			"label": "Merge Log",
			"options": "Composite Merge Log Entry",
			"read_only": 1,
			"insert_after": "merge_log_section",
		},
	],
	# ------------------------------------------------- Asset Category Account
	"Asset Category Account": [
		_acc("asset_suspense_account", "Asset Suspense Account", "capital_work_in_progress_account"),
		_acc("pya_expense_account", "Prior Year Adjustment Expense Account", "asset_suspense_account"),
		_acc(
			"asset_invoice_difference_account",
			"Asset Invoice Difference / Clearing Account",
			"pya_expense_account",
		),
		_acc(
			"post_disposal_invoice_diff_account",
			"Post-Disposal Invoice Difference Expense Account",
			"asset_invoice_difference_account",
		),
		_acc("impairment_loss_account", "Impairment Loss Account", "post_disposal_invoice_diff_account"),
		_acc(
			"revaluation_surplus_oci_account",
			"Revaluation Surplus Account (OCI)",
			"impairment_loss_account",
		),
		_acc("fa_count_discovery_account", "FA Count Discovery Account", "revaluation_surplus_oci_account"),
		_acc(
			"disposal_account_override",
			"Disposal Account Override",
			"fa_count_discovery_account",
			description="Category-level override of the Company disposal account.",
		),
		_acc(
			"capitalization_clearing_account",
			"Capitalization Clearing Account",
			"disposal_account_override",
			description="Intermediary for the Capitalized Maintenance merge entries. Nets to zero per merge.",
		),
	],
	# ---------------------------------------------------------- Asset Category
	"Asset Category": [
		{
			"fieldname": "calculate_from_receiving_date",
			"fieldtype": "Check",
			"label": "Depreciation Start From Receiving Date",
			"insert_after": "enable_cwip_accounting",
			"description": "When set, depreciation counts from the Purchase Receipt posting date for assets in this category.",
		},
		{
			"fieldname": "subject_to_impairment_review",
			"fieldtype": "Check",
			"label": "Subject to Impairment Review",
			"insert_after": "calculate_from_receiving_date",
			"description": "Reporting flag only — no automatic GL impact.",
		},
	],
	# ---------------------------------------------------- Asset Capitalization
	"Asset Capitalization": [
		{
			"fieldname": "transaction_type",
			"fieldtype": "Select",
			"label": "Transaction Type",
			"options": "Standard Capitalization\nCapitalized Maintenance\nReversal of Capitalized Maintenance",
			"default": "Standard Capitalization",
			"reqd": 1,
			"insert_after": "title",
		},
		{
			"fieldname": "transaction_sub_type",
			"fieldtype": "Select",
			"label": "Transaction Sub Type",
			"options": "\nStandard Maintenance\nReclassification / Asset Category Transfer",
			"depends_on": "eval:doc.transaction_type==='Capitalized Maintenance'",
			"insert_after": "transaction_type",
		},
		{
			# Reclassification names the NEW material directly — the asset
			# is created by the transaction, like purchasing, with
			# available-for-use = the posting date (client, 19/08). A
			# pre-created draft target_asset still works as before.
			"fieldname": "target_item",
			"fieldtype": "Link",
			"label": "Target Item (New Category)",
			"options": "Item",
			"insert_after": "transaction_sub_type",
			"depends_on": "eval:doc.transaction_sub_type==='Reclassification / Asset Category Transfer'",
			"description": "The new-category material. The reclassification creates the new asset from it; available-for-use is the posting date.",
		},
		{
			"fieldname": "reversal_of_capitalization",
			"fieldtype": "Link",
			"label": "Reversal Of Capitalization",
			"options": "Asset Capitalization",
			"read_only": 1,
			"depends_on": "eval:doc.transaction_type==='Reversal of Capitalized Maintenance'",
			"mandatory_depends_on": "eval:doc.transaction_type==='Reversal of Capitalized Maintenance'",
			"insert_after": "transaction_sub_type",
		},
		{
			"fieldname": "reversed_by_capitalization",
			"fieldtype": "Link",
			"label": "Reversed By Capitalization",
			"options": "Asset Capitalization",
			"read_only": 1,
			"insert_after": "reversal_of_capitalization",
		},
		{
			"fieldname": "source_asset_category",
			"fieldtype": "Link",
			"label": "Source Asset Category",
			"options": "Asset Category",
			"read_only": 1,
			"depends_on": "eval:doc.transaction_sub_type==='Reclassification / Asset Category Transfer'",
			"insert_after": "reversed_by_capitalization",
		},
		{
			"fieldname": "target_asset_category",
			"fieldtype": "Link",
			"label": "Target Asset Category",
			"options": "Asset Category",
			"read_only": 1,
			"depends_on": "eval:doc.transaction_sub_type==='Reclassification / Asset Category Transfer'",
			"insert_after": "source_asset_category",
		},
		{
			"fieldname": "fully_depreciated_treatment",
			"fieldtype": "Select",
			"label": "Fully Depreciated Target Treatment",
			"options": "\nExpense Immediately\nAdd Value and Extend Life",
			"insert_after": "target_asset_category",
			"description": "Required when the target composite is fully depreciated.",
		},
		{
			"fieldname": "extended_life_months",
			"fieldtype": "Float",
			"label": "Extended Life (Months)",
			"precision": "2",
			"depends_on": "eval:doc.fully_depreciated_treatment==='Add Value and Extend Life'",
			"mandatory_depends_on": "eval:doc.fully_depreciated_treatment==='Add Value and Extend Life'",
			"insert_after": "fully_depreciated_treatment",
		},
	],
	# ----------------------------------------------------------- Asset Repair
	"Asset Repair": [
		{
			"fieldname": "transaction_type",
			"fieldtype": "Select",
			"label": "Transaction Type",
			"options": "Standard\nReversal",
			"default": "Standard",
			"reqd": 1,
			"insert_after": "naming_series",
		},
		{
			"fieldname": "reversal_of_repair",
			"fieldtype": "Link",
			"label": "Reversal Of Repair",
			"options": "Asset Repair",
			"read_only": 1,
			"depends_on": "eval:doc.transaction_type==='Reversal'",
			"mandatory_depends_on": "eval:doc.transaction_type==='Reversal'",
			"insert_after": "transaction_type",
		},
		{
			"fieldname": "reversed_by_repair",
			"fieldtype": "Link",
			"label": "Reversed By Repair",
			"options": "Asset Repair",
			"read_only": 1,
			"insert_after": "reversal_of_repair",
		},
	],
	# ------------------------------------------------- Asset Value Adjustment
	"Asset Value Adjustment": [
		{
			# No "Downward Revaluation" option on purpose: value drops
			# below carrying value are Impairment (IAS 16).
			"fieldname": "transaction_type",
			"fieldtype": "Select",
			"label": "Transaction Type",
			"options": "\nInitial Impairment\nUpward Revaluation\nInvoice Adjustment\nUseful Life Adjustment\nValue + Life Adjustment",
			"insert_after": "company",
		},
		{
			# GAP-013: positive extends, negative shortens; prospective recalc.
			# §3.2/§3.4: life fields belong only to the two life-bearing types.
			"fieldname": "adjusted_life_months",
			"fieldtype": "Float",
			"label": "Adjusted Life (Months)",
			"precision": "2",
			"insert_after": "new_asset_value",
			"depends_on": "eval:['Useful Life Adjustment','Value + Life Adjustment'].includes(doc.transaction_type)",
		},
		{
			# Combines with months: +3 months +15 days moves the end of
			# life by both. Days shift the horizon, not the period count.
			"fieldname": "adjusted_life_days",
			"fieldtype": "Int",
			"label": "Adjusted Life (Days)",
			"insert_after": "adjusted_life_months",
			"depends_on": "eval:['Useful Life Adjustment','Value + Life Adjustment'].includes(doc.transaction_type)",
		},
		{
			# GAP-013 exhaustion: the immediate depreciation this AVA
			# triggered. Distinct from core's journal_entry (the
			# revaluation JE, which core cancels on AVA cancel).
			"fieldname": "exhaustion_journal_entry",
			"fieldtype": "Link",
			"label": "Depreciation Entry (Life Exhausted)",
			"options": "Journal Entry",
			"read_only": 1,
			"insert_after": "adjusted_life_days",
			"depends_on": "eval:doc.exhaustion_journal_entry",
		},
		{
			"fieldname": "reversal_of_ava",
			"fieldtype": "Link",
			"label": "Reversal Of AVA",
			"options": "Asset Value Adjustment",
			"read_only": 1,
			"insert_after": "exhaustion_journal_entry",
		},
		{
			"fieldname": "reversed_by_ava",
			"fieldtype": "Link",
			"label": "Reversed By AVA",
			"options": "Asset Value Adjustment",
			"read_only": 1,
			"insert_after": "reversal_of_ava",
		},
	],
	# ------------------------------------- Asset Depreciation Schedule (parent)
	"Asset Depreciation Schedule": [
		{
			"fieldname": "supersedes",
			"fieldtype": "Link",
			"label": "Supersedes",
			"options": "Asset Depreciation Schedule",
			"read_only": 1,
			"insert_after": "status",
			"description": "The schedule this one replaced. The old schedule stays submitted with status Superseded; its posted rows are copied here verbatim.",
		},
		{
			"fieldname": "superseded_on",
			"fieldtype": "Date",
			"label": "Superseded On",
			"read_only": 1,
			"insert_after": "supersedes",
		},
		{
			# which business document caused this generation — makes the
			# "why does this asset have N superseded schedules" question
			# self-answering on screen (client, 19/08).
			"fieldname": "triggered_by_doctype",
			"fieldtype": "Link",
			"label": "Triggered By Type",
			"options": "DocType",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "superseded_on",
		},
		{
			"fieldname": "triggered_by",
			"fieldtype": "Dynamic Link",
			"label": "Triggered By",
			"options": "triggered_by_doctype",
			"read_only": 1,
			"insert_after": "triggered_by_doctype",
			"depends_on": "eval:doc.triggered_by",
		},
	],
	# ------------------------------------------- Depreciation Schedule (child)
	"Depreciation Schedule": [
		{
			"fieldname": "cost_center",
			"fieldtype": "Link",
			"label": "Cost Center",
			"options": "Cost Center",
			"insert_after": "journal_entry",
			"description": "Cost centre carrying this row after a mid-period transfer split.",
		},
		{
			"fieldname": "is_pya_entry",
			"fieldtype": "Check",
			"label": "Is Prior Year Adjustment",
			"read_only": 1,
			"insert_after": "cost_center",
		},
		{
			"fieldname": "days_in_period",
			"fieldtype": "Int",
			"label": "Days in Period",
			"read_only": 1,
			"insert_after": "is_pya_entry",
		},
		{
			"fieldname": "daily_rate",
			"fieldtype": "Float",
			"label": "Daily Rate",
			"precision": "9",
			"read_only": 1,
			"insert_after": "days_in_period",
		},
		{
			"fieldname": "reversal_journal_entry",
			"fieldtype": "Link",
			"label": "Reversal Journal Entry",
			"options": "Journal Entry",
			"read_only": 1,
			"insert_after": "daily_rate",
			"description": "Mirror journal entry that reversed this posted row "
			"(e.g. straddling depreciation reversed at merge). Row drops out of "
			"the accumulated fold; original JE stays posted.",
		},
	],
	# ------------------------------------------------- Asset Movement Item
	"Asset Movement Item": [
		{
			"fieldname": "source_cost_center",
			"fieldtype": "Link",
			"label": "Source Cost Center",
			"options": "Cost Center",
			"read_only": 1,
			"fetch_from": "asset.cost_center",
			"insert_after": "to_employee",
		},
		{
			"fieldname": "target_cost_center",
			"fieldtype": "Link",
			"label": "Target Cost Center",
			"options": "Cost Center",
			"insert_after": "source_cost_center",
			"description": "Future depreciation routes to this cost centre after the transfer.",
		},
	],
	# ------------------------------------------------------- PR / PI item flags
	"Purchase Receipt Item": [
		{
			"fieldname": "asset_linked",
			"fieldtype": "Check",
			"label": "Asset Linked",
			"read_only": 1,
			"insert_after": "is_fixed_asset",
			"description": "Set when an Asset references this row. Reverse the asset before cancelling the receipt.",
		},
	],
	"Purchase Invoice Item": [
		{
			"fieldname": "asset_linked",
			"fieldtype": "Check",
			"label": "Asset Linked",
			"read_only": 1,
			"insert_after": "is_fixed_asset",
		},
	],
	# --------------------------------------------------------- Purchase Invoice
	"Purchase Invoice": [
		{
			"fieldname": "pi_asset_allocation_section",
			"fieldtype": "Section Break",
			"label": "Asset Allocation",
			"insert_after": "items",
			"depends_on": "eval:doc.update_stock!==1",
			"collapsible": 1,
		},
		{
			"fieldname": "pi_asset_allocation",
			"fieldtype": "Table",
			"label": "PI Asset Allocation",
			"options": "PI Asset Allocation",
			"insert_after": "pi_asset_allocation_section",
			"description": "Maps this (partial) invoice to specific received Assets. Fully-invoiced assets are filtered out.",
		},
	],
	# ------------------------------------------------------------ Asset Activity
	# §5.2 Asset History enrichment — 13 fields written by the TCC
	# post-process with the post-treatment financial snapshot.
	"Asset Activity": [
		{
			"fieldname": "financial_section",
			"fieldtype": "Section Break",
			"label": "Financial Context",
			"insert_after": "column_break_kkxv",
		},
		{
			"fieldname": "financial_effect",
			"fieldtype": "Check",
			"label": "Financial Effect",
			"read_only": 1,
			"insert_after": "financial_section",
		},
		{
			"fieldname": "transaction_category",
			"fieldtype": "Link",
			"label": "Transaction Category",
			"options": "Transaction Category",
			"read_only": 1,
			"insert_after": "financial_effect",
		},
		{
			"fieldname": "transaction_type",
			"fieldtype": "Data",
			"label": "Transaction Type",
			"read_only": 1,
			"insert_after": "transaction_category",
		},
		{
			"fieldname": "transaction_amount",
			"fieldtype": "Currency",
			"label": "Transaction Amount",
			"read_only": 1,
			"insert_after": "transaction_type",
		},
		{
			"fieldname": "snapshot_cb",
			"fieldtype": "Column Break",
			"insert_after": "transaction_amount",
		},
		{
			"fieldname": "historical_asset_value_after",
			"fieldtype": "Currency",
			"label": "HAV After",
			"read_only": 1,
			"insert_after": "snapshot_cb",
		},
		{
			"fieldname": "accumulated_depreciation_after",
			"fieldtype": "Currency",
			"label": "Accum. Depreciation After",
			"read_only": 1,
			"insert_after": "historical_asset_value_after",
		},
		{
			"fieldname": "net_book_value_after",
			"fieldtype": "Currency",
			"label": "NBV After",
			"read_only": 1,
			"insert_after": "accumulated_depreciation_after",
		},
		{
			"fieldname": "useful_life_after",
			"fieldtype": "Float",
			"label": "Useful Life After (Months)",
			"precision": "2",
			"read_only": 1,
			"insert_after": "net_book_value_after",
		},
		{
			"fieldname": "remaining_useful_life_after",
			"fieldtype": "Float",
			"label": "Remaining Useful Life After (Months)",
			"precision": "2",
			"read_only": 1,
			"insert_after": "useful_life_after",
		},
		{
			"fieldname": "refs_section",
			"fieldtype": "Section Break",
			"label": "References",
			"insert_after": "remaining_useful_life_after",
		},
		{
			"fieldname": "linked_journal_entry",
			"fieldtype": "Link",
			"label": "Linked Journal Entry",
			"options": "Journal Entry",
			"read_only": 1,
			"insert_after": "refs_section",
		},
		{
			"fieldname": "source_doctype",
			"fieldtype": "Link",
			"label": "Source DocType",
			"options": "DocType",
			"read_only": 1,
			"insert_after": "linked_journal_entry",
		},
		{
			"fieldname": "source_name",
			"fieldtype": "Dynamic Link",
			"label": "Source Document",
			"options": "source_doctype",
			"read_only": 1,
			"insert_after": "source_doctype",
		},
		{
			"fieldname": "reversal_reference",
			"fieldtype": "Link",
			"label": "Reversal Reference",
			"options": "Financial Treatment",
			"read_only": 1,
			"insert_after": "source_name",
		},
	],
	# ------------------------------------------------------------------ Company
	"Company": [
		{
			"fieldname": "enterprise_asset_defaults_section",
			"fieldtype": "Section Break",
			"label": "Enterprise Asset Defaults",
			"insert_after": "capital_work_in_progress_account",
			"collapsible": 1,
		},
		_acc(
			"default_asset_suspense_account",
			"Default Asset Suspense Account",
			"enterprise_asset_defaults_section",
		),
		_acc("default_pya_expense_account", "Default PYA Expense Account", "default_asset_suspense_account"),
		_acc(
			"default_asset_invoice_difference_account",
			"Default Asset Invoice Difference / Clearing Account",
			"default_pya_expense_account",
		),
		_acc(
			"default_post_disposal_invoice_diff_account",
			"Default Post-Disposal Invoice Difference Account",
			"default_asset_invoice_difference_account",
		),
		_acc(
			"default_impairment_loss_account",
			"Default Impairment Loss Account",
			"default_post_disposal_invoice_diff_account",
		),
		_acc(
			"default_revaluation_surplus_oci_account",
			"Default Revaluation Surplus Account (OCI)",
			"default_impairment_loss_account",
		),
		_acc(
			"default_fa_count_discovery_account",
			"Default FA Count Discovery Account",
			"default_revaluation_surplus_oci_account",
		),
		_acc(
			"default_capitalization_clearing_account",
			"Default Capitalization Clearing Account",
			"default_fa_count_discovery_account",
		),
	],
}
