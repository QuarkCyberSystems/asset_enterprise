// A Reversal Repair is created by reversing a capitalized Asset Repair —
// never chosen by hand (client, 25/08).
frappe.ui.form.on("Asset Repair", {
	refresh(frm) {
		window.ae_hide_system_only_option(frm, "transaction_type", "Reversal");

		// GAP-033 / GAP-030: a capitalized repair is reversed, never
		// cancelled — the reversal posts today by default, and a user with
		// the company's Reversal Date Edit Role may pick another date. A
		// repair that capitalized nothing cancels natively.
		window.ae_reversal_form(frm, {
			of_field: "reversal_of_repair",
			link_field: "reversed_by_repair",
			label: "Asset Repair",
			eligible: !!frm.doc.capitalize_repair_cost && frm.doc.transaction_type !== "Reversal",
			note: ae_repair_reversal_note(frm),
		});
	},
});

function ae_repair_reversal_note(frm) {
	const stock_note =
		frm.doc.stock_items && frm.doc.stock_items.length
			? " " + __("Consumed stock items return to their source warehouse by a Material Receipt.")
			: "";
	return `<p>${__("The capitalized cost {0} leaves the asset and the life grant is retracted.", [
		format_currency(frm.doc.total_repair_cost || 0),
	])}${stock_note}</p>`;
}
