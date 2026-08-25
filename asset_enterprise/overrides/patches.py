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
4. erpnext.assets.doctype.asset.depreciation.validate_disposal_date
   -> tolerate the optional available-for-use date (GAP-002)

Phase 0 ships verification only: on app boot we assert every target
still exists with a compatible signature, so a bench update that moves
or renames a target fails loudly at startup instead of silently
skipping our behavior.
"""

import inspect
import sys

import frappe
from frappe.utils import getdate

# (dotted module, attribute, minimum positional params we rely on)
PATCH_TARGETS = [
	("erpnext.assets.doctype.asset.depreciation", "post_depreciation_entries", 0),
	(
		"erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule",
		"reschedule_depreciation",
		1,
	),
	("erpnext.assets.doctype.asset.depreciation", "make_depreciation_entry", 1),
	("erpnext.assets.doctype.asset.depreciation", "get_gl_entries_on_asset_disposal", 1),
	("erpnext.assets.doctype.asset.depreciation", "validate_disposal_date", 3),
	# §2.2 override_whitelisted_methods targets — verified here too so a
	# rename surfaces at boot, not at first user click.
	("erpnext.assets.doctype.asset.depreciation", "restore_asset", 1),
	("erpnext.assets.doctype.asset.depreciation", "scrap_asset", 1),
]

# Attributes apply_patches() actually wraps — the subset of PATCH_TARGETS
# that must resolve to OUR function everywhere, not merely exist.
WRAPPED_ATTRS = {
	"post_depreciation_entries",
	"reschedule_depreciation",
	"make_depreciation_entry",
	"get_gl_entries_on_asset_disposal",
	"validate_disposal_date",
}

# Class-method targets: (module, class, attr, min positional params).
# verify_patch_targets checks these the same way, resolving through the
# class instead of the module.
CLASS_PATCH_TARGETS = [
	("erpnext.controllers.buying_controller", "BuyingController", "update_fixed_asset", 2),
]

# (attr, original callable, wrapper callable) — filled by _rebind().
_WRAPPED = []


def _own_modules():
	"""Every erpnext / asset_enterprise module currently imported."""
	for name, module in list(sys.modules.items()):
		if module is None or not name.startswith(("erpnext", "asset_enterprise")):
			continue
		yield name, module


def _rebind(attr, original, wrapper):
	"""Point every module that already imported `attr` at the wrapper.

	`from x import y` COPIES the reference. erpnext's depreciation.py
	imports reschedule_depreciation at its own import time, so patching
	the defining module alone left depreciate_asset calling core's
	version — which ends in current_schedule.cancel(). That is how a
	merged source asset ended up with a Cancelled depreciation schedule
	despite the supersede-never-cancel rule (§4.8 / GAP-031; client,
	ACC-ASS-2026-00106 and ACC-ASS-2026-00101). The same hazard applies
	to sales_invoice.py and asset_capitalization.py, which both import
	get_gl_entries_on_asset_disposal at import time.

	Patch the definition AND every copy; modules imported later pick the
	wrapper up from the defining module by themselves.
	"""
	_WRAPPED.append((attr, original, wrapper))
	for _name, module in _own_modules():
		try:
			if getattr(module, attr, None) is original:
				setattr(module, attr, wrapper)
		except Exception:
			continue


def stale_bindings():
	"""Modules still holding an unwrapped original — must always be empty."""
	stale = []
	for attr, original, _wrapper in _WRAPPED:
		for name, module in _own_modules():
			try:
				if getattr(module, attr, None) is original:
					stale.append(f"{name}.{attr}")
			except Exception:
				continue
	return stale


def verify_patch_targets():
	"""Assert every override target still exists post bench-update, AND
	that nothing anywhere still reaches the unwrapped original.

	Called from app boot (hooks: extend_bootinfo is too late for workers,
	so we invoke from __init__ import side-effect guarded by frappe init).
	"""
	apply_patches()  # idempotent; a fresh process may not have run it yet
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
		if attr in WRAPPED_ATTRS and not getattr(fn, "_asset_enterprise_wrapper", False):
			problems.append(f"not wrapped: {module_path}.{attr} is still core's function")

	for module_path, clsname, attr, min_params in CLASS_PATCH_TARGETS:
		try:
			module = frappe.get_module(module_path)
		except ImportError:
			problems.append(f"module missing: {module_path}")
			continue
		cls = getattr(module, clsname, None)
		fn = getattr(cls, attr, None) if cls else None
		if fn is None:
			problems.append(f"method missing: {module_path}.{clsname}.{attr}")
			continue
		params = [
			p
			for p in inspect.signature(fn).parameters.values()
			if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
		]
		if len(params) < min_params:
			problems.append(
				f"signature changed: {module_path}.{clsname}.{attr} has {len(params)} "
				f"positional params, expected >= {min_params}"
			)
		if not getattr(fn, "_asset_enterprise_wrapper", False):
			problems.append(f"not wrapped: {module_path}.{clsname}.{attr} is still core's method")

	# Existing-and-wrapped at the DEFINING module is not enough: a module
	# that did `from x import y` before we patched keeps calling core.
	problems.extend(
		f"stale binding: {ref} still resolves to core's unwrapped function"
		for ref in stale_bindings()
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

		# Ordering guard: core triggers this from inside on_submit BEFORE
		# our TCC records the value change, so a regeneration here would
		# re-spread the PRE-adjustment NBV. The controller sets this flag
		# and regenerates itself once the Financial Treatment exists.
		if frappe.flags.get("ae_defer_reschedule") == asset_doc.name:
			return None

		has_active_schedule = frappe.db.exists(
			"Asset Depreciation Schedule",
			{"asset": asset_doc.name, "status": "Active", "docstatus": 1},
		)
		if enterprise_enabled():
			if has_active_schedule:
				# GAP-031: supersede-not-cancel. On a disposal the new
				# schedule TERMINATES at the disposal date, leaving the
				# mid-period proration as its final row — the row core
				# only ever produced by cancelling and rebuilding.
				return supersede_and_regenerate(
					asset_doc.name, disposal_date=disposal_date, reason=notes
				)
			# No Active schedule left to reshape — do NOTHING. Falling
			# through to core here was cancelling the asset's superseded
			# schedule (core's reschedule_depreciation ends in
			# current_schedule.cancel()), which is how a merged source
			# ended up with a Cancelled schedule despite the
			# supersede-never-cancel rule (client, ACC-ASS-2026-00106).
			return None
		return core_resched(asset_doc, notes, disposal_date=disposal_date)

	core_make_entry = core_depr.make_depreciation_entry

	@frappe.whitelist()
	def make_depreciation_entry(
		depr_schedule_name, date=None, sch_start_idx=None, sch_end_idx=None,
		accounting_dimensions=None,
	):
		# Core's make_depreciation_entry_on_disposal passes the RAW
		# frappe.get_all result — a list of dicts — instead of a name.
		# Left as-is, the posting matched nothing and every full scrap
		# silently skipped its proration rows (client, 19/08:
		# ACC-ASS-2026-00139 — 14 days of April never posted).
		if isinstance(depr_schedule_name, (list, tuple)):
			depr_schedule_name = depr_schedule_name[0] if depr_schedule_name else None
		if isinstance(depr_schedule_name, dict):
			depr_schedule_name = depr_schedule_name.get("name")
		if not depr_schedule_name:
			return None
		"""The schedule form's own button. Core posts a plain JE that
		skips the §4.7 prior-year split and the Financial Treatment —
		route it through the same engine the scheduler uses.

		MUST stay whitelisted: the button calls core's dotted path, frappe
		resolves the attribute to THIS function, and an undecorated
		replacement makes the button fail with "Method Not Allowed"."""
		from asset_enterprise.depreciation import enterprise_enabled, post_schedule_entries

		if enterprise_enabled():
			frappe.has_permission("Journal Entry", throw=True)
			return post_schedule_entries(
				depr_schedule_name, date, sch_start_idx=sch_start_idx, sch_end_idx=sch_end_idx
			)
		return core_make_entry(
			depr_schedule_name, date, sch_start_idx, sch_end_idx, accounting_dimensions
		)

	make_depreciation_entry._asset_enterprise_wrapper = True
	post_depreciation_entries._asset_enterprise_wrapper = True
	reschedule_depreciation._asset_enterprise_wrapper = True

	# Patch the defining module AND rebind every already-imported copy.
	core_depr.make_depreciation_entry = make_depreciation_entry
	_rebind("make_depreciation_entry", core_make_entry, make_depreciation_entry)
	core_depr.post_depreciation_entries = post_depreciation_entries
	_rebind("post_depreciation_entries", core_post, post_depreciation_entries)
	core_ads.reschedule_depreciation = reschedule_depreciation
	_rebind("reschedule_depreciation", core_resched, reschedule_depreciation)

	# GAP-002 / TC-003: an asset that does not depreciate may be submitted
	# with NO available-for-use date — a passing test case of the signed
	# design. Core's disposal-date guard compares that field RAW against a
	# getdate()'d disposal date:
	#
	#     validate_disposal_date(asset_doc.available_for_use_date, ...)
	#     if reference_date > disposal_date:
	#
	# so a missing date raises TypeError: '>' not supported between
	# instances of 'NoneType' and 'datetime.date' before the guard can
	# decide anything. It surfaced as a Server Error the moment such an
	# asset was picked in a capitalization's Consumed Assets grid, which
	# calls get_value_after_depreciation_on_disposal_date (client, 25/08,
	# ACC-ASS-2026-00192). Nothing to compare means nothing to refuse.
	core_validate_disposal_date = core_depr.validate_disposal_date

	def validate_disposal_date(reference_date, disposal_date, label):
		from asset_enterprise.depreciation import enterprise_enabled

		if not enterprise_enabled():
			return core_validate_disposal_date(reference_date, disposal_date, label)
		if not reference_date:
			return
		return core_validate_disposal_date(
			getdate(reference_date), getdate(disposal_date), label
		)

	validate_disposal_date._asset_enterprise_wrapper = True
	core_depr.validate_disposal_date = validate_disposal_date
	_rebind("validate_disposal_date", core_validate_disposal_date, validate_disposal_date)

	# GAP-004.4: cancelling a receipt must REVERSE the assets it created,
	# never destroy them. Core does the opposite —
	# buying_controller.update_fixed_asset(delete_asset=True) calls
	# frappe.delete_doc("Asset", ..., force=1) for every auto-created
	# asset, wiping the record and its movements whatever its docstatus,
	# so the reversal our pr_before_cancel had just performed vanished
	# along with the asset (client, 24/08). Assets already cancelled are
	# skipped by core's own loop once deletion is off.
	import erpnext.controllers.buying_controller as core_buying

	core_update_fixed_asset = core_buying.BuyingController.update_fixed_asset

	def update_fixed_asset(self, field, delete_asset=False):
		from asset_enterprise.depreciation import enterprise_enabled

		if delete_asset and enterprise_enabled():
			delete_asset = False
		return core_update_fixed_asset(self, field, delete_asset)

	update_fixed_asset._asset_enterprise_wrapper = True
	# A class attribute — every subclass resolves it at call time, so
	# there is no already-imported copy to rebind.
	core_buying.BuyingController.update_fixed_asset = update_fixed_asset

	# GAP-036: a grouping asset is a structural container with no value —
	# it must not appear as a zero-value line in the Fixed Asset Register.
	import erpnext.assets.report.fixed_asset_register.fixed_asset_register as core_far

	core_far_execute = core_far.execute

	def fixed_asset_register(filters=None):
		from asset_enterprise.depreciation import enterprise_enabled

		result = core_far_execute(filters)
		if not enterprise_enabled() or not result:
			return result
		columns, data = result[0], result[1]
		group_nodes = set(
			frappe.get_all("Asset", filters={"is_group_node": 1}, pluck="name")
		)
		if group_nodes:
			data = [
				row
				for row in data
				if (row.get("asset_id") if isinstance(row, dict) else None) not in group_nodes
			]
		return (columns, data, *result[2:])

	fixed_asset_register._asset_enterprise_wrapper = True
	core_far.execute = fixed_asset_register
	_rebind("execute", core_far_execute, fixed_asset_register)

	# Phase 6 — patch #3: Scrape Type / ACA-override routing for the
	# loss account in core disposal GL (e.g. sale via Sales Invoice).
	from asset_enterprise.disposal import get_gl_entries_on_asset_disposal_wrapper

	core_disposal_gl = core_depr.get_gl_entries_on_asset_disposal
	disposal_gl = get_gl_entries_on_asset_disposal_wrapper(core_disposal_gl)
	core_depr.get_gl_entries_on_asset_disposal = disposal_gl
	# sales_invoice.py and asset_capitalization.py import this at their own
	# import time — without the rebind, an asset SOLD through a Sales
	# Invoice kept core's loss account instead of the §3.5 chain result.
	_rebind("get_gl_entries_on_asset_disposal", core_disposal_gl, disposal_gl)
	_PATCHED = True
