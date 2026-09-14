"""Asset Category — GAP-037 Control Category (client, 2026-09-14).

An asset the business wants to TRACK — register, custodian, location,
count — but never CAPITALISE: low-value tools, peripherals, anything
policy says is expensed on purchase. The asset record exists for
control; the money never touches the balance sheet.

Mechanically that is one rule: every account on the category is an
expense account, the fixed-asset and accumulated-depreciation accounts
included. The purchase debits expense, depreciation moves value between
two expense accounts, a scrap credits expense. Nothing downstream cares
what type those accounts are — purchase routing, the posting engine,
the values fold and the Fixed Asset Register all read the category's
account by NAME — so the only thing standing in the way was core's
`validate_account_types`, which insists on "Fixed Asset" and
"Accumulated Depreciation" account types. The flag switches that rule
for the opposite one.
"""

import frappe
from frappe import _
from frappe.utils import cint

from erpnext.assets.doctype.asset_category.asset_category import AssetCategory

CONTROLLED_ACCOUNTS = (
	"fixed_asset_account",
	"accumulated_depreciation_account",
	"depreciation_expense_account",
	"capital_work_in_progress_account",
)


def is_control_category(category):
	"""True when the category expenses its assets (GAP-037). Cached per
	request — posting paths ask this once per leg."""
	if not category:
		return False
	return bool(cint(frappe.get_cached_value("Asset Category", category, "is_control_category")))


class EnterpriseAssetCategory(AssetCategory):
	def _enterprise(self):
		from asset_enterprise.depreciation import enterprise_enabled

		return enterprise_enabled()

	def validate(self):
		if self._enterprise():
			self._lock_control_flag_once_used()
		super().validate()

	def validate_account_types(self):
		"""Core: fixed asset / accumulated depreciation / depreciation /
		CWIP must carry their matching account TYPES. Control category:
		every one of them must be an EXPENSE account instead. The two
		rules are exclusive by construction — an account cannot satisfy
		both — so a category is either wholly on the balance sheet or
		wholly in P&L, never a mixture."""
		if not (self._enterprise() and cint(self.get("is_control_category"))):
			return super().validate_account_types()
		for d in self.accounts:
			for fieldname in CONTROLLED_ACCOUNTS:
				account = d.get(fieldname)
				if not account:
					continue
				root_type = frappe.db.get_value("Account", account, "root_type")
				if root_type != "Expense":
					frappe.throw(
						_(
							"Row #{0}: {1} is a Control Category, so {2} must be an Expense "
							"account — {3} is {4}. Every account on a control category, the "
							"Fixed Asset and Accumulated Depreciation accounts included, is "
							"expensed (GAP-037)."
						).format(
							d.idx,
							frappe.bold(self.name),
							frappe.bold(_(self.meta.get_label(fieldname) or fieldname)),
							frappe.bold(account),
							frappe.bold(root_type or _("untyped")),
						),
						title=_("Control Category"),
					)

	def _lock_control_flag_once_used(self):
		"""Flipping the flag re-types every account on the category, and
		the assets already posted under it would be left with balances on
		the wrong side of the balance sheet with no document to move them.
		Once a submitted asset exists, the decision is made."""
		if self.is_new():
			return
		before = cint(frappe.db.get_value("Asset Category", self.name, "is_control_category"))
		if before == cint(self.get("is_control_category")):
			return
		used = frappe.db.get_value(
			"Asset", {"asset_category": self.name, "docstatus": 1}, "name"
		)
		if used:
			frappe.throw(
				_(
					"Control Category cannot be changed on {0}: submitted assets already "
					"post under it (e.g. {1}). Create a new category for the other "
					"treatment (GAP-037)."
				).format(frappe.bold(self.name), used),
				title=_("Control Category Locked"),
			)
