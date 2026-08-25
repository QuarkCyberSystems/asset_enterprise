// GA-0005-01 v2.14 — Asset form extensions (§9.6). JS is UX-only;
// every action calls a whitelisted backend that re-validates (C126).
frappe.ui.form.on("Asset", {
	// ERPNext decides whether Purchase Receipt / Purchase Invoice are
	// mandatory in toggle_reference_doc, which it triggers on refresh and
	// on those two fields — but NOT on asset_type. A new Asset therefore
	// refreshes with no type set, falls through to the fallback that
	// flags both mandatory, and choosing "Existing Asset" afterwards
	// never clears them. The user is then blocked by two fields that
	// depends_on has hidden from the form (client report 17/08/2026).
	asset_type(frm) {
		frm.trigger("toggle_reference_doc");
	},

	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;

		// Partial Scrap (GAP-018)
		if (!["Scrapped", "Sold", "Capitalized", "Cancelled"].includes(frm.doc.status)) {
			frm.add_custom_button(
				__("Partial Scrap"),
				() => partial_scrap_dialog(frm),
				__("Manage")
			);
		}

		// A partially scrapped asset keeps its normal status, so it never
		// matches the full-scrap buttons below (client, 25/08).
		window.ae_partial_scrap_actions(frm, frm.doc.name);

		// Same-period Restore (GAP-016 Path 1) — backend gates the window.
		if (frm.doc.status === "Scrapped") {
			frm.add_custom_button(
				__("Restore (Same Period)"),
				() =>
					frappe.call({
						method: "asset_enterprise.restore.restore_asset",
						args: { asset_name: frm.doc.name },
						callback: () => frm.reload_doc(),
					}),
				__("Manage")
			);
			// (Create Replacement Asset is added below for all disposed
			// statuses, not just Scrapped.)
			// Cross-Period Restore (GAP-016 Path 3, v2.16) — catch-up
			// depreciation covers the disposed periods in one entry.
			frm.add_custom_button(
				__("Cross-Period Restore"),
				() =>
					frappe.confirm(
						__(
							"Restore {0} with its value as of the disposal date? The first " +
								"depreciation after restore will catch up the disposed periods " +
								"in one posting (Path 3).",
							[frm.doc.name]
						),
						() =>
							frappe.call({
								method: "asset_enterprise.restore.cross_period_restore",
								args: { asset_name: frm.doc.name },
								callback: () => frm.reload_doc(),
							})
					),
				__("Manage")
			);
		}

		// Create Replacement Asset (GAP-016 Path 2) — any disposed state.
		if (["Scrapped", "Sold", "Capitalized"].includes(frm.doc.status)) {
			frm.add_custom_button(
				__("Create Replacement Asset"),
				() =>
					frappe.call({
						method: "asset_enterprise.restore.create_replacement_asset",
						args: { source_asset: frm.doc.name },
						callback: (r) => frappe.set_route("Form", "Asset", r.message),
					}),
				__("Manage")
			);
		}

		// Post Final Row with tolerance handling (§4.10 point 4, v2.16).
		if (frm.doc.calculate_depreciation) {
			frm.add_custom_button(
				__("Post Final Row (Tolerance)"),
				() => post_final_row_dialog(frm),
				__("Manage")
			);
		}

		// Recalculate ledger-derived values (GAP-006)
		frm.add_custom_button(
			__("Recalculate Values"),
			() =>
				frappe.call({
					method: "asset_enterprise.api.recalculate",
					args: { asset_name: frm.doc.name },
					callback: () => frm.reload_doc(),
				}),
			__("Manage")
		);

		// Enable Depreciation after creation (GAP-011) — amendment-free.
		if (
			!frm.doc.calculate_depreciation &&
			!["Scrapped", "Sold", "Capitalized", "Cancelled"].includes(frm.doc.status)
		) {
			frm.add_custom_button(
				__("Enable Depreciation"),
				() => enable_depreciation_dialog(frm),
				__("Manage")
			);
		}

		// Asset Tree report (GAP-009) — collapsible hierarchy with values.
		frm.add_custom_button(
			__("Asset Tree"),
			() => frappe.set_route("query-report", "Asset Tree"),
			__("View")
		);

		render_tree_panel(frm);
	},
});

function render_tree_panel(frm) {
	// GAP-009: parent link + children table directly on the form.
	frappe.call({
		method: "asset_enterprise.api.tree_panel",
		args: { asset_name: frm.doc.name },
		callback: (r) => {
			const t = r.message || {};
			if (!t.parent && !(t.children || []).length) return;

			let html = "";
			if (t.parent) {
				html += `<p>${__("Part of")} <a href="/app/asset/${encodeURIComponent(
					t.parent
				)}"><b>${frappe.utils.escape_html(t.parent)}</b></a> — ${frappe.utils.escape_html(
					t.parent_name || ""
				)}</p>`;
			}
			if ((t.children || []).length) {
				const rows = t.children
					.map(
						(c) => `<tr>
							<td><a href="/app/asset/${encodeURIComponent(c.name)}">${frappe.utils.escape_html(
								c.name
							)}</a></td>
							<td>${frappe.utils.escape_html(c.asset_name || "")}</td>
							<td>${frappe.utils.escape_html(c.status || "")}</td>
							<td class="text-right">${format_currency(c.historical_asset_value)}</td>
							<td class="text-right">${format_currency(c.net_book_value)}</td>
						</tr>`
					)
					.join("");
				html += `<table class="table table-bordered table-sm" style="margin-bottom:6px">
					<thead><tr>
						<th>${__("Child Asset")}</th><th>${__("Name")}</th><th>${__("Status")}</th>
						<th class="text-right">${__("HAV")}</th><th class="text-right">${__("NBV")}</th>
					</tr></thead><tbody>${rows}</tbody></table>`;
				if (t.totals) {
					html += `<p><b>${__("Subtree totals")}:</b> ${__("Assets")} ${t.totals.assets} ·
						HAV ${format_currency(t.totals.historical_asset_value)} ·
						${__("Accum")} ${format_currency(t.totals.accumulated_depreciation_value)} ·
						NBV ${format_currency(t.totals.net_book_value)}</p>`;
				}
			}
			frm.dashboard.add_section(html, __("Asset Tree"));
		},
	});
}

function enable_depreciation_dialog(frm) {
	// Prefill from the Asset Category's finance-book defaults — the same
	// values a new Asset inherits when the category is picked (client,
	// 18/08: dialog opened empty although the category carries them).
	frappe.call({
		method: "asset_enterprise.api.enable_depreciation_defaults",
		args: { asset_name: frm.doc.name },
		callback: (r) => open_enable_depreciation_dialog(frm, r.message || {}),
	});
}

function open_enable_depreciation_dialog(frm, defaults) {
	const d = new frappe.ui.Dialog({
		title: __("Enable Depreciation — {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "total_number_of_depreciations",
				fieldtype: "Int",
				label: __("Number of Depreciations"),
				default: defaults.total_number_of_depreciations,
				reqd: 1,
			},
			{
				fieldname: "frequency_of_depreciation",
				fieldtype: "Int",
				label: __("Frequency (Months)"),
				default: defaults.frequency_of_depreciation || 1,
				reqd: 1,
			},
			{
				// §4.4 basis — when the asset went into service;
				// depreciation counts from this date.
				fieldname: "available_for_use_date",
				fieldtype: "Date",
				label: __("Available-for-Use Date"),
				default: defaults.available_for_use_date,
				reqd: 1,
			},
			{
				// §4.5 — when the first entry posts; days between the two
				// dates arrive as one catch-up entry. Default: category
				// setting, else end of the in-service month (core's rule).
				fieldname: "depreciation_start_date",
				fieldtype: "Date",
				label: __("Depreciation Posting Date"),
				default: defaults.depreciation_start_date || "Today",
				reqd: 1,
			},
			{
				fieldname: "expected_value_after_useful_life",
				fieldtype: "Currency",
				label: __("Salvage Value"),
				default: defaults.expected_value_after_useful_life || 0,
			},
			{
				fieldname: "finance_book",
				fieldtype: "Link",
				options: "Finance Book",
				label: __("Finance Book"),
				default: defaults.finance_book,
			},
		],
		primary_action_label: __("Enable"),
		primary_action(values) {
			frappe.call({
				method: "asset_enterprise.depreciation.enable_depreciation",
				args: { asset_name: frm.doc.name, ...values },
				callback: () => {
					d.hide();
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}

function partial_scrap_dialog(frm) {
	// v2.16 CH-09: composite assets may scrap a specific Active merged
	// component — offer them from the Merge Log.
	const components = (frm.doc.merge_log || [])
		.filter((r) => r.status === "Active")
		.map((r) => r.merged_source_asset);
	const d = new frappe.ui.Dialog({
		title: __("Partial Scrap — {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "scrapping_type",
				fieldtype: "Link",
				options: "Scrapping Type",
				label: __("Scrapping Type"),
				reqd: 1,
			},
			...(components.length
				? [
						{
							fieldname: "composite_component",
							fieldtype: "Select",
							options: [""].concat(components).join("\n"),
							label: __("Composite Component"),
							description: __(
								"Optional: scrap one merged component; its value at merge defaults the scrap value."
							),
						},
					]
				: []),
			{
				fieldname: "mode",
				fieldtype: "Select",
				options: "By Value\nBy Percentage",
				default: "By Value",
				label: __("Mode"),
			},
			{
				fieldname: "scrap_value",
				fieldtype: "Currency",
				label: __("Scrap Value"),
				depends_on: "eval:doc.mode==='By Value'",
			},
			{
				fieldname: "percentage",
				fieldtype: "Percent",
				label: __("Percentage of HAV"),
				depends_on: "eval:doc.mode==='By Percentage'",
			},
			{ fieldname: "scrap_date", fieldtype: "Date", label: __("Scrap Date"), default: "Today" },
		],
		primary_action_label: __("Post Partial Scrap"),
		primary_action(values) {
			frappe.call({
				method: "asset_enterprise.disposal.partial_scrap_asset",
				args: {
					asset_name: frm.doc.name,
					scrap_value: values.mode === "By Value" ? values.scrap_value : null,
					percentage: values.mode === "By Percentage" ? values.percentage : null,
					scrapping_type: values.scrapping_type,
					scrap_date: values.scrap_date,
					composite_component: values.composite_component || null,
				},
				callback: () => {
					d.hide();
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}

function post_final_row_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Post Final Depreciation Row — {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "note",
				fieldtype: "HTML",
				options: __(
					"Posts the last unposted schedule row. A row whose drift exceeds the " +
						"company tolerance requires the override below, approved by the " +
						"Tolerance Approver role (Asset Settings)."
				),
			},
			{
				fieldname: "override_tolerance",
				fieldtype: "Check",
				label: __("Override Tolerance (requires approver role)"),
				default: 0,
			},
		],
		primary_action_label: __("Post"),
		primary_action(values) {
			frappe.call({
				method: "asset_enterprise.depreciation.post_final_row",
				args: {
					asset_name: frm.doc.name,
					override_tolerance: values.override_tolerance ? 1 : 0,
				},
				callback: () => {
					d.hide();
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}
