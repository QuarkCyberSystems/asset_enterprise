"""Monkeypatch registry for asset_enterprise.

The ONLY file in the app with bench-upgrade risk. Three module-level
functions in erpnext cannot be reached via override_doctype_class /
override_whitelisted_methods and are wrapped here (see build plan §2.3):

1. erpnext.assets.doctype.asset.depreciation.post_depreciation_entries
   -> daily-rate engine (Phase 3)
2. erpnext.assets.doctype.asset_depreciation_schedule
   .asset_depreciation_schedule.reschedule_depreciation
   -> supersede-not-cancel (Phase 3)
3. erpnext.assets.doctype.asset.depreciation
   .get_gl_entries_on_asset_disposal
   -> Scrape Type account resolution chain (Phase 6)

Phase 0 ships verification only: on app boot we assert every target
still exists with a compatible signature, so a bench update that moves
or renames a target fails loudly at startup instead of silently
skipping our behavior.
"""

import inspect

import frappe

# (dotted module, attribute, minimum positional params we rely on)
PATCH_TARGETS = [
	("erpnext.assets.doctype.asset.depreciation", "post_depreciation_entries", 0),
	(
		"erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule",
		"reschedule_depreciation",
		1,
	),
	("erpnext.assets.doctype.asset.depreciation", "get_gl_entries_on_asset_disposal", 1),
	# §2.2 override_whitelisted_methods targets — verified here too so a
	# rename surfaces at boot, not at first user click.
	("erpnext.assets.doctype.asset.depreciation", "restore_asset", 1),
	("erpnext.assets.doctype.asset.depreciation", "scrap_asset", 1),
]


def verify_patch_targets():
	"""Assert every override target still exists post bench-update.

	Called from app boot (hooks: extend_bootinfo is too late for workers,
	so we invoke from __init__ import side-effect guarded by frappe init).
	"""
	problems = []
	for module_path, attr, min_params in PATCH_TARGETS:
		try:
			module = frappe.get_module(module_path)
		except ImportError:
			problems.append(f"module missing: {module_path}")
			continue
		fn = getattr(module, attr, None)
		if fn is None:
			problems.append(f"function missing: {module_path}.{attr}")
			continue
		params = [
			p
			for p in inspect.signature(fn).parameters.values()
			if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
		]
		if len(params) < min_params:
			problems.append(
				f"signature changed: {module_path}.{attr} has {len(params)} positional params, expected >= {min_params}"
			)
	if problems:
		frappe.throw(
			"asset_enterprise: erpnext upgrade moved override targets:\n- " + "\n- ".join(problems)
		)
	return True


_PATCHED = False


def apply_patches():
	"""Apply the wrap-and-delegate patches (idempotent, import-time safe).

	Wrappers check the Asset Settings master switch AT CALL TIME, so a
	disabled site behaves exactly like stock erpnext.

	Phase 3: post_depreciation_entries + reschedule_depreciation (below)
	Phase 6: get_gl_entries_on_asset_disposal (pending)
	"""
	global _PATCHED
	if _PATCHED:
		return
	import erpnext.assets.doctype.asset.depreciation as core_depr
	import erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule as core_ads

	core_post = core_depr.post_depreciation_entries
	core_resched = core_ads.reschedule_depreciation

	def post_depreciation_entries(date=None):
		from asset_enterprise.depreciation import enterprise_enabled
		from asset_enterprise.depreciation import post_depreciation_entries as ours

		if enterprise_enabled():
			return ours(date)
		return core_post(date)

	def reschedule_depreciation(asset_doc, notes, disposal_date=None):
		from asset_enterprise.depreciation import enterprise_enabled, supersede_and_regenerate

		if enterprise_enabled():
			# GAP-031: supersede-not-cancel. as_of = disposal/reschedule date.
			return supersede_and_regenerate(
				asset_doc.name, as_of_date=disposal_date, reason=notes
			)
		return core_resched(asset_doc, notes, disposal_date=disposal_date)

	post_depreciation_entries._asset_enterprise_wrapper = True
	reschedule_depreciation._asset_enterprise_wrapper = True

	core_depr.post_depreciation_entries = post_depreciation_entries
	core_ads.reschedule_depreciation = reschedule_depreciation

	# Phase 6 — patch #3: Scrape Type / ACA-override routing for the
	# loss account in core disposal GL (e.g. sale via Sales Invoice).
	from asset_enterprise.disposal import get_gl_entries_on_asset_disposal_wrapper

	core_depr.get_gl_entries_on_asset_disposal = get_gl_entries_on_asset_disposal_wrapper(
		core_depr.get_gl_entries_on_asset_disposal
	)
	_PATCHED = True
