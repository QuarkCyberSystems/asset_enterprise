// The run names a PERIOD and the system supplies the date — §4.6 posts
// depreciation on the last day of the month, so the date is an answer,
// not an input (client, 25/08). Mirrored server-side in validate(); this
// only lets the user see the date before saving.

const AE_MONTHS = [
	"January", "February", "March", "April", "May", "June",
	"July", "August", "September", "October", "November", "December",
];

function ae_set_period_end(frm) {
	const month = AE_MONTHS.indexOf(frm.doc.period_month);
	const year = parseInt(frm.doc.period_year, 10);
	if (month < 0 || !year) return;
	// Day 0 of the next month is the last day of this one.
	const last = new Date(year, month + 1, 0);
	frm.set_value("posting_date", frappe.datetime.obj_to_str(last));
}

frappe.ui.form.on("Mass Asset Depreciation", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.period_month) {
			const today = frappe.datetime.str_to_obj(frappe.datetime.get_today());
			frm.set_value("period_month", AE_MONTHS[today.getMonth()]);
			frm.set_value("period_year", today.getFullYear());
		}
	},
	period_month: ae_set_period_end,
	period_year: ae_set_period_end,
});
