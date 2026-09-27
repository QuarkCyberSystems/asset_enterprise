// The run names the PERIOD whose depreciation is reversed; the reversal
// itself posts on the Reversal Posting Date (today unless the user holds
// the Reversal Date Edit Role — enforced server-side).

const AE_MDR_MONTHS = [
	"January", "February", "March", "April", "May", "June",
	"July", "August", "September", "October", "November", "December",
];

frappe.ui.form.on("Mass Depreciation Reversal", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.period_month) {
			const today = frappe.datetime.str_to_obj(frappe.datetime.get_today());
			frm.set_value("period_month", AE_MDR_MONTHS[today.getMonth()]);
			frm.set_value("period_year", today.getFullYear());
		}
	},
	setup(frm) {
		frm.set_query("mass_asset_depreciation", () => ({
			filters: { docstatus: 1, company: frm.doc.company },
		}));
		frm.set_query("asset", "selected_assets", () => ({
			filters: { docstatus: 1, company: frm.doc.company, calculate_depreciation: 1 },
		}));
	},
});
