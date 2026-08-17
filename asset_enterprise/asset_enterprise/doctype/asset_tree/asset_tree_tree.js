// Asset Tree — native Tree View settings (GAP-009).
frappe.treeview_settings["Asset Tree"] = {
	title: __("Asset Tree"),
	breadcrumb: "Assets",
	root_label: "All Asset Trees",
	get_tree_root: false,
	get_tree_nodes:
		"asset_enterprise.asset_enterprise.doctype.asset_tree.asset_tree.get_children",
	ignore_fields: ["parent_asset_tree"],
	menu_items: [
		{
			label: __("Open Asset Tree Report"),
			action: () => frappe.set_route("query-report", "Asset Tree"),
		},
	],
	onload: function (treeview) {
		treeview.make_tree();
	},

	// A tree view has no columns, so the money is rendered onto the node
	// itself: the asset's own net book value, and for a parent the total
	// of its whole subtree (client sheet, row 44).
	onrender: function (node) {
		const d = node.data || {};
		if (d.nbv === undefined || d.nbv === null) {
			return;
		}
		const fmt = (v) => format_currency(v, d.currency);
		const has_children = cint(d.expandable);
		const own = `<span class="text-muted">${__("NBV")} ${fmt(d.nbv)}</span>`;
		const subtree =
			has_children && d.subtree_nbv !== d.nbv
				? ` <span class="text-muted">· ${__("with parts")} ${fmt(d.subtree_nbv)}</span>`
				: "";
		$(node.$tree_link)
			.find(".tree-label")
			.append(` <span class="ml-2 small">${own}${subtree}</span>`);
	},
};
