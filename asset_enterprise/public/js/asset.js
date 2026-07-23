// GA-0005-01 v2.14 — Asset form extensions (§9.6). JS is UX-only;
// every action calls a whitelisted backend that re-validates (C126).
frappe.ui.form.on("Asset", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;

		// Partial Scrap (GAP-018)
		if (!["Scrapped", "Sold", "Capitalized", "Cancelled"].includes(frm.doc.status)) {
			frm.add_custom_button(
				__("Partial Scrap"),
				() => partial_scrap_dialog(frm),
				__("Manage")
			);
		}

		// Same-period Restore (GAP-016 Path 1) — backend gates the window.
		if (frm.doc.status === "Scrapped") {
			frm.add_custom_button(
				__("Restore (Same Period)"),
				() =>
					frappe.call({
						method: "asset_enterprise.restore.restore_asset",
						args: { asset_name: frm.doc.name },
						callback: () => frm.reload_doc(),
					}),
				__("Manage")
			);
			// Create Replacement Asset (GAP-016 Path 2)
			frm.add_custom_button(
				__("Create Replacement Asset"),
				() =>
					frappe.call({
						method: "asset_enterprise.restore.create_replacement_asset",
						args: { source_asset: frm.doc.name },
						callback: (r) => frappe.set_route("Form", "Asset", r.message),
					}),
				__("Manage")
			);
		}

		// Recalculate ledger-derived values (GAP-006)
		frm.add_custom_button(
			__("Recalculate Values"),
			() =>
				frappe.call({
					method: "asset_enterprise.api.recalculate",
					args: { asset_name: frm.doc.name },
					callback: () => frm.reload_doc(),
				}),
			__("Manage")
		);
	},
});

function partial_scrap_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Partial Scrap — {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "scrapping_type",
				fieldtype: "Link",
				options: "Scrapping Type",
				label: __("Scrapping Type"),
				reqd: 1,
			},
			{
				fieldname: "mode",
				fieldtype: "Select",
				options: "By Value\nBy Percentage",
				default: "By Value",
				label: __("Mode"),
			},
			{
				fieldname: "scrap_value",
				fieldtype: "Currency",
				label: __("Scrap Value"),
				depends_on: "eval:doc.mode==='By Value'",
			},
			{
				fieldname: "percentage",
				fieldtype: "Percent",
				label: __("Percentage of HAV"),
				depends_on: "eval:doc.mode==='By Percentage'",
			},
			{ fieldname: "scrap_date", fieldtype: "Date", label: __("Scrap Date"), default: "Today" },
		],
		primary_action_label: __("Post Partial Scrap"),
		primary_action(values) {
			frappe.call({
				method: "asset_enterprise.disposal.partial_scrap_asset",
				args: {
					asset_name: frm.doc.name,
					scrap_value: values.mode === "By Value" ? values.scrap_value : null,
					percentage: values.mode === "By Percentage" ? values.percentage : null,
					scrapping_type: values.scrapping_type,
					scrap_date: values.scrap_date,
				},
				callback: () => {
					d.hide();
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}
