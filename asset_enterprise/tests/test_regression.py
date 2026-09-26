"""The verifier must never turn a partial run into a green result."""
import sys
import unittest
from unittest.mock import patch

from asset_enterprise.tests.regression import _capture, _verdict


class TestRegressionCapture(unittest.TestCase):
	def test_exception_after_success_is_crash(self):
		def partial_phase():
			print("first case OK")
			raise RuntimeError("later case crashed")

		crashed, output = _capture(partial_phase)
		self.assertEqual(_verdict(crashed, output), "CRASHED")
		self.assertIn("later case crashed", output)

	def test_stderr_failure_is_not_green(self):
		crashed, output = _capture(lambda: print("FAIL on stderr", file=sys.stderr))
		self.assertEqual(_verdict(crashed, output), "FAIL")

	def test_edge_error_is_failure(self):
		self.assertEqual(_verdict(None, "E-31 ERROR ValueError: broken fixture"), "FAIL")

	def test_success_stays_green(self):
		self.assertEqual(_verdict(*_capture(lambda: print("all cases OK"))), "PASS")

	def test_reversal_policy_never_derives_current_attribution(self):
		from asset_enterprise.gl_attribution import apply_asset_cost_centre_policy

		for marker in ("is_reversal", "reversal_of"):
			with self.subTest(marker=marker), patch(
				"asset_enterprise.depreciation.enterprise_enabled",
				side_effect=AssertionError("reversal reached attribution policy"),
			):
				apply_asset_cost_centre_policy({marker: 1})

	def _migrate(self, mark, newest):
		"""Run after_migrate with every sync step and both backfills
		recorded, the migrate mark at `mark` and the newest submitted
		generation at `newest` - whatever this site actually holds."""
		import contextlib

		import frappe

		from asset_enterprise.setup import install

		calls, marks = [], []
		real_sql, real_get_global = frappe.db.sql, frappe.db.get_global

		def sql(query, *args, **kwargs):
			if "max(modified)" in query and "Asset Depreciation Schedule" in query:
				return [[newest]]
			return real_sql(query, *args, **kwargs)

		with contextlib.ExitStack() as stack:
			for name in (
				"create_custom_fields", "apply_property_setters", "_ava_property_setters",
				"_asset_status_property_setter", "_group_node_property_setter", "seed_masters",
				"seed_setting_defaults", "register_asset_accounting_dimension",
				"rebuild_asset_tree_nodes", "extend_assets_sidebar",
			):
				stack.enter_context(patch.object(install, name,
					side_effect=lambda *a, _name=name, **kw: calls.append(_name)))
			stack.enter_context(patch("asset_enterprise.repair.backfill_rate_breakdown",
				side_effect=lambda **kw: calls.append(("backfill_rate_breakdown", kw))))
			stack.enter_context(patch("asset_enterprise.repair.backfill_generation_basis",
				side_effect=lambda **kw: calls.append(("backfill_generation_basis", kw))))
			stack.enter_context(patch.object(frappe.db, "sql", side_effect=sql))
			stack.enter_context(patch.object(frappe.db, "get_global",
				side_effect=lambda key, *a, **kw: mark if key == install.GENERATION_BASIS_MARK
				else real_get_global(key, *a, **kw)))
			stack.enter_context(patch.object(frappe.db, "set_global",
				side_effect=lambda key, value, *a, **kw: marks.append((key, value))))
			install.after_migrate()
		return calls, marks

	def test_migration_runs_both_backfills_after_custom_fields(self):
		"""First migrate (no mark): both backfills run after the custom
		fields, the generation basis over everything, inside migrate's
		transaction, and the mark is written."""
		from asset_enterprise.setup import install

		calls, marks = self._migrate(mark=None, newest="2026-09-26 10:00:00")
		self.assertEqual(calls[0], "create_custom_fields")
		self.assertEqual(calls[-2:], [
			("backfill_rate_breakdown", {"dry_run": 0, "commit": False}),
			("backfill_generation_basis", {"dry_run": 0, "modified_after": None, "commit": False}),
		])
		self.assertEqual(marks, [(install.GENERATION_BASIS_MARK, "2026-09-26 10:00:00")])

	def test_migration_rescans_only_since_the_mark(self):
		"""A newer generation than the mark: only that range is scanned."""
		from asset_enterprise.setup import install

		calls, marks = self._migrate(mark="2026-09-20 00:00:00", newest="2026-09-26 10:00:00")
		self.assertEqual(calls[-1], ("backfill_generation_basis",
			{"dry_run": 0, "modified_after": "2026-09-20 00:00:00", "commit": False}))
		self.assertEqual(marks, [(install.GENERATION_BASIS_MARK, "2026-09-26 10:00:00")])

	def test_migration_skips_generation_basis_when_mark_is_current(self):
		"""Mark current (or no submitted generation at all): the rate
		breakdown still runs, the generation basis is not re-scanned and
		the mark is left alone."""
		for mark, newest in (("2026-09-26 10:00:00", "2026-09-26 10:00:00"), (None, None)):
			with self.subTest(mark=mark, newest=newest):
				calls, marks = self._migrate(mark=mark, newest=newest)
				self.assertEqual(calls[-1], ("backfill_rate_breakdown", {"dry_run": 0, "commit": False}))
				self.assertNotIn("backfill_generation_basis", [c[0] for c in calls if isinstance(c, tuple)])
				self.assertEqual(marks, [])

	def test_skipped_coverage_is_not_green(self):
		self.assertEqual(_verdict(None, "E-24 SKIP missing project fixture"), "INCOMPLETE")

	def test_gl_rollout_requires_explicit_site_activation(self):
		import frappe
		from asset_enterprise import asset_values

		asset = frappe._dict(name="TEST", docstatus=1)
		legacy = {"historical_asset_value": 99}
		ledger = {"historical_asset_value": 42}
		for enabled in (0, 1):
			with self.subTest(enabled=enabled), patch.object(
				frappe, "conf", {"asset_enterprise_gl_values_ready": enabled}
			), patch.object(frappe, "get_doc", return_value=asset), patch(
				"asset_enterprise.depreciation.enterprise_enabled", return_value=True
			), patch.object(asset_values, "fold_asset_values", return_value=legacy) as fold, patch.object(
				asset_values, "gl_asset_values", return_value=dict(ledger)
			) as gl, patch.object(asset_values, "_remaining_life_months", return_value=12):
				values = asset_values.recalculate_asset_values("TEST", save=False)
				self.assertEqual(values["historical_asset_value"], 42 if enabled else 99)
				self.assertEqual(gl.call_count, enabled)
				self.assertEqual(fold.call_count, 1 - enabled)
