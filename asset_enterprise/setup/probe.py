import frappe

def run():
    results = {}
    for dt in ["Asset", "Asset Repair", "Asset Value Adjustment",
               "Asset Capitalization", "Asset Depreciation Schedule", "Asset Movement"]:
        cls = frappe.get_doc({"doctype": dt}).__class__
        results[dt] = f"{cls.__module__}.{cls.__name__}"
    # patch-guard sanity
    from asset_enterprise.overrides.patches import verify_patch_targets
    results["patch_guard"] = "OK" if verify_patch_targets() else "FAIL"
    # sap_valuation coexistence
    results["sap_valuation_loaded"] = "sap_valuation" in frappe.get_installed_apps()
    for k, v in results.items():
        print(f"{k:32s} -> {v}")
