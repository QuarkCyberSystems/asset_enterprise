# D-033 — Control Category full depreciation in one day

Date: 2026-09-22. Authority: the user supplied the client's clarification and instructed implementation. This supersedes **only the Control Category depreciation prohibition in D-031 / CH-36**. The accepted project-exit rule CH-37 remains unchanged.

Client clarification:

> For the category it will depreciated in one day only all the amount will go expense, since user will put the asset account and accumulated will define as expense. Yes could be part of project settlement.

## Implemented locally

When depreciation is enabled, a Control Category asset uses one schedule row for the full remaining value, covering one day, with zero residual value. It retains the normal posting workflow and the selected depreciation posting date; this does not automatically submit a JE when the asset is created. Previously booked depreciation is not charged again.

- Removed yesterday's Control Category depreciation prohibition.
- Normalized finance-book inputs to a single charge and zero residual; schedule rows and Generation Basis show one day and the full daily amount.
- Applied the rule to initial asset creation and later Enable Depreciation, including the dialog defaults/explanation.
- Kept ordinary categories on their existing depreciation rules.
- Refused gradual or partial Control Category rows at posting; an old unposted schedule must be regenerated. Existing posted history is preserved.
- Kept category accounts on the Expense side. Cost, accumulated depreciation and depreciation expense must be distinct account names so their separate GL-derived balances can produce zero NBV after the charge. Expense account type alone does not imply they should share one account.
- Used a narrowly scoped creation preview while core creates a schedule before the acquisition GL is posted in the same submit transaction; subsequent operational balances come from the site's configured value model.

## Project settlement — source choice (ruled 2026-09-26)

The client approved eligibility for project settlement, but did not identify which debit is the settleable cost. The actual PA source reader currently accepts both the original acquisition expense and the one-day depreciation debit. Therefore implementing depreciation alone does **not** close the cross-app double-counting issue.

A pending question asks the user to choose:

1. Settle the one-day depreciation debit and exclude the original Control Category acquisition from settlement; or
2. Keep the acquisition expense as the source and exclude the depreciation debit.

No PA implementation changes have been made for this decision. Neither alternative is recorded as approved. Existing settled source amounts must be preserved/reconciled when implementing that choice. Do not roll this change out for project settlement until the source policy and its regression checks are complete.

## Verification

E-28 verifies a 12,000 full charge on the chosen day, zero NBV, idempotent repeat posting, expense-only accounts, physical disposal, and PA eligibility of the depreciation debit. E-38 verifies initial creation: a 36-period/500-residual input becomes one 6,000 charge covering one day, zero NBV, with gradual legacy posting refused. Both use rollback-only fixtures on qcsfresh.

Final suite results are recorded below after completion. Changes are local/uncommitted; no existing-site ledger migration or deployment has been performed.

Final suite results: **13/13 phases PASS, 38/38 edge cases PASS, 8 unit tests PASS**. Literal design suite: **52 PASS, 3 FAIL, 3 DEVIATION, 1 DOC, 1 MANUAL, 1 DEFERRED**; failures remain TC-015/016/017 (finance day-count basis). Python compilation, JS syntax and whitespace checks pass. The settlement source was ruled on 2026-09-26 (see below).

**2026-09-26 ruling (D-051):** option 2 — the acquisition expense is the settleable source. Every row carrying a Control Category asset on the category's accumulated-depreciation or depreciation-expense account (the one-day charge, its reversal and disposal legs) is excluded from project_accounting's source eligibility and refused when named by hand. Built in project_accounting `settlement/control_category.py`; edge case E-28 asserts the exclusion.
