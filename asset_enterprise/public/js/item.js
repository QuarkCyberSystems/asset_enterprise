// FA-013 (client, 01/10/2026): a fixed-asset item is held in a unit
// ticked "Allowed For FA" on the UOM master, and lists no other unit.
// The pickers offer only those units; the server enforces the rule.
frappe.ui.form.on("Item", {
	setup(frm) {
		const fa_query = () =>
			frm.doc.is_fixed_asset && (frm.__fa_uoms || []).length
				? { filters: { name: ["in", frm.__fa_uoms] } }
				: {};
		frm.set_query("stock_uom", fa_query);
		frm.set_query("purchase_uom", fa_query);
		frm.set_query("sales_uom", fa_query);
		frm.set_query("uom", "uoms", fa_query);
	},
	onload(frm) {
		frappe.call({
			method: "asset_enterprise.asset_items.fa_uom_filters",
			callback: (r) => (frm.__fa_uoms = r.message || []),
		});
	},
});
