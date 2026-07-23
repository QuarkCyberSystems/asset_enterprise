__version__ = "0.0.1"

# Apply erpnext monkeypatches at import time. This package is imported
# in every frappe process (web, worker, scheduler) when hooks resolve,
# so the wrappers are in place before the core scheduler can call the
# originals. Wrappers no-op unless Asset Settings enables the app, and
# fail open if erpnext is not importable (fresh installs / app sync).
try:
	from asset_enterprise.overrides.patches import apply_patches

	apply_patches()
except Exception:
	pass
