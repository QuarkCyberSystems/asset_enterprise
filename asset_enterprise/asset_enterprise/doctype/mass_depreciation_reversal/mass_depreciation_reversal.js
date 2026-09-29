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
	refresh(frm) {
		lock_posting_date(frm);
	},
	company(frm) {
		lock_posting_date(frm);
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

// Reversals post today (client ticket FA-003, 29/09: a 31/03 date typed
// for a March reversal was refused with a message about the source's
// date). Without the company's Reversal Date Edit Role the date is set
// to today and locked — the same rule as the single-reversal dialogs.
function lock_posting_date(frm) {
	if (frm.doc.docstatus !== 0 || !frm.doc.company) return;
	frappe.call({
		method: "asset_enterprise.api.reversal_date_editable",
		args: { company: frm.doc.company },
		callback(r) {
			const info = r.message || {};
			frm.set_df_property("posting_date", "read_only", info.editable ? 0 : 1);
			if (info.editable) return;
			const today = frappe.datetime.get_today();
			if (frm.doc.posting_date !== today) frm.set_value("posting_date", today);
			frm.set_df_property(
				"posting_date",
				"description",
				info.role
					? __("Only the {0} role may change this date — the reversal posts today.", [info.role])
					: __("No role is configured to change this date — the reversal posts today.")
			);
		},
	});
}
