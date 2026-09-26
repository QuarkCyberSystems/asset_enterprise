# GA-0005-01 pending-item remediation — 2026-09-21

**2026-09-22 update:** [D-033](decision-control-category-one-day-2026-09-22.md) supersedes the Control Category block below with full depreciation in one day. Project-settlement source selection is still pending. Earlier implementation evidence remains historical.

Follow-up to `a1e3b2a` in asset_enterprise. Overall conformance is still **FAIL** pending rollout/data repairs, business decisions, and design approval. This is implementation evidence, not an independent conformance verdict. No project_accounting code was changed.

## Implemented

- **R053/R161/R044:** added operational dimension-keyed GL calculations for posted assets, with independent fold and against-voucher diagnostics in Daily Reconciliation. Financial Treatment metadata cannot change the GL-derived monetary balance. Draft assets retain a preview. GL values require distinct cost and accumulated-depreciation accounts.
- **C1 rollout gate:** the new calculation requires `asset_enterprise_gl_values_ready=1` in site configuration. Existing sites retain the legacy calculation until their ledger keys are reviewed/repaired. Once enabled, missing keys never trigger a per-asset fallback. Enabled only on qcsfresh for verification; badiav16 has not been activated or revalued. R053 therefore remains open for existing-site rollout.
- **R174 / D-026:** CM and AVA reversals use the native JE mirror and retain reversal identity and attribution. Multi-JE CM treatment reversals link to their respective mirrored JE. Repair reversals preserve original GL rows, post their mirror under the reversal Repair voucher, and use the chosen date for GL and stock return. The invoice-difference attribution hook also respects reversal identity.
- **Fresh-site coverage:** E-22/E-24 create rollback-only project dimension fixtures. Control-category fixture uses separate cost/contra accounts; the Control Category business rule is unchanged. Added E-34 GL-versus-treatment independence, E-35 AVA reversal attribution/back-links, and E-36 Repair reversal date/voucher/GL preservation. E-32 now also checks native CM reversal links.
- **Test fidelity:** phase 11 t35 posts an actual acquisition-increase JE instead of a treatment-only increase; phase 7 checks that unkeyed purchase GL is excluded until its explicit backfill. TC-047d really fully depreciates its fixture; TC-047e stocks materials at the expected cost; TC-047g verifies missing treatment metadata cannot corrupt GL values and the audit-link backfill remains safe.
- **Unsigned design working copy:** registered R201/R202 as CH-34/35 with historical commit provenance; recorded R095/R027/R150; aligned TC-029/049 with CH-26, VR-024 precedence and §12.13 with existing rulings. Registered entries are not client approval. Signed documents and the existing DOCX export were not changed.

## Verification

- Complete phase sweep: **13/13 PASS**.
- Complete edge sweep: **36 PASS, no skips**.
- Complete literal design suite: **52 PASS, 3 FAIL, 3 DEVIATION, 1 DOC, 1 MANUAL, 1 DEFERRED**. The remaining failures are TC-015/016/017, awaiting finance confirmation of the depreciation basis. The deviations are TC-005/023/028 against their literal design expectations; changes to the unsigned working copy do not retroactively approve them.
- **8 unit tests PASS**, including the explicit site activation gate and failure/skip detection. Python compilation and whitespace checks pass.
- Operational suites ran on qcsfresh with rollback fixtures. The configuration flag is intentionally persisted on that disposable site. No production migration or ledger backfill was executed.

## Read-only existing-site preflight

On badiav16.localhost, the audit examined **303 submitted assets**: **96** have comparison/keying issues and **54** have no dimension-keyed cost-account entry. It found **507** unkeyed rows on category asset accounts: 153 Journal Entry, 49 Purchase Receipt, 305 Project Settlement Run. These are review candidates; an unkeyed account row is not automatically a defect for a particular asset.

Dry-run utilities identified:

- 45 acquisition legs with receipt/invoice line attribution candidates.
- 214 reference-keyed rows across 40 assets eligible for explicit dimension stamping; this scope includes counterpart accounts and is not additive to the 507 above.
- Four legacy acquisition candidates without line references: two attributable rows and two skipped. The two attributable rows reference the same receipt/asset, so their economic duplication needs review before applying any backfill.

No posted rows were changed. Review the candidates, reconcile source voucher totals and asset ownership, apply only validated repairs, rerun the audit, then explicitly activate the new model. The 305 Project Settlement Run candidates require cross-app/source review; no settlement allocation was guessed. Do not enable the GL switch on an unreviewed existing site.

Read-only commands:

```sh
bench --site <site> execute asset_enterprise.repair.audit_gl_value_keying
bench --site <site> execute asset_enterprise.repair.backfill_asset_dimension --kwargs '{"dry_run":1}'
bench --site <site> execute asset_enterprise.repair.backfill_asset_dimension_from_references --kwargs '{"dry_run":1}'
bench --site <site> execute asset_enterprise.repair.backfill_legacy_acquisition_rows --kwargs '{"dry_run":1}'
```

## Pending decisions and acceptance

1. **R135:** decision accepted in [D-031](decision-control-category-project-exit-2026-09-21.md): refuse Control Category depreciation; process the original eligible project expense once. Implemented and verified on qcsfresh; existing-site deployment pending.
2. **R126:** decision accepted in [D-031](decision-control-category-project-exit-2026-09-21.md): blank keeps project; explicit Leave project clears it from the effective date. Implemented and verified on qcsfresh; existing-site deployment pending.
3. **CH-12 / R021/R025/R026/R185:** finance confirmation of the depreciation day-count basis. No test expectations were changed to conceal these failures.
4. **R150:** stock-return valuation semantics still need confirmation; registering the difference is not a ruling.
5. Existing-site attribution cleanup, activation, migration and reconciliation; remaining built-untested/manual UAT evidence.
6. Approval/sign-off of the revised design, regenerated export, and independent trace/conformance rerun. Original r2 trace statuses were preserved.

Second-batch changes are local and uncommitted. The previously requested push of `a1e3b2a` remains blocked by automatic approval review; it has not been retried or bypassed.


## D-031 implementation follow-up

R135 and R126 are now implemented locally; see [decision and evidence](decision-control-category-project-exit-2026-09-21.md). The phase checks pass after correcting the Enable Depreciation endpoint registration; the edge suite now includes E-37/E-38. Literal design cases remain 52 PASS / 3 FAIL / 3 DEVIATION / 1 DOC / 1 MANUAL / 1 DEFERRED, with the same finance-basis failures. The Control Category source-reader test exercises project_accounting without modifying that app. No posted legacy data was rewritten.
