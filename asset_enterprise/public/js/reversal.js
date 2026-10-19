// GAP-030 (19/10/2026): an Asset Value Adjustment, a Capitalized Maintenance
// and a capitalized Asset Repair are REVERSED, never cancelled. The server
// decides and offers it: the module's platform action "Reverse" (with its
// posting-date prompt) replaces Cancel, and the platform's headline names
// the reversal (platform.AssetLedgerAdapter). This only shows the state, on
// the form and in the list, the same way for the three doctypes.

// opts: { of_field, link_field }
window.ae_reversal_form = function (frm, opts) {
	if (frm.doc.docstatus !== 1) return;
	if (frm.doc[opts.of_field]) {
		frm.page.set_indicator(__("Reversal"), "orange");
	} else if (frm.doc[opts.link_field]) {
		frm.page.set_indicator(__("Reversed"), "grey");
	}
};

window.ae_reversal_listview = function (doctype, of_field, link_field) {
	frappe.listview_settings[doctype] = Object.assign(frappe.listview_settings[doctype] || {}, {
		add_fields: [of_field, link_field, "docstatus"],
		get_indicator(doc) {
			if (doc[of_field]) return [__("Reversal"), "orange", `${of_field},is,set`];
			if (doc[link_field]) return [__("Reversed"), "grey", `${link_field},is,set`];
			if (doc.docstatus === 1) return [__("Submitted"), "blue", "docstatus,=,1"];
			if (doc.docstatus === 2) return [__("Cancelled"), "red", "docstatus,=,2"];
			return [__("Draft"), "red", "docstatus,=,0"];
		},
	});
};
