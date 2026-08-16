// GA-0005-01 GAP-012 — Asset Allocation usability.
//
// The allocation table disambiguates PARTIAL invoices; the server
// auto-resolves it for ordinary full invoices. Here we (a) restrict
// the Asset picker to assets this invoice can actually cover, and
// (b) open the section as soon as the invoice carries a fixed-asset
// row, so the difference treatment is never invisible.

frappe.ui.form.on("Purchase Invoice", {
	setup(frm) {
		frm.set_query("asset", "pi_asset_allocation", () => ({
			query: "asset_enterprise.invoice_diff.allocatable_assets",
			filters: { purchase_invoice: frm.doc.name },
		}));
	},

	refresh(frm) {
		toggle_asset_allocation(frm);
	},

	items_on_form_rendered(frm) {
		toggle_asset_allocation(frm);
	},
});

frappe.ui.form.on("Purchase Invoice Item", {
	is_fixed_asset(frm) {
		toggle_asset_allocation(frm);
	},
	items_remove(frm) {
		toggle_asset_allocation(frm);
	},
});

function toggle_asset_allocation(frm) {
	const has_asset_row = (frm.doc.items || []).some((row) => row.is_fixed_asset);
	if (!has_asset_row) {
		return;
	}
	const section = frm.get_field("pi_asset_allocation_section");
	if (section && section.collapse) {
		section.collapse(false);
	}
}
