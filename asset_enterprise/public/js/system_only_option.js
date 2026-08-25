// Some transaction types are raised BY the system and never chosen by a
// user: a Reversal of Capitalized Maintenance is created when a
// capitalization is cancelled, a Reversal Repair when a repair is
// cancelled, an Invoice Adjustment by the invoice carrying the
// difference. Offering them in the picker invites a document the server
// then has to refuse (client, 24-25/08).
//
// Removed from the SELECT on a new document only — an existing document
// of that type must keep its own value visible, or the field renders
// blank. The server refuses these types independently; this is UX.
window.ae_hide_system_only_option = function (frm, fieldname, option) {
	if (!frm.is_new() || frm.doc[fieldname] === option) return;
	const df = frm.get_docfield(fieldname);
	if (!df || !df.options || !df.options.includes(option)) return;
	frm.set_df_property(
		fieldname,
		"options",
		df.options
			.split("\n")
			.filter((o) => o !== option)
			.join("\n")
	);
};
