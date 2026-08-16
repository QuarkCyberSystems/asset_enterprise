// GA-0005-01 §9.6 — Asset Capitalization form behaviour (GAP-014).
//
// Core scopes the Target Asset picker to `asset_type = "Composite Asset"`
// AND `docstatus = 0` (draft WIP composites). That is right for Standard
// Capitalization, but it makes Capitalized Maintenance unusable: per
// VR-037 / TC-027 / TC-046 a CM target is a SUBMITTED asset of any type,
// so nothing selectable in the core list is ever a valid CM target.
//
// This re-scopes the query per transaction_type (JS is UX only — the
// server re-validates in EnterpriseAssetCapitalization._validate_cm).
frappe.ui.form.on("Asset Capitalization", {
	onload(frm) {
		set_target_asset_query(frm);
	},
	refresh(frm) {
		set_target_asset_query(frm);
	},
	transaction_type(frm) {
		frm.set_value("target_asset", null);
		set_target_asset_query(frm);
	},
	transaction_sub_type(frm) {
		frm.set_value("target_asset", null);
		set_target_asset_query(frm);
	},
});

function set_target_asset_query(frm) {
	frm.set_query("target_asset", function () {
		const ttype = frm.doc.transaction_type || "Standard Capitalization";

		if (ttype === "Capitalized Maintenance") {
			if (
				frm.doc.transaction_sub_type ===
				"Reclassification / Asset Category Transfer"
			) {
				// Reclassification takes over a DRAFT asset pre-created
				// under the new category (Phase 11 F5).
				return {
					filters: {
						docstatus: 0,
						company: frm.doc.company,
					},
				};
			}
			// VR-037: submitted target, any asset_type, not disposed.
			return {
				filters: {
					docstatus: 1,
					status: ["not in", ["Sold", "Scrapped", "Cancelled"]],
					company: frm.doc.company,
				},
			};
		}

		if (ttype === "Reversal of Capitalized Maintenance") {
			return { filters: { company: frm.doc.company } };
		}

		// Standard Capitalization — core behaviour (draft WIP composite).
		return { filters: { asset_type: "Composite Asset", docstatus: 0 } };
	});
}
