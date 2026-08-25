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

// C3 (GA-0005-01 §3.7): the reversal posting date defaults to today;
// changing it needs the company's Reversal Date Edit Role (per-company
// table on Asset Settings). This locks the dialog's date field when the
// current user lacks the role — enforcement lives server-side in the
// cancel endpoints; this is UX only (C126).
window.ae_apply_reversal_date_gate = function (dialog, company) {
	frappe.call({
		method: "asset_enterprise.api.reversal_date_editable",
		args: { company: company },
		callback(r) {
			const info = r.message || {};
			if (info.editable) return;
			dialog.set_df_property("posting_date", "read_only", 1);
			dialog.set_df_property(
				"posting_date",
				"description",
				info.role
					? __("Only the {0} role may change this date — the reversal posts today.", [
							info.role,
					  ])
					: __(
							"No role is configured to change this date — the reversal posts today."
					  )
			);
		},
	});
};


// GAP-016 / TC-043: a partial scrap is undone by REVERSING it — the
// original entry stays posted. Same pair of routes as a full scrap
// (client ruling, 25/08): same period, or cross-period once that window
// has passed. The asset is never "Scrapped" after a partial scrap, so
// none of the full-scrap buttons ever appear for it.
window.ae_partial_scrap_actions = function (frm, asset) {
	frappe.call({
		method: "asset_enterprise.api.reversible_partial_scraps",
		args: { asset: asset },
		callback(r) {
			const rows = r.message || [];
			if (!rows.length) return;
			frm.add_custom_button(
				__("Reverse Partial Scrap"),
				() => ae_pick_partial_scrap(frm, asset, rows),
				__("Manage")
			);
		},
	});
};

function ae_pick_partial_scrap(frm, asset, rows) {
	const choose = (row) => {
		const cross = !row.in_window;
		frappe.confirm(
			cross
				? __(
						"The same-period window for the {0} scrap of {1} has passed. Reverse it cross-period? The value returns today, the schedule re-prices from there, and the original entry stays posted.",
						[frappe.format(row.posting_date, { fieldtype: "Date" }), format_currency(row.amount)]
				  )
				: __(
						"Reverse the {0} partial scrap of {1}? A mirror entry is posted; the original stays posted.",
						[frappe.format(row.posting_date, { fieldtype: "Date" }), format_currency(row.amount)]
				  ),
			() => {
				frappe.call({
					method: "asset_enterprise.restore.restore_partial_scrap",
					args: {
						asset_name: asset,
						financial_treatment: row.name,
						cross_period: cross ? 1 : 0,
					},
					freeze: true,
					callback(res) {
						if (res.message) {
							frappe.msgprint(
								__("Partial scrap reversed via {0}.", [res.message])
							);
							frm.reload_doc();
						}
					},
				});
			}
		);
	};
	if (rows.length === 1) {
		choose(rows[0]);
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("Which partial scrap?"),
		fields: [
			{
				fieldname: "ft",
				fieldtype: "Select",
				label: __("Partial Scrap"),
				reqd: 1,
				options: rows.map(
					(x) =>
						`${x.name} | ${frappe.format(x.posting_date, { fieldtype: "Date" })} | ${format_currency(
							x.amount
						)}${x.in_window ? "" : " (cross-period)"}`
				),
			},
		],
		primary_action_label: __("Continue"),
		primary_action(values) {
			d.hide();
			choose(rows.find((x) => values.ft.startsWith(x.name)));
		},
	});
	d.show();
}
