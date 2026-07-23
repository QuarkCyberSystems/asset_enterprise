import importlib
import traceback


def run():
	for mod in [
		"asset_enterprise.rounding",
		"asset_enterprise.asset_values",
		"asset_enterprise.tcc",
		"asset_enterprise.setup.test_fixtures",
		"asset_enterprise.setup.verify_phase2",
	]:
		try:
			importlib.import_module(mod)
			print(f"OK    {mod}")
		except Exception:
			print(f"FAIL  {mod}")
			traceback.print_exc()


def run2():
	from asset_enterprise.setup import verify_phase2

	try:
		verify_phase2.run()
	except Exception:
		traceback.print_exc()
