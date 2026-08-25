// A cost-centre transfer posts nothing of its own — its effect shows up
// later, inside depreciation entries. Say what will happen BEFORE the
// movement is submitted (client, 24/08), so the split and the treatment
// of earlier unposted periods are not a surprise afterwards.
frappe.ui.form.on("Asset Movement", {
	before_submit(frm) {
		const rows = (frm.doc.assets || []).filter((d) => d.target_cost_center);
		if (!rows.length) return;

		return new Promise((resolve, reject) => {
			frappe.call({
				method: "asset_enterprise.api.movement_cost_centre_impact",
				args: {
					asset: rows[0].asset,
					transaction_date: frm.doc.transaction_date,
					target_cost_center: rows[0].target_cost_center,
				},
				callback(r) {
					const m = r.message;
					if (!m || (!m.split && !(m.earlier_unposted || []).length)) {
						resolve();
						return;
					}
					const lines = [];
					if (m.split) {
						lines.push(
							__(
								"The period ending {0} will be split by days in ONE entry: {1} to {2} ({3} days) and {4} to {5} ({6} days).",
								[
									m.split.period_end,
									format_currency(m.split.old_amount),
									m.old_cost_center,
									m.split.days_before,
									format_currency(m.split.new_amount),
									m.new_cost_center,
									m.split.days_after,
								]
							)
						);
					}
					if ((m.earlier_unposted || []).length) {
						lines.push(
							__(
								"{0} earlier period(s) are still unposted. They stay with {1} whenever they are posted, because depreciation follows the cost centre that held the asset during the period — not the one it moves to now.",
								[m.earlier_unposted.length, m.old_cost_center]
							)
						);
					}
					if (rows.length > 1) {
						lines.push(
							__("The same applies to the other {0} asset(s) on this movement.", [
								rows.length - 1,
							])
						);
					}
					frappe.confirm(
						`<b>${__("Effect on depreciation")}</b><br><br>` +
							lines.map((l) => `• ${l}`).join("<br><br>") +
							`<br><br>${__("Submit this transfer?")}`,
						resolve,
						() => reject(new Error("cancelled"))
					);
				},
				error: resolve,
			});
		});
	},
});
