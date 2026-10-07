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
3b. erpnext.assets.doctype.asset.depreciation
   .get_gl_entries_on_asset_regain
   -> the same for a Sales Invoice return, plus the asset stamp and
      the Control Category acquisition attribution (D-053)
4. erpnext.assets.doctype.asset.depreciation.validate_disposal_date
   -> tolerate the optional available-for-use date (GAP-002)
5. erpnext.assets.doctype.asset.depreciation
   .get_value_after_depreciation_on_disposal_date
6. erpnext.assets.doctype.asset.asset.get_asset_value_after_depreciation
   -> both derive the value from the ledger, not the counter (GAP-006)
7. erpnext.assets.report.fixed_asset_register.fixed_asset_register.execute
   -> group nodes hidden, replacement-chain columns (GAP-036)

Not here any more (qcs_platform Build 0.2 step 4, N-4 RULED (a)): the
wraps of methods on SHARED core classes - PurchaseReceipt / PurchaseInvoice
`get_gl_entries` (asset-leg attribution), BuyingController
`update_fixed_asset` (never delete a cancelled receipt's assets) and
JournalEntry `update_journal_entry_link_on_depr_schedule` (no guessed
schedule row). Those classes' overrides are the platform's; each rule is
now a function this app declares on the platform's socket hook of the same
purpose (`asset_enterprise.platform_sockets`, hooks.py), and the platform
refuses this app's dependent postings while a socket is unhealthy. What
stays here is asset-family: module functions of erpnext's asset modules
(and the Fixed Asset Register report) with this app as their one owner.

Phase 0 ships verification only: on app boot we assert every target
still exists with a compatible signature, so a bench update that moves
or renames a target fails loudly at startup instead of silently
skipping our behavior.
"""

import inspect

import frappe
from frappe.utils import flt, getdate

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
	("erpnext.assets.doctype.asset.depreciation", "get_gl_entries_on_asset_regain", 1),
	("erpnext.assets.doctype.asset.depreciation", "validate_disposal_date", 3),
	(
		"erpnext.assets.doctype.asset.depreciation",
		"get_value_after_depreciation_on_disposal_date",
		2,
	),
	("erpnext.assets.doctype.asset.asset", "get_asset_value_after_depreciation", 1),
	# GAP-036 / replacement chain: the Fixed Asset Register's execute is
	# wrapped too (group nodes hidden, chain columns added), so a core move
	# or rename must fail the migrate, not silently drop both (D-052 (iv)).
	("erpnext.assets.report.fixed_asset_register.fixed_asset_register", "execute", 1),
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
	"get_gl_entries_on_asset_regain",
	"validate_disposal_date",
	"get_value_after_depreciation_on_disposal_date",
	"get_asset_value_after_depreciation",
	"execute",  # fixed_asset_register.execute
}

# Class-method targets: (module, class, attr, min positional params).
# verify_patch_targets checks these the same way, resolving through the
# class instead of the module, and requires each to carry this app's
# wrapper marker. EMPTY since qcs_platform Build 0.2 step 4: the four
# shared-class wraps it listed (BuyingController.update_fixed_asset,
# JournalEntry.update_journal_entry_link_on_depr_schedule,
# PurchaseReceipt / PurchaseInvoice.get_gl_entries) are platform sockets
# now, and a row left here would fail every migrate with "not wrapped" (the
# platform, for its part, refuses the marker while it owns the method).
# Kept for an asset-family class wrap, should one ever be needed.
CLASS_PATCH_TARGETS = []

# Core methods an `override_doctype_class` subclass overrides BY NAME to
# post GL (one credit per consumed cost line): (module, class, attr, min
# positional params, overriding class path). Not wrapped - the subclass
# replaces them - so verify_patch_targets checks that core still defines
# them with that signature and that the site's controller is the subclass
# (chief r5 S-4, cross-app r3): a rename upstream would otherwise silently
# bring back core's merged, line-less credit.
CLASS_OVERRIDE_TARGETS = [
	("erpnext.assets.doctype.asset_repair.asset_repair", "AssetRepair", "get_gl_entries_for_repair_cost", 3,
		"asset_enterprise.overrides.asset_repair.EnterpriseAssetRepair"),
	("erpnext.assets.doctype.asset_repair.asset_repair", "AssetRepair", "get_gl_entries_for_consumed_items", 3,
		"asset_enterprise.overrides.asset_repair.EnterpriseAssetRepair"),
	("erpnext.assets.doctype.asset_capitalization.asset_capitalization", "AssetCapitalization",
		"get_gl_entries_for_consumed_service_items", 5,
		"asset_enterprise.overrides.asset_capitalization.EnterpriseAssetCapitalization"),
	("erpnext.assets.doctype.asset_capitalization.asset_capitalization", "AssetCapitalization", "before_submit", 1,
		"asset_enterprise.overrides.asset_capitalization.EnterpriseAssetCapitalization"),
	# CH-26 (merged source stays Disposed) and CH-44 (in-service-date floor)
	("erpnext.assets.doctype.asset.asset", "Asset", "get_status", 1,
		"asset_enterprise.overrides.asset.EnterpriseAsset"),
	# FA-009: the two not-depreciating statuses cancel as "Submitted"
	("erpnext.assets.doctype.asset.asset", "Asset", "validate_cancellation", 1,
		"asset_enterprise.overrides.asset.EnterpriseAsset"),
	("erpnext.assets.doctype.asset.asset", "Asset", "validate_depreciation_start_date", 2,
		"asset_enterprise.overrides.asset.EnterpriseAsset"),
]

# (attr, original callable, wrapper callable) — filled by _rebind().
_WRAPPED = []


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
	wrapper up from the defining module by themselves. The sweep is the
	platform's one `core_patches.rebind()` (RULES §7 "by-name importer
	rebinding": ownership = platform, adopted here at this file's first
	touch after it was built - qcs_platform Build 0.2 step 4). It sweeps
	erpnext's modules, which hold every by-name importer of these targets
	(an AST scan finds none in this app), and records the rebind so the
	platform's `check()` reports a stale one too.
	"""
	from qcs_platform.core_patches import rebind

	_WRAPPED.append((attr, original, wrapper))
	rebind(attr, original, wrapper)


def stale_bindings():
	"""Modules still holding an original this app rebound — must always be
	empty (the platform's sweep, narrowed to this app's rebinds)."""
	from qcs_platform.core_patches import stale_bindings as platform_stale_bindings

	mine = {attr for attr, _original, _wrapper in _WRAPPED}
	return [ref for ref in platform_stale_bindings() if ref.rsplit(".", 1)[-1] in mine]


def _carry_control_assets_at_zero(columns, data):
	"""FA-735 (client, 06/10): a Control Category asset is expensed on
	purchase. The register lists it for control at its cost but carries it
	at 0, saying so, so the register agrees with the balance sheet."""
	control_categories = set(frappe.get_all("Asset Category", {"is_control_category": 1}, pluck="name"))
	if not control_categories:
		return columns, data
	columns = list(columns) + [{"label": frappe._("Carried As"), "fieldname": "carried_as",
		"fieldtype": "Data", "width": 160}]
	for row in data:
		if isinstance(row, dict) and row.get("asset_category") in control_categories:
			row["asset_value"] = 0
			row["carried_as"] = frappe._("Expensed on purchase")
	return columns, data


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

	problems.extend(_class_override_problems())

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


def _class_override_problems():
	"""CLASS_OVERRIDE_TARGETS: core still defines each method with its
	signature, the subclass still overrides it, and the site's controller
	for the doctype is the subclass."""
	problems = []
	for module_path, clsname, attr, min_params, override_path in CLASS_OVERRIDE_TARGETS:
		try:
			core_cls = getattr(frappe.get_module(module_path), clsname, None)
		except ImportError:
			problems.append(f"module missing: {module_path}")
			continue
		fn = getattr(core_cls, attr, None) if core_cls else None
		if fn is None:
			problems.append(f"method missing: {module_path}.{clsname}.{attr} (overridden by {override_path})")
			continue
		params = [p for p in inspect.signature(fn).parameters.values()
			if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
		if len(params) < min_params:
			problems.append(
				f"signature changed: {module_path}.{clsname}.{attr} has {len(params)} "
				f"positional params, expected >= {min_params}"
			)
		override_module, override_cls = override_path.rsplit(".", 1)
		sub = getattr(frappe.get_module(override_module), override_cls, None)
		if sub is None or attr not in vars(sub):
			problems.append(f"override missing: {override_path}.{attr}")
			continue
		doctype = frappe.unscrub(module_path.rsplit(".", 1)[1])
		try:
			from frappe.model.base_document import get_controller

			controller = get_controller(doctype)
		except Exception:
			controller = None
		if controller is not None and not issubclass(controller, sub):
			problems.append(f"controller for {doctype} is {controller.__module__}.{controller.__name__}, not {override_path}")
	return problems


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

	# GAP-006: asset values are DERIVED from the ledger; the core
	# `value_after_depreciation` counter is bookkeeping we keep in step,
	# never the source of truth. Core returns that counter outright for an
	# asset that does not depreciate, and for a Composite Component:
	#
	#     if not asset_doc.calculate_depreciation:
	#         return flt(asset_doc.value_after_depreciation)
	#
	# Nothing maintains the counter on such an asset, so it stays 0 — the
	# Consumed Assets grid offered a 35,000 asset as 0.00 for approval
	# (client, 25/08, ACC-ASS-2026-00192). Where core reads the counter we
	# answer from the ledger instead; where it computes a value AT a date
	# from a temporary schedule, that is date-specific work and stands.
	core_value_on_disposal = core_depr.get_value_after_depreciation_on_disposal_date

	@frappe.whitelist()
	def get_value_after_depreciation_on_disposal_date(
		asset, disposal_date, finance_book=None
	):
		"""MUST stay whitelisted — the capitalization form calls core's
		dotted path and resolves the attribute to THIS function."""
		from asset_enterprise.depreciation import enterprise_enabled

		if not enterprise_enabled():
			return core_value_on_disposal(asset, disposal_date, finance_book)

		asset_doc = frappe.get_doc("Asset", asset)
		counter_branch = (
			asset_doc.asset_type == "Composite Component"
			or not asset_doc.calculate_depreciation
		)
		if not counter_branch:
			return core_value_on_disposal(asset, disposal_date, finance_book)

		# core validates before returning; keep that, through the wrapped
		# guard so a missing date is tolerated (patch #4).
		core_depr.validate_disposal_date(
			asset_doc.purchase_date
			if asset_doc.asset_type == "Composite Component"
			else asset_doc.available_for_use_date,
			getdate(disposal_date),
			"purchase" if asset_doc.asset_type == "Composite Component" else "available for use",
		)

		from asset_enterprise.asset_values import recalculate_asset_values

		return flt(recalculate_asset_values(asset, save=False)["net_book_value"])

	get_value_after_depreciation_on_disposal_date._asset_enterprise_wrapper = True
	core_depr.get_value_after_depreciation_on_disposal_date = (
		get_value_after_depreciation_on_disposal_date
	)
	_rebind(
		"get_value_after_depreciation_on_disposal_date",
		core_value_on_disposal,
		get_value_after_depreciation_on_disposal_date,
	)

	# The SAME counter read, one function along. The capitalization form
	# fills two columns per consumed row — Asset Value from the function
	# above, Current Asset Value from this one — and both take the
	# `value_after_depreciation` shortcut for an asset that does not
	# depreciate:
	#
	#     if not asset.calculate_depreciation:
	#         return flt(asset.value_after_depreciation)
	#
	# Fixing only the first left the row half right: 35,000 in one column
	# and 0.00 in the other (client, 25/08). set_asset_values() writes
	# both onto the submitted document, so this is stored, not cosmetic.
	import erpnext.assets.doctype.asset.asset as core_asset

	core_asset_value_after_depr = core_asset.get_asset_value_after_depreciation

	@frappe.whitelist()
	def get_asset_value_after_depreciation(asset_name, finance_book=None):
		"""MUST stay whitelisted — core exposes it and forms call it."""
		from asset_enterprise.depreciation import enterprise_enabled

		if not enterprise_enabled():
			return core_asset_value_after_depr(asset_name, finance_book)

		asset_doc = frappe.get_doc("Asset", asset_name)
		if asset_doc.calculate_depreciation:
			return core_asset_value_after_depr(asset_name, finance_book)

		from asset_enterprise.asset_values import recalculate_asset_values

		return flt(recalculate_asset_values(asset_name, save=False)["net_book_value"])

	get_asset_value_after_depreciation._asset_enterprise_wrapper = True
	core_asset.get_asset_value_after_depreciation = get_asset_value_after_depreciation
	_rebind(
		"get_asset_value_after_depreciation",
		core_asset_value_after_depr,
		get_asset_value_after_depreciation,
	)

	# GAP-004.4 (never delete a cancelled receipt's assets), GAP-031 / §4.6
	# (no guessed depreciation schedule row) and GAP-006 / §5.1 audit C1
	# (asset-leg attribution of PR / PI GL) are qcs_platform sockets since
	# Build 0.2 step 4: see asset_enterprise.platform_sockets.

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
		from asset_enterprise.asset_enterprise.report.replacement_chain.replacement_chain import add_register_chain

		columns, data = add_register_chain(columns, data)
		columns, data = _carry_control_assets_at_zero(columns, data)
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

	# Patch #3b — the regain map a Sales Invoice RETURN posts: the same
	# account swap, the `asset` stamp and the D-053 acquisition attribution
	# as the sale it undoes (review 2026-09-26 B-1 (iii)). sales_invoice.py
	# imports it at its own import time, hence the rebind.
	from asset_enterprise.disposal import get_gl_entries_on_asset_regain_wrapper

	core_regain_gl = core_depr.get_gl_entries_on_asset_regain
	regain_gl = get_gl_entries_on_asset_regain_wrapper(core_regain_gl)
	core_depr.get_gl_entries_on_asset_regain = regain_gl
	_rebind("get_gl_entries_on_asset_regain", core_regain_gl, regain_gl)
	_PATCHED = True
