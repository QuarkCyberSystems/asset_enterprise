// Selling an asset: core's picker lists assets that are Submitted,
// Partially or Fully Depreciated. A submitted asset with depreciation off
// now reads Pending Depreciation Setup or Non-Depreciable (client, 28/09,
// FA-009) and must stay sellable. Same filters as core otherwise;
// registered in setup, after core's, so this query is the one used.
frappe.ui.form.on("Sales Invoice", {
	setup(frm) {
		frm.set_query("asset", "items", function (doc, cdt, cdn) {
			const row = locals[cdt][cdn];
			return {
				filters: [
					["Asset", "item_code", "=", row.item_code],
					["Asset", "docstatus", "=", 1],
					[
						"Asset",
						"status",
						"in",
						// asset_enterprise/status.py ON_REGISTER
						[
							"Submitted",
							"Partially Depreciated",
							"Fully Depreciated",
							"Pending Depreciation Setup",
							"Non-Depreciable",
						],
					],
					["Asset", "company", "=", doc.company],
				],
			};
		});
	},
});
