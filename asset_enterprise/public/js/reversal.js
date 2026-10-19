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

// A row that is neither keeps the list's own indicator (core's, e.g. a
// repair's Pending / Completed), else frappe's default by docstatus.
window.ae_reversal_listview = function (doctype, of_field, link_field) {
	const settings = (frappe.listview_settings[doctype] = frappe.listview_settings[doctype] || {});
	const prior = settings.get_indicator;
	settings.add_fields = [...new Set([...(settings.add_fields || []), of_field, link_field, "docstatus"])];
	settings.get_indicator = function (doc) {
		if (doc[of_field]) return [__("Reversal"), "orange", `${of_field},is,set`];
		if (doc[link_field]) return [__("Reversed"), "grey", `${link_field},is,set`];
		return prior ? prior.call(this, doc) : undefined;
	};
};
