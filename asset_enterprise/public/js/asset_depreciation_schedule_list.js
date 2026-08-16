// GA-0005-01 §4.8 — make supersession legible.
//
// An asset can carry several schedules: the live one plus one frozen
// copy per value change. Core hides the status field entirely, so the
// list gave no way to tell them apart. Colour it.

frappe.listview_settings["Asset Depreciation Schedule"] = {
	add_fields: ["status", "asset", "supersedes"],
	filters: [["status", "!=", "Cancelled"]],

	get_indicator(doc) {
		const map = {
			Active: ["Active", "green", "status,=,Active"],
			Superseded: ["Superseded", "gray", "status,=,Superseded"],
			Draft: ["Draft", "red", "status,=,Draft"],
			Cancelled: ["Cancelled", "dark grey", "status,=,Cancelled"],
		};
		return map[doc.status] || [doc.status, "blue", "status,=," + doc.status];
	},
};
