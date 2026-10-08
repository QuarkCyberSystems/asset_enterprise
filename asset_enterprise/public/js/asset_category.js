// Client ticket FA - Asset Category (TSK-2026-00735), reopened 08/10: on a
// Control Category every account is an expense account (GAP-037), but core's
// pickers offer only the Fixed Asset / Accumulated Depreciation /
// Depreciation / Capital Work in Progress account types, so no expense
// account could be chosen. Wrap core's query rather than copy it: an
// ordinary category keeps core's filters, a control category is offered the
// company's Expense accounts. The server rule is validate_account_types in
// overrides/asset_category.py.
const AE_CONTROL_ACCOUNTS = [
	"fixed_asset_account",
	"accumulated_depreciation_account",
	"depreciation_expense_account",
	"capital_work_in_progress_account",
];

frappe.ui.form.on("Asset Category", {
	setup(frm) {
		const grid = frm.fields_dict.accounts.grid;
		AE_CONTROL_ACCOUNTS.forEach((fieldname) => {
			const core_query = grid.get_field(fieldname).get_query;
			frm.set_query(fieldname, "accounts", (doc, cdt, cdn) => {
				if (!doc.is_control_category) {
					return core_query ? core_query(doc, cdt, cdn) : {};
				}
				const row = locals[cdt][cdn];
				return {
					filters: { root_type: "Expense", is_group: 0, company: row.company_name },
				};
			});
		});
	},
});
