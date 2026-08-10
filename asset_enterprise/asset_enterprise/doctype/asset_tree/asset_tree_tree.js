// Asset Tree — native Tree View settings (GAP-009).
frappe.treeview_settings["Asset Tree"] = {
	title: __("Asset Tree"),
	breadcrumb: "Assets",
	get_tree_root: false,
	menu_items: [
		{
			label: __("Open Asset Tree Report"),
			action: () => frappe.set_route("query-report", "Asset Tree"),
		},
	],
	onclick(node) {
		// node name == asset id — one click through to the Asset.
		if (node && node.data && node.data.value && !node.is_root) {
			frappe.set_route("Form", "Asset", node.data.value);
		}
	},
};
