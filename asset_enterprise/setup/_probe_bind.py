import frappe


def run():
	from asset_enterprise.overrides.patches import _WRAPPED, stale_bindings, verify_patch_targets
	import erpnext.assets.doctype.asset.depreciation as d
	import erpnext.accounts.doctype.sales_invoice.sales_invoice as si

	print("registry:", [a for a, _o, _w in _WRAPPED])
	print("depreciation.reschedule_depreciation wrapped:",
	      getattr(d.reschedule_depreciation, "_asset_enterprise_wrapper", False))
	print("sales_invoice.get_gl_entries_on_asset_disposal wrapped:",
	      getattr(si.get_gl_entries_on_asset_disposal, "_asset_enterprise_wrapper", False))
	print("stale now:", stale_bindings())

	# Detector must FIRE when a module is put back onto the original.
	attr, original, wrapper = next(w for w in _WRAPPED if w[0] == "reschedule_depreciation")
	d.reschedule_depreciation = original
	print("stale after sabotage:", stale_bindings())
	try:
		verify_patch_targets()
		print("DETECTOR FAILED — verify_patch_targets passed with a stale binding")
	except Exception as e:
		print("DETECTOR OK — verify_patch_targets threw:", str(e).splitlines()[-1][:120])
	d.reschedule_depreciation = wrapper
	print("restored, stale:", stale_bindings())
