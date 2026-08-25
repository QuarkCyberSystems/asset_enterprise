// A Reversal Repair is created when a capitalized Asset Repair is
// cancelled — never chosen by hand (client, 25/08).
frappe.ui.form.on("Asset Repair", {
	refresh(frm) {
		window.ae_hide_system_only_option(frm, "transaction_type", "Reversal");
	},
});
