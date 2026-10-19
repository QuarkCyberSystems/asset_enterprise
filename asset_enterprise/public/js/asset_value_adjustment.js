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

// PA-008: a Project Settlement is raised by the Project Settlement Run,
// which sets the type and the account it settles from; neither is the
// user's to change.
const AE_PROJECT_SETTLEMENT = "Project Settlement";

function ae_apply_difference_account(frm) {
	const ttype = frm.doc.transaction_type;
	// reset as well as set: the form object is reused across documents
	frm.set_df_property("transaction_type", "read_only", ttype === AE_PROJECT_SETTLEMENT ? 1 : 0);
	if (ttype === AE_PROJECT_SETTLEMENT) {
		frm.set_df_property("difference_account", "read_only", 1);
		frm.set_df_property(
			"difference_account",
			"description",
			__("Set by the Project Settlement Run that capitalizes the cost.")
		);
		return;
	}
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
			if (r.message.account) {
				frm.set_df_property("difference_account", "read_only", 1);
				frm.set_value("difference_account", r.message.account);
				frm.set_df_property(
					"difference_account",
					"description",
					__("From Asset Category {0}.", [r.message.asset_category])
				);
			} else {
				// Lock it only when we can actually supply it. The field is
				// mandatory, so a locked EMPTY one would leave the user
				// unable to save at all — worse than choosing by hand.
				frm.set_df_property("difference_account", "read_only", 0);
				frm.set_df_property(
					"difference_account",
					"description",
					__(
						"Set this account on Asset Category {0} (or the Company defaults) and it will fill in automatically.",
						[r.message.asset_category]
					)
				);
			}
		},
	});
}

frappe.ui.form.on("Asset Value Adjustment", {
	asset(frm) {
		ae_apply_difference_account(frm);
	},

	transaction_type(frm) {
		ae_apply_difference_account(frm);
	},

	refresh(frm) {
		window.ae_hide_system_only_option(frm, "transaction_type", "Invoice Adjustment");
		window.ae_hide_system_only_option(frm, "transaction_type", AE_PROJECT_SETTLEMENT);
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
		}
		// GAP-030: reversed, never cancelled — the original stays submitted
		// (the platform's headline names its reversal)
		window.ae_reversal_form(frm, {
			of_field: "reversal_of_ava",
			link_field: "reversed_by_ava",
			label: "Asset Value Adjustment",
			note: ae_ava_reversal_note(frm),
		});
	},
});

function ae_ava_reversal_note(frm) {
	return `<p>${__("The mirror of journal entry {0} posts on that date and depreciation recalculates from it.", [
		frm.doc.journal_entry
			? `<a href="/app/journal-entry/${encodeURIComponent(frm.doc.journal_entry)}"><b>${frappe.utils.escape_html(
					frm.doc.journal_entry
			  )}</b></a>`
			: __("(none)"),
	])}</p>`;
}
