"""Which cost lines a capitalizing document consumes.

A capitalized Asset Repair names its cost by invoice row (a Purchase
Invoice and an expense account) and by consumed stock; a Capitalized
Maintenance or Standard Capitalization service row names an expense
account and the Purchase Invoice the service was bought on. None of them
names the LINE. Core posts one credit per repair invoice row and then
merges credits on one account, so a repair spanning two lines of one
invoice, or two invoices on one account, landed as one credit that a
reader had to guess a line for.

This module decides the lines, once, at submit, and records them on the
document (`consumed_cost_lines`, child doctype Asset Consumed Cost Line):

    an invoice's lines on the account, one per (invoice row, cost centre),
    each at its live net: the invoice's own rows less its returns and
    debit notes (a valuation Create Cancellation of an invoice is a
    return too), followed through `purchase_invoice_item`;
    ranked: a line of the consumer's PROJECT first (the project
    dimensions are the ones whose doctype an installed app declares
    through `asset_project_dimension_doctypes`, plus core's Project), then
    the other matching dimensions, then the consumer's cost centre, then
    the earliest;
    each line capped at its net less what other submitted consumers took
    from it - a repair submitted before lines were recorded included (the
    upgrade records its lines, `record_legacy_repairs`); what cannot be
    placed is refused;
    a repair's consumed stock: one line per row of the Material Issue core
    raises for the repair, recorded when the issue exists (on submit).

Each consumed line then gets ONE credit, whose voucher row is the consumed
line's record row (the repair's own GL, the capitalization's GL, or the
service Journal Entry's row), and whose dimensions are the consumed line's:
capitalizing a cost takes it off the dimension it was booked on. The cost
centre and the asset dimension stay the consumer's (core's and GAP-023's
attribution); the asset debit keeps the consumer's dimensions (the asset
is not a project cost line).

The record is the owner's statement of what it consumed; a reader (the
project ledger) follows the credit's voucher row to it and never infers a
line.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

LINE_DOCTYPE = "Asset Consumed Cost Line"
TABLE = "consumed_cost_lines"
CONSUMERS = ("Asset Repair", "Asset Capitalization")


def _dimensions():
	from asset_enterprise.gl_attribution import dimension_fields

	return [f for f in dimension_fields("GL Entry") if f != "cost_center"]


def project_fields():
	"""The GL dimensions that are a PROJECT: core's Project plus the
	doctypes an installed app declares (`asset_project_dimension_doctypes`)
	- named by the declaring app, never here."""
	from asset_enterprise.depreciation import project_dimension_doctypes

	meta = frappe.get_meta("GL Entry")
	doctypes = project_dimension_doctypes()
	return [f for f in _dimensions() if meta.get_field(f) and meta.get_field(f).options in doctypes]


def _round(amount, company):
	from asset_enterprise.rounding import fa_module_round

	return fa_module_round(amount, company)


# ------------------------------------------------------------------ lines
def _returns_of(purchase_invoice):
	"""{return item row: the invoice's item row it returns} over every
	return or debit note of the invoice - a return of a return included -
	and the returns' names. A handful of queries per invoice."""
	names, frontier = [], [purchase_invoice]
	while frontier:
		frontier = [
			n for n in frappe.db.sql_list(
				"""select name from `tabPurchase Invoice`
				where is_return = 1 and docstatus > 0 and return_against in %s""",
				(tuple(frontier),),
			)
			if n not in names and n != purchase_invoice
		]
		names += frontier
	if not names:
		return {}, []
	link = dict(frappe.db.sql(
		"""select name, purchase_invoice_item from `tabPurchase Invoice Item`
		where parent in %s and parenttype = 'Purchase Invoice'""",
		(tuple(names),),
	))
	resolved = {}
	for row in link:
		target, seen = link.get(row), {row}
		while target in link and target not in seen:
			seen.add(target)
			target = link.get(target)
		resolved[row] = target
	return resolved, names


def invoice_lines(purchase_invoice, account):
	"""The invoice's live lines on `account`: one per (invoice row, cost
	centre, dimensions), earliest first, each at its live net - its own
	rows less its returns' rows on that invoice row (M-3)."""
	dims = _dimensions()
	cols = "".join(f", gl.`{d}`" for d in dims)
	lines = frappe.db.sql(
		f"""select gl.voucher_detail_no, gl.cost_center {cols},
		       sum(gl.debit - gl.credit) as amount, min(concat(gl.creation, '|', gl.name)) as first
		from `tabGL Entry` gl
		where gl.voucher_type = 'Purchase Invoice' and gl.voucher_no = %s and gl.account = %s
		  and gl.is_cancelled = 0
		group by gl.voucher_detail_no, gl.cost_center {cols}
		having sum(gl.debit - gl.credit) > 0
		order by first""",
		(purchase_invoice, account),
		as_dict=True,
	)
	returned_rows, returns = _returns_of(purchase_invoice)
	if returns and lines:
		for detail, cost_center, amount in frappe.db.sql(
			"""select voucher_detail_no, cost_center, sum(debit - credit) from `tabGL Entry`
			where voucher_type = 'Purchase Invoice' and voucher_no in %s and account = %s and is_cancelled = 0
			group by voucher_detail_no, cost_center""",
			(tuple(returns), account),
		):
			origin = returned_rows.get(detail)
			if not origin or not flt(amount):
				continue
			same = [ln for ln in lines if ln.voucher_detail_no == origin]
			target = next((ln for ln in same if ln.cost_center == cost_center), same[0] if same else None)
			if target:
				target.amount = flt(target.amount) + flt(amount)
	return [ln for ln in lines if flt(ln.amount) > 0]


def consumed_elsewhere(purchase_invoice, account, exclude_parent=None):
	"""{(invoice row, cost centre): amount} other SUBMITTED consumers took
	from the invoice's lines on `account` - one query. A cancelled consumer
	(its reversal document stands) has given its lines back."""
	if not frappe.db.table_exists(LINE_DOCTYPE):
		return {}
	joins = " ".join(
		f"left join `tab{dt}` p{i} on l.parenttype = '{dt}' and p{i}.name = l.parent"
		for i, dt in enumerate(CONSUMERS)
	)
	status = "coalesce(" + ", ".join(f"p{i}.docstatus" for i in range(len(CONSUMERS))) + ")"
	return {
		(detail or None, cc or None): flt(amount)
		for detail, cc, amount in frappe.db.sql(
			f"""select l.purchase_invoice_item, l.cost_center, sum(l.amount)
			from `tab{LINE_DOCTYPE}` l {joins}
			where l.purchase_invoice = %s and l.expense_account = %s and l.parent != %s
			  and {status} = 1
			group by l.purchase_invoice_item, l.cost_center""",
			(purchase_invoice, account, exclude_parent or ""),
		)
	}


def _rank(line, dims, cost_center, projects):
	"""Project first (R231), then the other matching dimensions, then the
	consumer's cost centre, then the earliest line."""
	project = sum(1 for d in projects if dims.get(d) and line.get(d) == dims.get(d))
	other = sum(1 for d, v in dims.items() if d not in projects and v and line.get(d) == v)
	return (-project, -other, 0 if cost_center and line.cost_center == cost_center else 1, line.first)


def plan(requests, company, exclude_parent=None, prior=None):
	"""The line rows `requests` consume: ([row dict], [(request, amount not
	placed)]). `requests` are (source row, purchase invoice, expense
	account, amount, {dimension: value}, cost centre); `prior` holds
	{(invoice, account, invoice row, cost centre): amount} taken by
	consumers not yet recorded (the upgrade's earlier legacy repairs). The
	lines and other consumers are read once per (invoice, account)."""
	rows, short = [], []
	taken = dict(prior or {})
	projects = project_fields()
	lines_of, elsewhere_of = {}, {}
	for request in requests:
		source_row, invoice, account, amount, dims, cost_center = request
		remaining = _round(amount, company)
		if remaining <= 0:
			continue
		if (invoice, account) not in lines_of:
			lines_of[(invoice, account)] = invoice_lines(invoice, account)
			elsewhere_of[(invoice, account)] = consumed_elsewhere(invoice, account, exclude_parent=exclude_parent)
		elsewhere = elsewhere_of[(invoice, account)]
		for line in sorted(lines_of[(invoice, account)], key=lambda ln: _rank(ln, dims, cost_center, projects)):
			key = (invoice, account, line.voucher_detail_no or None, line.cost_center or None)
			capacity = flt(line.amount) - elsewhere.get(key[2:], 0) - taken.get(key, 0)
			take = _round(min(capacity, remaining), company)
			if take <= 0:
				continue
			rows.append({
				"source_row": source_row,
				"purchase_invoice": invoice,
				"purchase_invoice_item": line.voucher_detail_no,
				"expense_account": account,
				"cost_center": line.cost_center,
				"amount": take,
				"dimensions": json.dumps({d: line.get(d) for d in _dimensions()}, sort_keys=True),
			})
			taken[key] = taken.get(key, 0) + take
			remaining = _round(remaining - take, company)
			if remaining <= 0:
				break
		if remaining > 0:
			short.append((request, remaining))
	return rows, short


def allocate(doc, requests):
	"""Fill `doc.consumed_cost_lines` from `requests` (see `plan`) - one
	line row per consumed invoice line. Refuses an amount the invoice's
	lines on that account cannot cover."""
	doc.set(TABLE, [row for row in (doc.get(TABLE) or []) if row.get("stock_entry")])
	if not requests:
		return
	rows, short = plan(requests, doc.company, exclude_parent=doc.name)
	for row in rows:
		doc.append(TABLE, row)
	for (_source_row, invoice, account, amount, _dims, _cc), remaining in short:
		frappe.throw(
			_(
				"{0} capitalizes {1} of Purchase Invoice {2} on {3}, but only {4} of that invoice's "
				"cost on the account is left to capitalize (its returns and debit notes, and other "
				"repairs and capitalizations, took the rest). Reduce the amount or name another invoice."
			).format(
				_(doc.doctype),
				frappe.format(amount, "Currency"),
				invoice,
				account,
				frappe.format(flt(amount) - remaining, "Currency"),
			),
			title=_("Invoice Cost Already Capitalized"),
		)


# ------------------------------------------------------------ stock lines
def record_stock_lines(repair, stock_entry):
	"""The lines of the repair's consumed-stock Material Issue, one per
	issue row with a value, recorded on the (submitted) repair: the row's
	expense account, centre and dimensions as the issue's GL booked them
	(a row the issue booked without dimensions consumes none). Rows already
	recorded (a rebuild at cancel) are returned as they are."""
	existing = [row for row in (repair.get(TABLE) or []) if row.get("stock_entry")]
	if existing or not stock_entry or repair.docstatus != 1:
		return existing
	details = frappe.get_all("Stock Entry Detail", filters={"parent": stock_entry, "parenttype": "Stock Entry"},
		fields=["name", "expense_account", "amount", "cost_center"], order_by="idx")
	dims = _dimensions()
	booked = {}
	accounts = tuple({d.expense_account for d in details if d.expense_account})
	if accounts:
		cols = "".join(f", `{d}`" for d in dims)
		for row in frappe.db.sql(
			f"""select voucher_detail_no, account, cost_center {cols} from `tabGL Entry`
			where voucher_type = 'Stock Entry' and voucher_no = %s and account in %s
			  and is_cancelled = 0 and debit > credit order by creation, name""",
			(stock_entry, accounts),
			as_dict=True,
		):
			booked.setdefault((row.voucher_detail_no, row.account), row)
	default_expense = None
	out = []
	for detail in details:
		if flt(detail.amount) <= 0:
			continue
		account = detail.expense_account
		if not account:
			default_expense = default_expense or frappe.get_cached_value("Company", repair.company, "default_expense_account")
			account = default_expense
		gl = booked.get((detail.name, account)) or {}
		row = repair.append(TABLE, {
			"source_row": detail.name,
			"stock_entry": stock_entry,
			"stock_entry_detail": detail.name,
			"expense_account": account,
			"cost_center": gl.get("cost_center") or detail.cost_center,
			"amount": flt(detail.amount),
			"dimensions": json.dumps({d: gl.get(d) for d in dims}, sort_keys=True),
			"gl_voucher_type": repair.doctype,
			"gl_voucher_no": repair.name,
		})
		row.name = frappe.generate_hash(length=10)
		row.gl_voucher_detail_no = row.name
		row.docstatus = 1
		row.db_insert()
		out.append(row)
	return out


# -------------------------------------------------------- legacy repairs
def _legacy_repairs():
	"""Submitted capitalizing repairs with invoice rows and no recorded
	invoice line - submitted before this record existed - oldest first.
	Readable before the record's table exists (the upgrade census)."""
	if not frappe.db.table_exists("Asset Repair Purchase Invoice"):
		return []
	reversal = "and ifnull(ar.transaction_type, '') != 'Reversal'" if frappe.db.has_column(
		"Asset Repair", "transaction_type") else ""
	recorded = f"""and not exists (select 1 from `tab{LINE_DOCTYPE}` l where l.parent = ar.name
		      and l.parenttype = 'Asset Repair' and ifnull(l.purchase_invoice, '') != '')""" if frappe.db.table_exists(
		LINE_DOCTYPE) else ""
	return frappe.db.sql_list(
		f"""select ar.name from `tabAsset Repair` ar
		where ar.docstatus = 1 and ar.capitalize_repair_cost = 1 {reversal}
		  and exists (select 1 from `tabAsset Repair Purchase Invoice` ari where ari.parent = ar.name
		      and ari.parenttype = 'Asset Repair' and ifnull(ari.purchase_invoice, '') != '')
		  {recorded}
		order by ar.creation, ar.name"""
	)


def legacy_allocation():
	"""{repair: {"rows": [line row dict], "short": [(invoice, account,
	amount not placed)]}} for every legacy repair (`_legacy_repairs`),
	decided by the same rule as a new repair, oldest first, each seeing the
	lines the earlier ones took. Read-only: `record_legacy_repairs` writes
	it; the project ledger's upgrade census reads it before that ran."""
	out, prior = {}, {}
	dims = _dimensions()
	for name in _legacy_repairs():
		repair = frappe.db.get_value("Asset Repair", name, ["name", "company", "cost_center", *dims], as_dict=True)
		header = {d: repair.get(d) for d in dims}
		requests = [
			(row.name, row.purchase_invoice, row.expense_account, flt(row.repair_cost), header, repair.cost_center)
			for row in frappe.get_all("Asset Repair Purchase Invoice", filters={"parent": name,
				"parenttype": "Asset Repair"}, fields=["name", "purchase_invoice", "expense_account", "repair_cost"],
				order_by="idx")
			if row.purchase_invoice and row.expense_account and flt(row.repair_cost)
		]
		rows, short = plan(requests, repair.company, exclude_parent=name, prior=prior)
		for row in rows:
			key = (row["purchase_invoice"], row["expense_account"], row["purchase_invoice_item"] or None,
				row["cost_center"] or None)
			prior[key] = prior.get(key, 0) + flt(row["amount"])
		out[name] = {"rows": rows, "short": [(r[1], r[2], flt(remaining)) for r, remaining in short]}
	return out


def record_legacy_repairs():
	"""Write `legacy_allocation` onto the repairs (the upgrade patch): each
	row is marked `recorded_at_upgrade` and names the repair's voucher but
	no GL row - its GL still carries core's merged credit. Idempotent: a
	recorded repair is no longer legacy. Returns the allocation."""
	allocation = legacy_allocation()
	for name, plan_ in allocation.items():
		for idx, values in enumerate(plan_["rows"], start=1):
			row = frappe.get_doc({
				"doctype": LINE_DOCTYPE,
				"parent": name,
				"parenttype": "Asset Repair",
				"parentfield": TABLE,
				"idx": 1000 + idx,
				"docstatus": 1,
				"recorded_at_upgrade": 1,
				"gl_voucher_type": "Asset Repair",
				"gl_voucher_no": name,
				**values,
			})
			row.db_insert()
	return allocation


# ---------------------------------------------------------------- helpers
def line_dimensions(line):
	"""The consumed line's dimensions for its credit (cost centre and asset
	stay the consumer's)."""
	try:
		values = json.loads(line.get("dimensions") or "{}")
	except ValueError:
		values = {}
	return {d: values.get(d) for d in _dimensions()}


def mark_voucher(line, voucher_type, voucher_no, voucher_detail_no):
	"""Record the GL voucher row that carries this line's credit - what a
	reader follows from the credit back to the line."""
	frappe.db.set_value(
		LINE_DOCTYPE,
		line.name,
		{"gl_voucher_type": voucher_type, "gl_voucher_no": voucher_no, "gl_voucher_detail_no": voucher_detail_no},
		update_modified=False,
	)
	line.gl_voucher_type, line.gl_voucher_no, line.gl_voucher_detail_no = voucher_type, voucher_no, voucher_detail_no


def invoice_lines_of(doc):
	"""The recorded invoice lines whose credits this document posts: a
	legacy repair's upgrade record posts none (core's merged credit)."""
	return [
		line for line in (doc.get(TABLE) or [])
		if line.get("purchase_invoice") and not line.get("recorded_at_upgrade")
	]


def lines_for(doc, source_row):
	return [line for line in invoice_lines_of(doc) if line.source_row == source_row]


def header_dimensions(doc):
	return {d: doc.get(d) for d in _dimensions()}


# ------------------------------------------ service rows that must consume
def accounts_with_project_lines(company, accounts):
	"""Of `accounts`, those that carry a live project cost line in
	`company`: a live debit booked on a project dimension - one query."""
	projects = project_fields()
	accounts = tuple({a for a in accounts if a})
	if not projects or not accounts:
		return set()
	condition = " or ".join(f"ifnull(`{f}`, '') != ''" for f in projects)
	return set(frappe.db.sql_list(
		f"""select distinct account from `tabGL Entry`
		where company = %s and account in %s and is_cancelled = 0 and debit > credit and ({condition})""",
		(company, accounts),
	))
