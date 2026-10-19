// GAP-030 (19/10/2026): an Asset Value Adjustment, a Capitalized Maintenance
// and a capitalized Asset Repair are REVERSED, never cancelled. The server
// decides (asset_enterprise.reversal; the platform adapter hides Cancel and
// says why); this only offers the action with its date and shows the state.
// One helper for the three forms, so they cannot drift apart.

// opts: { link_field, of_field, label, note }  — note: HTML shown in the dialog
window.ae_reversal_form = function (frm, opts) {
	if (frm.doc.docstatus !== 1) return;
	if (frm.doc[opts.of_field]) {
		frm.page.set_indicator(__("Reversal"), "orange");
		return;
	}
	if (frm.doc[opts.link_field]) {
		frm.page.set_indicator(__("Reversed"), "grey");
		return;
	}
	if (opts.eligible === false) return;
	frm.add_custom_button(__("Reverse"), () => ae_reverse_dialog(frm, opts), __("Actions"));
};

function ae_reverse_dialog(frm, opts) {
	const d = new frappe.ui.Dialog({
		title: __("Reverse {0}", [__(opts.label)]),
		fields: [
			{ fieldtype: "HTML", fieldname: "preview" },
			{
				fieldtype: "Date",
				fieldname: "posting_date",
				label: __("Reversal Posting Date"),
				reqd: 1,
				default: frappe.datetime.get_today(),
			},
		],
		primary_action_label: __("Reverse"),
		primary_action(values) {
			frappe
				.xcall("asset_enterprise.api.reverse_document", {
					doctype: frm.doctype,
					name: frm.doc.name,
					posting_date: values.posting_date,
				})
				.then((counter) => {
					d.hide();
					frappe.show_alert({
						message: __("Reversed by {0} on {1}.", [counter, frappe.datetime.str_to_user(values.posting_date)]),
						indicator: "green",
					});
					frm.reload_doc();
				});
		},
	});
	d.fields_dict.preview.$wrapper.html(
		`<p>${__(
			"A reversal entry posts the mirror of {0} on the date below. {0} stays on the record, marked Reversed; its ledger entries remain posted (immutable ledger).",
			[`<b>${frappe.utils.escape_html(frm.doc.name)}</b>`]
		)}</p>${opts.note || ""}`
	);
	window.ae_apply_reversal_date_gate(d, frm.doc.company);
	d.show();
}

// The list badge for the same three states.
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
