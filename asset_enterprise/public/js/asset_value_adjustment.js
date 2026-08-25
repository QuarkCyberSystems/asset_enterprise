// GA-0005-01 — Asset Value Adjustment form additions.
//
// A Reversal AVA carries the swapped values and the reversal_of_ava
// link, but visually looked like any ordinary adjustment (client,
// 19/08). Announce the relationship loudly on BOTH documents.

// The Difference Account is resolved from the Asset Category (§3.5) and
// locked for the types that own one, and Invoice Adjustment is dropped
// from the picker — it is raised by the Purchase Invoice, never by hand
// (client, 24/08).
const AE_SYSTEM_ACCOUNT_TYPES = [
	"Initial Impairment",
	"Upward Revaluation",
	"Invoice Adjustment",
];

function ae_apply_difference_account(frm) {
	const ttype = frm.doc.transaction_type;
	if (!frm.doc.asset || !AE_SYSTEM_ACCOUNT_TYPES.includes(ttype)) {
		frm.set_df_property("difference_account", "read_only", 0);
		frm.set_df_property("difference_account", "description", "");
		return;
	}
	frappe.call({
		method: "asset_enterprise.api.ava_difference_account",
		args: { asset: frm.doc.asset, transaction_type: ttype },
		callback(r) {
			if (!r.message) return;
			frm.set_df_property("difference_account", "read_only", 1);
			if (r.message.account) {
				frm.set_value("difference_account", r.message.account);
				frm.set_df_property(
					"difference_account",
					"description",
					__("From Asset Category {0}.", [r.message.asset_category])
				);
			} else {
				frm.set_df_property(
					"difference_account",
					"description",
					__(
						"Not configured on Asset Category {0} or its Company defaults — set it there.",
						[r.message.asset_category]
					)
				);
			}
		},
	});
}

function ae_hide_invoice_adjustment(frm) {
	// Only on a NEW document: an existing Invoice Adjustment (or its
	// reversal) must keep its own value visible in the select.
	if (!frm.is_new() || frm.doc.transaction_type === "Invoice Adjustment") return;
	const df = frm.get_docfield("transaction_type");
	if (!df || !df.options) return;
	const kept = df.options
		.split("\n")
		.filter((o) => o !== "Invoice Adjustment")
		.join("\n");
	frm.set_df_property("transaction_type", "options", kept);
}

frappe.ui.form.on("Asset Value Adjustment", {
	asset(frm) {
		ae_apply_difference_account(frm);
	},

	transaction_type(frm) {
		ae_apply_difference_account(frm);
	},

	refresh(frm) {
		ae_hide_invoice_adjustment(frm);
		ae_apply_difference_account(frm);
		if (frm.doc.reversal_of_ava) {
			frm.set_intro(
				__(
					"REVERSAL ENTRY — this document reverses {0}. The original journal entry stays posted; this document posted the mirror entry {1}.",
					[
						`<a href="/app/asset-value-adjustment/${encodeURIComponent(
							frm.doc.reversal_of_ava
						)}"><b>${frappe.utils.escape_html(frm.doc.reversal_of_ava)}</b></a>`,
						frm.doc.journal_entry
							? `<a href="/app/journal-entry/${encodeURIComponent(
									frm.doc.journal_entry
							  )}"><b>${frappe.utils.escape_html(frm.doc.journal_entry)}</b></a>`
							: "",
					]
				),
				"orange"
			);
			frm.page.set_indicator(__("Reversal"), "orange");
		} else if (frm.doc.reversed_by_ava) {
			frm.set_intro(
				__(
					"This adjustment has been REVERSED by {0}. Its journal entry remains posted per the immutable ledger; the reversal posted the mirror.",
					[
						`<a href="/app/asset-value-adjustment/${encodeURIComponent(
							frm.doc.reversed_by_ava
						)}"><b>${frappe.utils.escape_html(frm.doc.reversed_by_ava)}</b></a>`,
					]
				),
				"red"
			);
			frm.page.set_indicator(__("Reversed"), "red");
		}
	},
});
