import frappe
from frappe.utils import add_days, add_months, get_first_day, get_last_day, getdate, nowdate


def _dump(asset, tag):
	rows = frappe.db.sql(
		"""select ads.name, ads.status, ads.docstatus, count(ds.name) n,
		          min(ds.schedule_date) lo, max(ds.schedule_date) hi
		   from `tabAsset Depreciation Schedule` ads
		   left join `tabDepreciation Schedule` ds on ds.parent = ads.name
		   where ads.asset = %s group by ads.name order by ads.creation""",
		asset, as_dict=True)
	print(f"  [{tag}] generations:")
	for r in rows:
		print(f"     {r.name} status={r.status} ds={r.docstatus} rows={r.n} {r.lo}..{r.hi}")
	h = frappe.db.sql(
		"""select max(ds.schedule_date) from `tabDepreciation Schedule` ds
		   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		   where ads.asset = %s""", asset)[0][0]
	print(f"     horizon(all generations) = {h}")


def run():
	from asset_enterprise.setup.test_fixtures import make_test_asset, ae_company
	from asset_enterprise.depreciation import enable_depreciation, schedule_horizon_from_life
	from asset_enterprise import disposal

	frappe.db.savepoint("ch08probe")
	try:
		company = ae_company()
		r1 = make_test_asset(company, gross=36_500, submit=True)
		print("afu:", frappe.db.get_value("Asset", r1.name, "available_for_use_date"))
		enable_depreciation(
			r1.name, total_number_of_depreciations=24, frequency_of_depreciation=1,
			depreciation_start_date=get_first_day(add_months(nowdate(), 3)),
		)
		_dump(r1.name, "after enable")
		print("  horizon_from_life:", schedule_horizon_from_life(r1.name))

		disposal_date = add_days(get_first_day(nowdate()), -10)
		print("disposal_date:", disposal_date)
		disposal.scrap_asset(r1.name, scrap_date=disposal_date, scrapping_type="Damage")
		_dump(r1.name, "after scrap")
		print("  horizon_from_life:", schedule_horizon_from_life(r1.name))

		from asset_enterprise.restore import cross_period_restore
		cross_period_restore(r1.name)
		_dump(r1.name, "after restore")
	finally:
		frappe.db.rollback(save_point="ch08probe")
