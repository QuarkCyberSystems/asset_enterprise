// GA-0005-01 — Asset Value Adjustment form additions.
//
// A Reversal AVA carries the swapped values and the reversal_of_ava
// link, but visually looked like any ordinary adjustment (client,
// 19/08). Announce the relationship loudly on BOTH documents.

frappe.ui.form.on("Asset Value Adjustment", {
	refresh(frm) {
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
