// Client, 20/08: the Scrap Transaction must SHOW where it posts. The
// account and cost centre come from the Scrapping Type and are
// read-only; a type that allows a cost-centre change unlocks that one
// field. Resolution is server-side (api.scrap_posting_defaults) — this
// only mirrors it into the form so the user sees it before submitting.
frappe.ui.form.on("Scrap Transaction", {
	refresh(frm) {
		frm.trigger("ae_lock_cost_center");
		if (
			frm.doc.docstatus === 1 &&
			frm.doc.scrap_type === "Partial Scrap" &&
			!frm.doc.reversal_journal_entry
		) {
			window.ae_partial_scrap_actions(frm, frm.doc.asset);
		}
	},

	asset(frm) {
		frm.trigger("ae_fetch_posting_defaults");
	},

	scrapping_type(frm) {
		frm.trigger("ae_fetch_posting_defaults");
	},

	ae_fetch_posting_defaults(frm) {
		if (!frm.doc.asset) return;
		frappe.call({
			method: "asset_enterprise.api.scrap_posting_defaults",
			args: { asset: frm.doc.asset, scrapping_type: frm.doc.scrapping_type },
			callback(r) {
				if (!r.message) return;
				frm.set_value("disposal_account", r.message.disposal_account);
				frm.set_value(
					"allow_cost_center_override",
					r.message.allow_cost_center_override
				);
				// Keep a deliberate override; otherwise follow the type.
				if (!r.message.allow_cost_center_override || !frm.doc.cost_center) {
					frm.set_value("cost_center", r.message.cost_center);
				}
				frm.trigger("ae_lock_cost_center");
			},
		});
	},

	ae_lock_cost_center(frm) {
		frm.set_df_property(
			"cost_center",
			"read_only",
			frm.doc.allow_cost_center_override ? 0 : 1
		);
		frm.set_df_property(
			"cost_center",
			"description",
			frm.doc.allow_cost_center_override
				? __("This Scrapping Type allows the cost centre to be changed.")
				: __("Locked to the Scrapping Type's cost centre.")
		);
	},
});
