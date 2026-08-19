// Reversal AVAs and reversed originals must be tellable apart in the
// list without opening each document (client, 19/08).
frappe.listview_settings["Asset Value Adjustment"] = {
	add_fields: ["reversal_of_ava", "reversed_by_ava", "docstatus"],
	get_indicator(doc) {
		if (doc.reversal_of_ava) {
			return [__("Reversal"), "orange", "reversal_of_ava,is,set"];
		}
		if (doc.reversed_by_ava) {
			return [__("Reversed"), "red", "reversed_by_ava,is,set"];
		}
		if (doc.docstatus === 1) {
			return [__("Submitted"), "blue", "docstatus,=,1"];
		}
		if (doc.docstatus === 2) {
			return [__("Cancelled"), "grey", "docstatus,=,2"];
		}
		return [__("Draft"), "red", "docstatus,=,0"];
	},
};
