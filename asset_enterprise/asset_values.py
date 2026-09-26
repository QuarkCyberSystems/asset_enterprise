"""GL-derived posted asset values — GAP-006 / §5.1.

Sites explicitly enabled after the C1 keying audit read submitted/cancelled
asset cost and accumulated depreciation from
posted GL keyed by the asset accounting dimension. Missing keys are
reported separately; reference keys never silently enter the value math.
Drafts have no posting yet and show the treatment-based preview. The old
fold remains a separately named diagnostic and the legacy calculation
until site_config.asset_enterprise_gl_values_ready is enabled.
"""

import frappe
from frappe.utils import cint, date_diff, flt, month_diff, nowdate

from asset_enterprise.rounding import fa_module_round


def calendar_remaining_life_months(asset):
	"""C33: original UL − elapsed months since the posting-basis date.
	Calendar time to the end of life — what a merged component still
	brings to a composite (CH-06 Merge Log snapshot keeps THIS meaning).
	"""
	fb = frappe.db.get_value(
		"Asset Finance Book",
		{"parent": asset.name},
		["total_number_of_depreciations", "frequency_of_depreciation", "depreciation_start_date"],
		as_dict=True,
	)
	if not fb or not fb.total_number_of_depreciations:
		return 0.0

	# The finance book's period count is ALREADY re-written by a Useful
	# Life Adjustment (TC-025 expects total UL = 48 there), so folding
	# the treatment's life delta on top counted every adjustment twice.
	total_months = flt(fb.total_number_of_depreciations) * flt(fb.frequency_of_depreciation or 1)
	start = fb.depreciation_start_date or asset.available_for_use_date
	elapsed = month_diff(nowdate(), start) - 1 if start else 0
	elapsed = max(0, elapsed)
	return max(0.0, flt(total_months - elapsed, 2))


def _remaining_life_months(asset, life_delta_months):
	"""Remaining Useful Life as DISPLAYED: what is left to POST (client,
	18/08 — "base it on the depreciated posting"). A backdated asset
	whose calendar life already ended still shows the periods finance
	has not booked yet, and the figure counts down to zero as entries
	post. Assets with no Active schedule (depreciation not enabled yet)
	fall back to the calendar measure."""
	has_schedule = frappe.db.exists(
		"Asset Depreciation Schedule",
		{"asset": asset.name, "status": "Active", "docstatus": 1},
	)
	if not has_schedule:
		return calendar_remaining_life_months(asset)
	unposted = frappe.db.sql(
		"""
		select count(*) from `tabDepreciation Schedule` ds
		join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
		where ads.asset = %s and ads.status = 'Active' and ads.docstatus = 1
		  and ifnull(ds.journal_entry, '') = ''
		""",
		asset.name,
	)[0][0]
	return flt(unposted)


def fold_asset_values(asset_name):
	"""Treatment-based preview/audit; never writes operational values."""
	asset = frappe.get_doc("Asset", asset_name)
	values = AssetValueBatch([asset_name]).fold(asset_name)
	rul_months = _remaining_life_months(asset, 0)
	values.update(
		remaining_useful_life_months=rul_months,
		remaining_useful_life_years=flt(rul_months / 12, 2),
	)
	return values


def recalculate_asset_values(asset_name, save=True):
	"""Use GL on audited, explicitly enabled sites; drafts remain previews.

	The rollout switch is site-wide, never a per-asset fallback for missing
	keys. Legacy sites must complete the C1 audit/backfill before activation.
	"""
	asset = frappe.get_doc("Asset", asset_name)
	from asset_enterprise.depreciation import enterprise_enabled

	if asset.docstatus == 0 or not enterprise_enabled() or not cint(
		frappe.conf.get("asset_enterprise_gl_values_ready", 0)
	):
		values = fold_asset_values(asset_name)
	else:
		values = gl_asset_values(asset_name)
		rul = _remaining_life_months(asset, 0)
		values.update(remaining_useful_life_months=rul, remaining_useful_life_years=flt(rul / 12, 2))
	if save:
		frappe.db.set_value("Asset", asset_name, values, update_modified=False)
		_sync_core_bookkeeping(asset, values["net_book_value"], values["accumulated_depreciation_value"])
	return values


def _sync_core_bookkeeping(asset, nbv, accum):
	"""Keep core's own counters honest against the derived values.

	Core decrements finance_books.value_after_depreciation on every
	depreciation JE and its status logic reads THAT counter — but no
	core code credits it on our value events (TCC additions, invoice
	adjustments, revaluations). On ACC-ASS-2026-00125 a +100,000
	invoice adjustment left the counter 100,000 short, it went negative
	one row before the end, and core declared "Fully Depreciated" while
	6,162.30 was still unposted. The derived NBV is the authority —
	overwrite the counter with it after every recalculation, and
	restate the depreciation-lifecycle status from it.
	"""
	if asset.docstatus != 1 or not asset.calculate_depreciation:
		return
	frappe.db.set_value(
		"Asset Finance Book",
		{"parent": asset.name, "parenttype": "Asset"},
		"value_after_depreciation",
		flt(nbv),
		update_modified=False,
	)
	# Only the depreciation-lifecycle statuses may be restated — never
	# Disposed / Scrapped / Sold / Capitalized, which our flows own.
	if asset.status not in ("Submitted", "Partially Depreciated", "Fully Depreciated"):
		return
	salvage = flt(
		frappe.db.get_value(
			"Asset Finance Book", {"parent": asset.name}, "expected_value_after_useful_life"
		)
		or 0
	)
	if flt(nbv) <= salvage + 0.005:
		status = "Fully Depreciated"
	elif flt(accum) > 0:
		status = "Partially Depreciated"
	else:
		status = "Submitted"
	if status != asset.status:
		frappe.db.set_value("Asset", asset.name, "status", status, update_modified=False)


def assert_nbv_covers_reversal(asset_name, amount, context=None):
	"""VR-042 (2026-07-23 review): a reversal that reduces asset value
	is blocked when the current NBV cannot cover the amount being
	reversed — it would drive NBV negative / below salvage."""
	from frappe import _

	amount = flt(amount)
	if amount <= 0:
		return
	nbv = flt(recalculate_asset_values(asset_name, save=False)["net_book_value"])
	if amount > nbv + 0.005:
		frappe.throw(
			_(
				"Reversal amount {0} cannot be covered by the current Net Book Value "
				"{1} of Asset {2}{3}. The reversal is not allowed (VR-042) — handle "
				"the correction via Asset Value Adjustment."
			).format(amount, nbv, asset_name, f" ({context})" if context else ""),
			title=_("Reversal Not Covered by NBV"),
		)



def gl_asset_values(asset_name):
	"""C1/D1: strictly dimension-keyed posted balances; never infer a key."""
	return AssetValueBatch([asset_name]).gl(asset_name)


def reference_gl_asset_values(asset_name):
	"""Independent reference-key comparison for detecting attribution gaps."""
	return AssetValueBatch([asset_name]).reference(asset_name)


class AssetValueBatch:
	"""The three value readings — the treatment fold, the dimension-keyed
	GL balances and the reference-keyed GL balances — for a SET of assets
	in a fixed number of set-based queries (RULES §4: no per-asset query
	loop on an Asset / GL path). The single-asset functions above are a
	batch of one, so the arithmetic exists once.

	Remaining useful life is not part of it: no set reader needs it, and
	`fold_asset_values` adds it for the one asset it is asked about.
	"""

	def __init__(self, asset_names):
		self.names = tuple(dict.fromkeys(n for n in asset_names if n))
		self.assets = {}
		if not self.names:
			return
		params = {"assets": self.names}
		self.assets = {
			a.name: a
			for a in frappe.db.sql(
				"""select name, company, asset_category, net_purchase_amount,
				          opening_accumulated_depreciation
				   from `tabAsset` where name in %(assets)s""",
				params,
				as_dict=True,
			)
		}
		self.accounts = {
			(r.parent, r.company_name): r
			for r in frappe.db.sql(
				"""select parent, company_name, fixed_asset_account, accumulated_depreciation_account
				   from `tabAsset Category Account`
				   where parenttype = 'Asset Category'
				     and parent in (select asset_category from `tabAsset` where name in %(assets)s)""",
				params,
				as_dict=True,
			)
		}
		# `_posted_ft_sums`, grouped
		self.ft = {
			r.asset: r
			for r in frappe.db.sql(
				"""select asset,
				          coalesce(sum(hav_delta), 0) as hav_delta,
				          coalesce(sum(case when source_doctype = 'Asset Depreciation Schedule'
				                            then 0 else accum_delta end), 0) as accum_delta
				   from `tabFinancial Treatment`
				   where asset in %(assets)s and status = 'Posted'
				     and ifnull(reversal_reference, '') = ''
				   group by asset""",
				params,
				as_dict=True,
			)
		}
		# `_posted_depreciation_total` and `_counted_vouchers`' schedule half:
		# one read of every generation's rows
		self.posted_depreciation, self.counted = {}, {}
		for asset, status, docstatus, je, rev, amount in frappe.db.sql(
			"""select ads.asset, ads.status, ads.docstatus, ds.journal_entry,
			          ds.reversal_journal_entry, ds.depreciation_amount
			   from `tabDepreciation Schedule` ds
			   join `tabAsset Depreciation Schedule` ads on ds.parent = ads.name
			   where ads.asset in %(assets)s""",
			params,
		):
			self.counted.setdefault(asset, set()).update(x for x in (je, rev) if x)
			if status == "Active" and docstatus == 1 and je and not rev:
				self.posted_depreciation[asset] = self.posted_depreciation.get(asset, 0.0) + flt(amount)
		# `_counted_vouchers`' treatment half
		for asset, je, voucher in frappe.db.sql(
			"""select asset, journal_entry, voucher_no from `tabFinancial Treatment`
			   where asset in %(assets)s""",
			params,
		):
			self.counted.setdefault(asset, set()).update(x for x in (je, voucher) if x)
		# reference-keyed rows (`against_voucher`), per voucher so the fold
		# can drop the counted ones; the reference balance sums them all
		self.reference_rows = {}
		for asset, company, account, voucher_no, balance in frappe.db.sql(
			"""select against_voucher, company, account, voucher_no, sum(debit - credit)
			   from `tabGL Entry`
			   where is_cancelled = 0 and against_voucher_type = 'Asset'
			     and against_voucher in %(assets)s
			   group by against_voucher, company, account, voucher_no""",
			params,
		):
			self.reference_rows.setdefault(asset, []).append((company, account, voucher_no, flt(balance)))
		self._dimension_rows = None  # read on the first `gl()` — the fold never needs it

	def _asset(self, name):
		asset = self.assets.get(name)
		if not asset:
			frappe.throw(frappe._("Asset {0} not found").format(name), frappe.DoesNotExistError)
		return asset

	def category_accounts(self, asset):
		return self.accounts.get((asset.asset_category, asset.company)) or frappe._dict({})

	def fold(self, name):
		"""`fold_asset_values` without remaining life."""
		asset = self._asset(name)
		company = asset.company
		ft = self.ft.get(name) or frappe._dict(hav_delta=0, accum_delta=0)
		manual_hav, manual_accum = self._manual_gl_adjustments(asset)
		hav = fa_module_round(
			flt(asset.net_purchase_amount) + flt(ft.hav_delta) + flt(manual_hav), company
		)
		accum = fa_module_round(
			flt(asset.opening_accumulated_depreciation)
			+ flt(self.posted_depreciation.get(name))
			+ flt(ft.accum_delta)
			+ flt(manual_accum),
			company,
		)
		return {
			"historical_asset_value": hav,
			"accumulated_depreciation_value": accum,
			"net_book_value": fa_module_round(hav - accum, company),
		}

	def _manual_gl_adjustments(self, asset):
		"""§5.1 / TC-010: the values are LEDGER-derived. A journal entry
		posted straight to the asset's fixed-asset or accumulated-
		depreciation account — outside the schedule and outside any
		Financial Treatment — still moves the asset's value, and the
		Recalculate button must pick it up."""
		accounts = self.category_accounts(asset)
		fa, accum = accounts.get("fixed_asset_account"), accounts.get("accumulated_depreciation_account")
		if not (fa or accum):
			return 0.0, 0.0
		counted = self.counted.get(asset.name, set())
		hav_delta = accum_delta = 0.0
		for _company, account, voucher_no, balance in self.reference_rows.get(asset.name, ()):
			if voucher_no in counted or account not in (fa, accum):
				continue
			if account == fa:
				hav_delta += balance
			else:
				accum_delta -= balance
		return hav_delta, accum_delta

	def acquisition_key_missing(self, name):
		"""No live GL row keyed to the asset on its fixed-asset account."""
		asset = self._asset(name)
		fa = self.category_accounts(asset).get("fixed_asset_account")
		self.gl(name)
		return not any(
			company == asset.company and account == fa
			for company, account, _balance in self._dimension_rows.get(name, ())
		)

	def gl(self, name):
		"""C1/D1: strictly dimension-keyed posted balances."""
		if self._dimension_rows is None:
			self._dimension_rows = {}
			for asset, company, account, balance in frappe.db.sql(
				"""select asset, company, account, sum(debit - credit)
				   from `tabGL Entry`
				   where is_cancelled = 0 and asset in %(assets)s
				   group by asset, company, account""",
				{"assets": self.names},
			):
				self._dimension_rows.setdefault(asset, []).append((company, account, flt(balance)))
		return self._balances(name, self._dimension_rows.get(name, ()))

	def reference(self, name):
		"""Reference-keyed balances, for detecting attribution gaps."""
		return self._balances(
			name,
			[(company, account, balance) for company, account, _v, balance in self.reference_rows.get(name, ())],
		)

	def _balances(self, name, rows):
		asset = self._asset(name)
		accounts = self.category_accounts(asset)
		fa = accounts.get("fixed_asset_account")
		accum = accounts.get("accumulated_depreciation_account")
		if fa and fa == accum:
			frappe.throw("Fixed Asset and Accumulated Depreciation accounts must be distinct "
				"to derive asset balances from GL. Review the asset category accounts.")
		by_account = {}
		for company, account, balance in rows:
			if company == asset.company and account in (fa, accum):
				by_account[account] = by_account.get(account, 0.0) + balance
		hav = fa_module_round(by_account.get(fa, 0), asset.company)
		accumulated = fa_module_round(-by_account.get(accum, 0), asset.company)
		return {"historical_asset_value": hav, "accumulated_depreciation_value": accumulated,
			"net_book_value": fa_module_round(hav - accumulated, asset.company)}
