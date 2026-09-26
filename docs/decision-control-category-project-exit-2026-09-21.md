# D-031 — Control Category depreciation and project exit

**2026-09-22 update:** the client superseded the Control Category depreciation prohibition with full depreciation in one day. See [D-033](decision-control-category-one-day-2026-09-22.md). The project-exit decision stands. Text and results below preserve the prior decision.

Date: 2026-09-21. Scope: GA-0005-01, R135 / D-027(1) and R126 / D-013(1).
Status: **Accepted by the user; implemented locally with qcsfresh regression evidence below. Existing-site deployment remains pending.**
Authority: following the SAP comparison and proposed rules, the user stated: “ok accepting, and make a record of this decision”. This acceptance covers the two rules below; it is not sign-off of the entire v2.18 design or an assertion that the code conforms.

## Control Category — CH-36

A Control Category asset is expensed on acquisition and retained for physical control: register, custodian, location, count, movements and disposal.

- Recognize the eligible acquisition expense once.
- Refuse depreciation schedule generation and depreciation posting for Control Category assets, including attempts to enable depreciation on them.
- Project accounting may process the original eligible expense once under its normal eligibility rules. It must not create an additional depreciation-based project charge for that expensed asset. This is not a blanket exclusion of its original purchase expense from project settlement.
- Preserve historical postings. Existing Control Category assets with depreciation schedules/postings need an explicit migration review; this decision does not authorize deleting or rewriting posted entries.

This supersedes GAP-037's permission to depreciate Control Category assets and resolves the business choice in D-027(1). Implementation and cross-app source-reader verification are recorded below. Current expense-to-expense depreciation need not increase total company expense; the identified risk is duplicate project cost recognition.

## Project exit — CH-37

Transfers distinguish three intentions:

| Input | Meaning |
| --- | --- |
| Blank project field, no exit action | Keep the current project. |
| Selected project | Assign that project from the effective date. |
| Explicit **Leave project** action | Clear the project from the effective date. |

Maintain dated assignment history. A project exit effective 16 September attributes 1–15 September to the old project and 16 September onward to the applicable non-project cost centre. Historical postings and acquisition attribution remain unchanged. A selected destination project and Leave project are mutually exclusive.

This resolves D-013(1). Project assignment/exit is start-of-effective-day under this accepted example. D-009's existing end-of-day rule for cost-centre transfers is not silently changed; implementation must distinguish the two attribution boundaries, including a movement that changes both.

## Acceptance checks

1. Refuse Control Category depreciation through UI/API, schedule generation and posting paths; preserve normal-category behavior.
2. Prove project settlement recognizes an eligible Control Category purchase once without a second depreciation charge.
3. Prove blank transfer retains project, selected project changes assignment, and explicit exit clears it on the effective date.
4. Prove mid-period attribution, subsequent transfers, reversal/history behavior and unchanged acquisition attribution, including combined cost-centre/project movements.

## SAP research supporting the recommendation

SAP's mechanisms informed this design; these are local accepted rules, not claims that SAP has the same checkbox or transfer form.

- [Depreciation keys: no depreciation and immediate low-value-asset depreciation](https://help.sap.com/docs/SAP_S4HANA_CLOUD/a624a4a6d1d8473eb95fb42660127cfe/1e774ee3c5ef4e38926e1bfa4c02dde4.html).
- [Statistical budget monitoring and avoiding repeated settlement](https://help.sap.com/docs/SAP_ERP/d774ad24bc874914a10091b21c1eee6d/46a1c6535e601e4be10000000a174cb4.html).
- [Time-dependent asset assignment history](https://help.sap.com/docs/SAP_ERP/e054164e736046be90f4aec602391321/d63dde531ed3424de10000000a174cb4.html?version=6.17.latest).

## Implementation evidence — 2026-09-21

- Shared depreciation refusal is enforced by Asset validation (including submitted updates), schedule validation, the Enable Depreciation API/dialog defaults, individual scheduled-row posting and immediate-depreciation posting. Posted records are preserved.
- Asset Movement Item has a **Leave Project** checkbox, installed by normal custom-field migration. It clears registered Project/Project Accounting dimensions; other dimensions retain their existing rules. A destination project and exit cannot be submitted together. Destination company is checked when the project doctype carries a company.
- The attribution engine replays date-ordered project and cost-centre events separately, preserving the day-16/day-17 distinction in a combined movement. The Asset's acquisition dimensions are not mutated. Cancellation excludes the cancelled movement from attribution history.
- Project-only movements preserve custody and location; Receipt retains its explicit custody-return behavior.
- E-22 verifies the three GL expense segments for a combined project/cost-centre change. E-28 verifies Control Category enablement refusal, no residual schedule, ordinary disposal behavior, and exactly one eligible 12,000 purchase expense in project_accounting's actual source reader. E-37 covers blank retention, explicit exit, re-entry, cancellation, custody, standalone exit, contradictory inputs, amount conservation and posted GL with unchanged acquisition-project contra. E-38 refuses direct Asset validation, new schedules and legacy scheduled-row posting on a Control Category asset.
- No project_accounting implementation changes were needed; its original-expense source reader was exercised directly.
- Read-only badiav16 audit found **0** submitted Control Category assets with depreciation enabled or existing non-cancelled schedules. No ledger records were changed. The audit utility is `asset_enterprise.repair.audit_control_category_depreciation`.

The Leave Project field was installed on qcsfresh only. Other sites require the normal app migration before using it. The separate GL-keying/activation backlog, finance day-count decision and overall design-sign-off requirements remain open. This decision's implementation is not an overall conformance PASS.

Final verification: **13/13 phases PASS; 38/38 edges PASS; 8 unit tests PASS**. Literal suite: **52 PASS, 3 FAIL, 3 DEVIATION, 1 DOC, 1 MANUAL, 1 DEFERRED**; remaining FAILs TC-015/016/017 are the unchanged finance day-count issue. Python compilation and `git diff --check` pass. Committed 2026-09-26 (3ec356c).
