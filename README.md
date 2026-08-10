# Asset Enterprise

Enterprise Fixed Asset Management as a standalone Frappe app that overrides
the erpnext asset module. Zero erpnext source edits.

## What this app adds

- **Transaction Category Controller (TCC)** — every asset financial impact
  flows through `tcc.apply()` / `tcc.reverse()`, producing append-only
  **Financial Treatment** records with signed deltas and Asset Activity
  snapshots. Posted journal entries are never cancelled; every undo is a
  counter-document.
- **Ledger-derived asset values** — HAV / Accumulated Depreciation / NBV /
  Remaining Useful Life derived from the Financial Treatment ledger and the
  Active depreciation schedule (Enterprise tab on Asset), never mutated in
  place.
- **Daily-rate prospective depreciation** — unrounded daily rate, end-of-month
  rows, first-posting catch-up, prior-year-adjustment routing, cost-center
  split, final-row drift absorption at company currency precision. Schedule
  changes go through **supersession** (old schedule marked Superseded, posted
  rows preserved verbatim) instead of cancel-and-recreate.
- **Reversal family** — Reversal Asset Value Adjustment, Reversal Asset
  Repair (with stock return), Reversal of Capitalized Maintenance:
  same-doctype counter-documents that post mirror JEs and pair Financial
  Treatments so the value fold nets out while staying auditable.
- **Composite merge** — Capitalized Maintenance merges source assets into a
  composite through a Capitalization Clearing account (two-leg JE, nets to
  zero) with a value-snapshot Merge Log and bidirectional component↔parent
  links; fully-depreciated sources follow the configured treatment.
- **Disposal & recovery** — every scrap is a first-class **Scrap
  Transaction** document (composite assets can scrap a selected merged
  component at its value-at-merge), Scrapping Type loss-account routing
  (Scrapping Type → Asset Category Account override → Company default),
  partial scrap by value or percentage, three recovery paths: same-period
  restore, cross-period restore with catch-up depreciation, and Create
  Replacement Asset with two-way links. Reversals are gated by NBV
  coverage (VR-042).
- **PR/PI flows** — assets created at delivery (PR rate), PR over-allocation
  block, PI Asset Allocation (one submitted allocation per asset, qty-based),
  price/FX delta decomposition, automatic "Invoice Adjustment" value
  adjustment through the invoice-difference account, below-receipt
  warning with acknowledgement (Option B, default on).
- **Mass Asset Depreciation** with role-based authority for restricted
  modes; reports: Composite Merge Log, Replacement Chain, Asset Daily
  Reconciliation.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/QuarkCyberSystems/asset_enterprise
bench --site <site> install-app asset_enterprise
bench --site <site> migrate
```

Then open **Asset Settings** and tick **Enable Enterprise Assets**. With the
switch off, every override passes through to stock erpnext behavior.

## Go-live configuration checklist

| # | Setting | Where | Why |
|---|---------|-------|-----|
| 1 | Enable Enterprise Assets | Asset Settings | Master switch — all behavior gates on it |
| 2 | Tolerance rows (per company) | Asset Settings | Final-row drift flag threshold (design §4.10); falls back to 100× smallest currency fraction |
| 3 | Mass Depreciation Authority Roles | Asset Settings | Required for restricted mass-run modes (VR-006) |
| 4 | Enterprise accounts | Asset Category Account rows and/or Company `default_*` fields | Resolution chain (design §3.5): category override → company default → error. Includes Capitalization Clearing, Asset Invoice Difference, PYA Expense, Disposal accounts |
| 5 | Scrapping Type accounts | Scrapping Type (per reason, per company) | Authoritative disposal loss routing; 9 types seeded on install |
| 6 | `maintain_same_rate` = OFF | Buying Settings | Invoice Adjustment requires PI rate ≠ PR rate |
| 7 | `over_billing_allowance` headroom | Accounts Settings | Allows PI above PR for upward adjustments |
| 8 | `auto_create_assets` + `asset_naming_series` | Item (fixed-asset items) | PR-time asset creation |
| 9 | Asset Location on PR items | Purchase Receipt entry practice | Mandatory for auto asset creation |
| 10 | **`enable_immutable_ledger` = ON** | Accounts Settings | **Hard prerequisite** — with it off, core flags original GL entries cancelled on every mirror reversal (Repair / CM / restore). A migrate-time warning fires when violated |
| 11 | `daily_prorata_based` = 1 | Asset Finance Book rows (Badia default) | §4.3 daily-rate model |
| 12 | `calculate_depr_using_total_days` = 1 | Accounts Settings | §4.3 daily-rate model on core-generated schedules |
| 13 | "Asset" registered as Accounting Dimension | Accounting Dimension | GAP-023 — GL filtering/grouping by asset (manual config; fixture pending client decision) |

## Architecture

- [hooks.py](asset_enterprise/hooks.py) — 6 controller overrides
  (`override_doctype_class`), 2 whitelisted-method replacements
  (restore_asset, scrap_asset), Purchase Receipt / Purchase Invoice
  doc_events, Asset form JS.
- [overrides/patches.py](asset_enterprise/overrides/patches.py) — the only
  file with bench-upgrade risk: 3 wrap-and-delegate monkeypatches
  (post_depreciation_entries, reschedule_depreciation,
  get_gl_entries_on_asset_disposal). Target signatures are re-verified on
  **every migrate** — a bench update that moves a target fails the migrate
  loudly instead of silently dropping behavior.
- App-owned doctypes: Transaction Category, Scrapping Type, Asset Settings,
  Financial Treatment (append-only subledger), Composite Merge Log Entry,
  PI Asset Allocation, Mass Asset Depreciation, Scrap Transaction.

## Verification suite

Every build phase ships a savepoint-rolled-back end-to-end verification —
safe to run on a live site; nothing persists:

```bash
bench --site <site> execute asset_enterprise.setup.verify_all.run   # all 10 phase suites
bench --site <site> execute asset_enterprise.setup.verify_phase3.run  # single phase
```

## Contributing

This app uses `pre-commit` for code formatting and linting. Please
[install pre-commit](https://pre-commit.com/#installation) and enable it for
this repository:

```bash
cd apps/asset_enterprise
pre-commit install
```

### License

mit
