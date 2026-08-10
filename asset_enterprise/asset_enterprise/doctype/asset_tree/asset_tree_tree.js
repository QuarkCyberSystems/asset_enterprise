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
};
