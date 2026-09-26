"""Client clarification 2026-09-22: Control Category depreciation is a one-day charge."""
import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate


def applies(asset):
	from asset_enterprise.depreciation import enterprise_enabled
	from asset_enterprise.overrides.asset_category import is_control_category
	return enterprise_enabled() and is_control_category(asset.asset_category)


def configure_asset(asset):
	if not applies(asset) or not asset.calculate_depreciation:
		return
	validate_accounts(asset)
	for book in asset.get("finance_books") or []:
		book.depreciation_method = "Straight Line"
		book.total_number_of_depreciations = 1
		book.frequency_of_depreciation = 1
		book.expected_value_after_useful_life = 0
		book.daily_prorata_based = 0


def normalize_schedule(schedule):
	asset = frappe.get_doc("Asset", schedule.asset)
	if not applies(asset):
		return
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.depreciation import stamp_generation_basis
	from asset_enterprise.rounding import fa_module_round

	validate_accounts(asset)
	posted = [row for row in schedule.depreciation_schedule if row.journal_entry]
	unposted = [row for row in schedule.depreciation_schedule if not row.journal_entry]
	# Submitted history is never edited; newly constructed generations may
	# carry posted rows and one charge for the remaining current balance.
	if schedule.docstatus == 1 and not schedule.is_new():
		return
	book = next((b for b in asset.finance_books if b.finance_book == schedule.finance_book), None)
	date = getdate((unposted[0].schedule_date if unposted else None)
		or (book and book.depreciation_start_date) or asset.available_for_use_date)
	if not posted and not schedule.get("repriced_from") and book and book.depreciation_start_date:
		date = getdate(book.depreciation_start_date)
	values = recalculate_asset_values(asset.name, save=False)
	if frappe.flags.get("ae_asset_submission") == asset.name:
		# Core submits its schedule before this transaction posts acquisition
		# GL. Use the creation preview only inside that explicit lifecycle.
		hav = flt(asset.net_purchase_amount) + flt(asset.additional_asset_cost)
		accum = flt(asset.opening_accumulated_depreciation)
		values = {"historical_asset_value": hav, "accumulated_depreciation_value": accum,
			"net_book_value": hav - accum}
	amount = fa_module_round(values["net_book_value"], asset.company)
	schedule.set("depreciation_schedule", [])
	for row in posted:
		schedule.append("depreciation_schedule", row)
	if amount > 0:
		schedule.append("depreciation_schedule", {
			"schedule_date": date, "period_end_date": date, "days_in_period": 1,
			"daily_rate": amount, "depreciation_amount": amount,
			"accumulated_depreciation_amount": fa_module_round(
				flt(values["accumulated_depreciation_value"]) + amount, asset.company),
		})
	schedule.expected_value_after_useful_life = 0
	schedule.total_number_of_depreciations = len(posted) + (1 if amount > 0 else 0)
	schedule.frequency_of_depreciation = 1
	schedule.depreciation_method = "Straight Line"
	schedule.daily_prorata_based = 0
	stamp_generation_basis(schedule, asset.name, asset.company, add_days(date, -1),
		date, date, max(0, amount), 0)
	schedule.basis_hav = values["historical_asset_value"]
	schedule.basis_accumulated = values["accumulated_depreciation_value"]
	schedule.basis_nbv = values["net_book_value"]


def validate_posting(asset, row):
	if not applies(asset):
		return
	validate_accounts(asset)
	from asset_enterprise.asset_values import recalculate_asset_values
	from asset_enterprise.rounding import fa_module_round
	amount = fa_module_round(recalculate_asset_values(asset.name, save=False)["net_book_value"], asset.company)
	if int(row.get("days_in_period") or 0) != 1 or abs(flt(row.depreciation_amount) - amount) > 0.005:
		frappe.throw(_("Control Category depreciation must charge the full remaining value in one day. Regenerate the unposted schedule before posting."))


def validate_accounts(asset):
	accounts = frappe.db.get_value("Asset Category Account", {
		"parent": asset.asset_category, "company_name": asset.company},
		["fixed_asset_account", "accumulated_depreciation_account", "depreciation_expense_account"], as_dict=True)
	if accounts and len(set(accounts.values())) != 3:
		frappe.throw(_("Control Category cost, accumulated depreciation and depreciation expense must use distinct accounts so the full charge reduces the tracked balance to zero."))
