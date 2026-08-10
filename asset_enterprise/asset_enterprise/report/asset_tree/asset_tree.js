// Asset Tree — collapsible hierarchy view (GAP-009 / TC-012).
frappe.query_reports["Asset Tree"] = {
	tree: true,
	name_field: "asset",
	parent_field: "parent_asset",
	initial_depth: 10,
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
		},
		{
			fieldname: "include_standalone",
			label: __("Include Standalone Assets"),
			fieldtype: "Check",
			default: 0,
		},
	],
};
