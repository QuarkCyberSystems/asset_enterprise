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

	// C3 (§3.7 / GAP-030): cancelling a Standard AVA raises its Reversal
	// AVA. Offer the posting-date choice (default today; role-gated)
	// instead of the bare cancel — the server enforces the same rules.
	before_cancel(frm) {
		if (frm.doc.reversal_of_ava || frm.doc.reversed_by_ava) {
			return; // a reversal (or an already-reversed original) — let the server refuse
		}
		frappe.validated = false;
		ae_reverse_ava_dialog(frm);
	},

	refresh(frm) {
		window.ae_hide_system_only_option(frm, "transaction_type", "Invoice Adjustment");
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

// C3 — confirm + posting-date dialog for the auto-raised Reversal AVA.
function ae_reverse_ava_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Reverse Asset Value Adjustment"),
		fields: [
			{ fieldtype: "HTML", fieldname: "preview" },
			{
				fieldtype: "Date",
				fieldname: "posting_date",
				label: __("Reversal Posting Date"),
				default: frappe.datetime.get_today(),
			},
		],
		primary_action_label: __("Cancel & Create Reversal"),
		primary_action(values) {
			frappe.call({
				method: "asset_enterprise.api.cancel_ava_with_reversal",
				args: { ava_name: frm.doc.name, posting_date: values.posting_date },
				freeze: true,
				callback(r) {
					if (r.exc) return;
					d.hide();
					frappe.show_alert({
						message: __("Reversal AVA created — posting date {0}.", [values.posting_date]),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		},
	});
	d.fields_dict.preview.$wrapper.html(
		__(
			"This reverses <b>{0}</b> ({1}). The original journal entry {2} stays posted per the immutable ledger; a mirror entry posts on the chosen date and depreciation recalculates prospectively.",
			[
				frappe.utils.escape_html(frm.doc.name),
				frappe.utils.escape_html(frm.doc.transaction_type || ""),
				frm.doc.journal_entry
					? `<a href="/app/journal-entry/${encodeURIComponent(
							frm.doc.journal_entry
					  )}"><b>${frappe.utils.escape_html(frm.doc.journal_entry)}</b></a>`
					: __("(none)"),
			]
		)
	);
	window.ae_apply_reversal_date_gate(d, frm.doc.company);
	d.show();
}
