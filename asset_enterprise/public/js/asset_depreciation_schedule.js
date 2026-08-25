// GAP-031: only the ACTIVE generation may post. A superseded schedule is
// a historical copy — its rows were re-priced into the generation that
// replaced it — so offering "Make Depreciation Entry" on one invites an
// action the server refuses, and it used to post a stale amount while
// the Active generation still showed the period as due (client, 25/08).
frappe.ui.form.on("Asset Depreciation Schedule", {
	refresh(frm) {
		const live = frm.doc.status === "Active";
		frm.fields_dict.depreciation_schedule?.grid?.update_docfield_property(
			"make_depreciation_entry",
			"hidden",
			live ? 0 : 1
		);
		if (!live && frm.doc.status) {
			frm.dashboard.clear_headline();
			frm.dashboard.set_headline(
				__(
					"This schedule is {0} — kept for audit. Depreciation posts only from the asset's Active schedule.",
					[__(frm.doc.status)]
				),
				"orange"
			);
		}
	},
});
