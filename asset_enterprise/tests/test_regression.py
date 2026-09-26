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

	def test_migration_runs_both_backfills_after_custom_fields(self):
		import contextlib
		from asset_enterprise.setup import install

		calls = []
		with contextlib.ExitStack() as stack:
			for name in (
				"create_custom_fields", "apply_property_setters", "_ava_property_setters",
				"_asset_status_property_setter", "_group_node_property_setter", "seed_masters",
				"seed_setting_defaults", "register_asset_accounting_dimension",
				"rebuild_asset_tree_nodes", "extend_assets_sidebar",
			):
				stack.enter_context(patch.object(install, name,
					side_effect=lambda *a, _name=name, **kw: calls.append(_name)))
			for name in ("backfill_rate_breakdown", "backfill_generation_basis"):
				stack.enter_context(patch("asset_enterprise.repair." + name,
					side_effect=lambda *, dry_run, _name=name: calls.append((_name, dry_run))))
			install.after_migrate()
		self.assertEqual(calls[0], "create_custom_fields")
		self.assertEqual(calls[-2:], [
			("backfill_rate_breakdown", 0), ("backfill_generation_basis", 0)])

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
