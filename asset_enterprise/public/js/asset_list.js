// The Asset badge — list AND form header — comes from this indicator
// map. ERPNext's map has no entry for "Disposed" (added for merged-away
// assets, client sheet item 24), so the badge fell back to the document
// state and read "Submitted" while the record said Disposed.

frappe.listview_settings["Asset"] = {
	add_fields: ["status"],

	get_indicator(doc) {
		const map = {
			Draft: "red",
			Submitted: "blue",
			"Partially Depreciated": "blue",
			"Fully Depreciated": "green",
			"In Maintenance": "orange",
			"Out of Order": "orange",
			"Work In Progress": "orange",
			Issue: "orange",
			Receipt: "blue",
			Disposed: "grey",
			Capitalized: "grey",
			Sold: "grey",
			Scrapped: "grey",
			Cancelled: "dark grey",
		};
		const colour = map[doc.status] || "blue";
		return [__(doc.status), colour, "status,=," + doc.status];
	},
};
