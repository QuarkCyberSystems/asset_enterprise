// A Reversal Repair is created by reversing a capitalized Asset Repair —
// never chosen by hand (client, 25/08).
frappe.ui.form.on("Asset Repair", {
	refresh(frm) {
		window.ae_hide_system_only_option(frm, "transaction_type", "Reversal");

		// GAP-033 / GAP-030: a capitalized repair is reversed, never
		// cancelled — Reverse is the platform's action, its posting date
		// today unless the user holds the company's Reversal Date Edit Role.
		// A repair that capitalized nothing cancels natively.
		window.ae_reversal_form(frm, { of_field: "reversal_of_repair", link_field: "reversed_by_repair" });
	},
});
