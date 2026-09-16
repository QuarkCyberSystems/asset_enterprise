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

	// The Depreciation tab is core's own five-column table, not the
	// schedule grid, so a column added to the grid never reached the one
	// place the finance team actually reads (client, 16/09: "we need to
	// make it visible to the user"). Core calls frm.events.<this> from
	// its fetch callback and frappe binds the LAST handler registered
	// under a name, so defining it here replaces core's renderer with one
	// that also shows Days, the effective daily rate and — on a row an
	// event split — the rates actually used, one entry per stretch.
	render_depreciation_schedule_view(frm, asset_depr_schedule_doc) {
		const wrapper = $(frm.fields_dict["depreciation_schedule_view"].wrapper).empty();
		const money = (v) =>
			frappe.format(v, { fieldtype: "Currency", options: "Company:company:default_currency" });
		const split_rows = asset_depr_schedule_doc.depreciation_schedule.some((s) => s.rate_breakdown);

		const data = asset_depr_schedule_doc.depreciation_schedule.map((sch) => {
			const row = [
				sch.idx,
				frappe.format(sch.schedule_date, { fieldtype: "Date" }),
				sch.days_in_period || "",
				sch.daily_rate ? frappe.format(sch.daily_rate, { fieldtype: "Float", precision: 6 }) : "",
				money(sch.depreciation_amount),
				money(sch.accumulated_depreciation_amount),
				sch.journal_entry || "",
			];
			if (split_rows) row.push(sch.rate_breakdown || "");
			if (asset_depr_schedule_doc.shift_based) row.push(sch.shift);
			return row;
		});

		const columns = [
			{ name: __("No."), editable: false, resizable: false, format: (v) => v, width: 50 },
			{ name: __("Schedule Date"), editable: false, resizable: false, width: 110 },
			{ name: __("Days"), editable: false, resizable: false, width: 60 },
			{ name: __("Daily Rate (Effective)"), editable: false, resizable: false, width: 140 },
			{ name: __("Depreciation Amount"), editable: false, resizable: false, width: 150 },
			{ name: __("Accumulated Depreciation Amount"), editable: false, resizable: false, width: 170 },
			{
				name: __("Journal Entry"),
				editable: false,
				resizable: false,
				format: (v) => (v ? `<a href="/app/journal-entry/${v}">${v}</a>` : ""),
				width: 180,
			},
		];
		if (split_rows) {
			columns.push({
				name: __("Rate Breakdown"),
				editable: false,
				resizable: true,
				format: (v) => v,
				width: 460,
			});
		}
		if (asset_depr_schedule_doc.shift_based) {
			columns.push({ name: __("Shift"), editable: false, resizable: false, width: 59 });
		}

		// The generation's own basis — asset value, accumulated, NBV,
		// remaining days, rate — above the rows it produced (client,
		// 16/09: "there is no NBV and remaining days"). The stamp lives
		// on the schedule document; the table lives here.
		const d = asset_depr_schedule_doc;
		if (d.basis_daily_rate) {
			const fmt = (v) => money(v);
			const rate = frappe.format(d.basis_daily_rate, { fieldtype: "Float", precision: 6 });
			const from = frappe.format(d.repriced_from, { fieldtype: "Date" });
			const eol = frappe.format(d.basis_end_of_life, { fieldtype: "Date" });
			$(`<div class="ae-generation-basis" style="margin:0 0 10px 0;padding:10px 12px;border:1px solid var(--border-color);border-radius:6px;background:var(--bg-light-gray);font-size:var(--text-md);line-height:1.8">
				<div style="font-weight:600;margin-bottom:2px">${__("Generation Basis")} — ${d.name}
					<span style="font-weight:400;color:var(--text-muted)"> · ${__("re-priced from")} ${from} · ${__("end of life")} ${eol}</span></div>
				<div><b>${__("Asset Value")}</b> ${fmt(d.basis_hav)}
					&nbsp;−&nbsp; <b>${__("Accumulated")}</b> ${fmt(d.basis_accumulated)}
					&nbsp;=&nbsp; <b>${__("NBV")}</b> ${fmt(d.basis_nbv)}
					${d.basis_salvage ? `&nbsp;−&nbsp; <b>${__("Salvage")}</b> ${fmt(d.basis_salvage)}` : ""}
					&nbsp;=&nbsp; <b>${__("Depreciable Base")}</b> ${fmt(d.basis_depreciable_base)}</div>
				<div><b>${__("Remaining Days")}</b> ${d.basis_remaining_days}
					&nbsp;→&nbsp; <b>${__("Daily Rate")}</b> ${rate}
					<span style="color:var(--text-muted)"> (${fmt(d.basis_depreciable_base)} ÷ ${d.basis_remaining_days})</span></div>
			</div>`).appendTo(wrapper);
		}

		const datatable = new frappe.DataTable(wrapper.get(0), {
			columns,
			data,
			layout: "fluid",
			serialNoColumn: false,
			checkboxColumn: false,
			cellHeight: 35,
		});
		datatable.style.setStyle(".dt-scrollable", { "overflow-y": "hidden" });
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
