"""asset_enterprise regression entry point — the same shape the other
bench apps expose under <app>.tests, so the test-runner can invoke every
app the same way:

    bench --site <site> execute asset_enterprise.tests.regression.run
    bench --site <site> execute asset_enterprise.tests.regression.run --kwargs '{"phases": [3, 9]}'
    bench --site <site> execute asset_enterprise.tests.regression.run_edge
    bench --site <site> execute asset_enterprise.tests.regression.run_tc
    bench --site <site> execute asset_enterprise.tests.smoke_platform_registration.run

The phase suites live in setup/verify_phase*.py (they are the WP's
official verification and the GA-0005-01 runbook points at them). Each
phase manages its own savepoint and rolls back; nothing persists.

Unlike setup.verify_all, a phase that prints FAIL counts as a failure
here, and the run raises at the end so a caller cannot mistake a red
sweep for a green one. Remember bench execute masks the real exception
as "NameError: name 'asset_enterprise' is not defined"; on a crash call
the function through env/bin/python to see the traceback.
"""

import contextlib
import importlib
import io
import re
import sys

PHASES = tuple(range(1, 14))
_FAIL = re.compile(r"\bFAIL\b")


class RegressionFailed(AssertionError):
	pass


def _capture(fn, *args, **kwargs):
	"""Run fn, echo its output, and return (crashed, output)."""
	buf = io.StringIO()

	class Tee(io.TextIOBase):
		def write(self, s):
			buf.write(s)
			sys.__stdout__.write(s)
			return len(s)

	crashed = None
	with contextlib.redirect_stdout(Tee()), contextlib.redirect_stderr(Tee()):
		try:
			fn(*args, **kwargs)
		except Exception as e:  # a crash is a failure, not an abort of the sweep
			crashed = f"{type(e).__name__}: {e}"
			print(f"CRASHED — {crashed}")
	return crashed, buf.getvalue()


def _verdict(crashed, output):
	if crashed:
		return "CRASHED"
	if _FAIL.search(output) or re.search(r"\bERROR\b|Traceback \(most recent call last\)", output):
		return "FAIL"
	return "INCOMPLETE" if re.search(r"\bSKIP\b", output) else "PASS"


def run(phases=None, raise_on_fail=True):
	"""Run the phase suites (all thirteen by default) and summarise."""
	phases = [int(p) for p in (phases or PHASES)]
	results = {}
	for phase in phases:
		mod = importlib.import_module(f"asset_enterprise.setup.verify_phase{phase}")
		print(f"\n{'=' * 20} PHASE {phase} {'=' * 20}")
		results[phase] = _verdict(*_capture(mod.run))

	# the platform registration suite runs with every sweep (Build 0.1 §12)
	from asset_enterprise.tests import smoke_platform_registration

	print(f"\n{'=' * 20} PLATFORM REGISTRATION {'=' * 20}")
	results["platform"] = _verdict(*_capture(smoke_platform_registration.run))
	print("\nasset_enterprise regression summary")
	for phase, verdict in results.items():
		print(f"  phase {phase:>2}: {verdict}")
	red = {p: v for p, v in results.items() if v != "PASS"}
	print(f"  {len(results) - len(red)}/{len(results)} phases PASS")
	if red and raise_on_fail:
		raise RegressionFailed(f"phases not passing: {red}")
	return results


def run_phase(phase, raise_on_fail=True):
	return run([phase], raise_on_fail=raise_on_fail)


def run_edge(only=None, raise_on_fail=True):
	"""Design-derived edge suite (setup/verify_edge.py)."""
	from asset_enterprise.setup import verify_edge

	verdict = _verdict(*_capture(verify_edge.run, only=only))
	print(f"\nverify_edge: {verdict}")
	if verdict != "PASS" and raise_on_fail:
		raise RegressionFailed(f"verify_edge: {verdict}")
	return verdict


def run_tc(only=None, raise_on_fail=True):
	"""Literal GA-0005-01 §11 test-case audit (setup/verify_tc.py)."""
	from asset_enterprise.setup import verify_tc

	verdict = _verdict(*_capture(verify_tc.run, only=only))
	print(f"\nverify_tc: {verdict}")
	if verdict != "PASS" and raise_on_fail:
		raise RegressionFailed(f"verify_tc: {verdict}")
	return verdict
