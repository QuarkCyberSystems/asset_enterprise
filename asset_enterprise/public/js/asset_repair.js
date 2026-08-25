// A Reversal Repair is created when a capitalized Asset Repair is
// cancelled — never chosen by hand (client, 25/08).
frappe.ui.form.on("Asset Repair", {
	refresh(frm) {
		window.ae_hide_system_only_option(frm, "transaction_type", "Reversal");

		// C3 (GAP-033): the design names a "Reverse Repair" button on the
		// source form — the reversal posts TODAY by default, and a user
		// with the company's Reversal Date Edit Role may pick another
		// date. The plain Cancel still works and posts today (the
		// backend fallback); this button is the date-choice surface.
		if (
			frm.doc.docstatus === 1 &&
			frm.doc.capitalize_repair_cost &&
			frm.doc.transaction_type !== "Reversal" &&
			!frm.doc.reversed_by_repair
		) {
			frm.add_custom_button(
				__("Reverse Repair"),
				() => ae_reverse_repair_dialog(frm),
				__("Actions")
			);
		}
	},
});

function ae_reverse_repair_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Reverse Asset Repair"),
		fields: [
			{ fieldtype: "HTML", fieldname: "preview" },
			{
				fieldtype: "Date",
				fieldname: "posting_date",
				label: __("Reversal Posting Date"),
				default: frappe.datetime.get_today(),
			},
		],
		primary_action_label: __("Cancel & Create Reversal"),
		primary_action(values) {
			frappe.call({
				method: "asset_enterprise.api.cancel_repair_with_reversal",
				args: { repair_name: frm.doc.name, posting_date: values.posting_date },
				freeze: true,
				callback(r) {
					if (r.exc) return;
					d.hide();
					frappe.show_alert({
						message: __("Reversal Repair created — posting date {0}.", [
							values.posting_date,
						]),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		},
	});
	const stock_note =
		frm.doc.stock_items && frm.doc.stock_items.length
			? __(
					"Consumed stock items will be returned to their source warehouse via a Material Receipt."
			  )
			: "";
	d.fields_dict.preview.$wrapper.html(
		__(
			"This reverses repair <b>{0}</b> — the capitalized cost {1} leaves the asset, the life grant is retracted, and the original journal entry stays posted per the immutable ledger.{2}",
			[
				frappe.utils.escape_html(frm.doc.name),
				format_currency(frm.doc.total_repair_cost || 0),
				stock_note ? "<br><br>" + stock_note : "",
			]
		)
	);
	window.ae_apply_reversal_date_gate(d, frm.doc.company);
	d.show();
}
