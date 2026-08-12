"""Full cross-phase regression — run:
bench --site <site> execute asset_enterprise.setup.verify_all.run

Executes every phase verification in sequence. Each phase manages its
own savepoint/rollback; nothing persists.
"""

import importlib


def run():
	results = {}
	for phase in range(1, 13):
		mod = importlib.import_module(f"asset_enterprise.setup.verify_phase{phase}")
		print(f"\n{'=' * 20} PHASE {phase} {'=' * 20}")
		try:
			mod.run()
			results[phase] = "ran"
		except Exception as e:
			print(f"PHASE {phase}: CRASHED — {e}")
			results[phase] = "crashed"
	print("\nRegression sweep finished:", results)
