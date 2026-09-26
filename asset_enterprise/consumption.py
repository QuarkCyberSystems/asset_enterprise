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
		projects = project_fields()
		for row in frappe.db.sql(
			f"""select gl.voucher_detail_no, gl.cost_center {cols}, sum(gl.debit - gl.credit) as amount
			from `tabGL Entry` gl
			where gl.voucher_type = 'Purchase Invoice' and gl.voucher_no in %s and gl.account = %s
			  and gl.is_cancelled = 0
			group by gl.voucher_detail_no, gl.cost_center {cols}""",
			(tuple(returns), account),
			as_dict=True,
		):
			if not flt(row.amount):
				continue
			origin = returned_rows.get(row.voucher_detail_no)
			if origin:
				same = [ln for ln in lines if ln.voucher_detail_no == origin]
				target = next((ln for ln in same if ln.cost_center == row.cost_center), same[0] if same else None)
			else:
				# a debit-note row with no link back to its invoice row (chief
				# r6 S-1): netted into the line the project ledger's reader
				# nets it into - same project and cost centre, then same
				# project, then same cost centre, then the earliest line
				target = _unlinked_return_line(lines, row, projects)
			if target:
				target.amount = flt(target.amount) + flt(row.amount)
	return [ln for ln in lines if flt(ln.amount) > 0]


def _unlinked_return_line(lines, row, projects):
	"""The line an unlinked return row nets into: project_accounting's
	`reversals._pick` order (its asset levels never split an invoice's
	lines here - the asset is not a line dimension of an invoice cost)."""
	same_project = lambda ln: bool(projects) and all(ln.get(f) == row.get(f) for f in projects) and any(  # noqa: E731
		row.get(f) for f in projects)
	same_cc = lambda ln: ln.cost_center == row.cost_center  # noqa: E731
	for match in (lambda ln: same_project(ln) and same_cc(ln), same_project, same_cc):
		found = [ln for ln in lines if match(ln)]
		if found:
			return found[0]
	return lines[0] if lines else None


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


def standing_consumers(purchase_invoice):
	"""[(doctype, name)] of the submitted repairs and capitalizations whose
	recorded lines consume the invoice's cost - one indexed query. A
	cancelled consumer (its reversal stands) has given its lines back."""
	if not purchase_invoice or not frappe.db.table_exists(LINE_DOCTYPE):
		return []
	joins = " ".join(
		f"left join `tab{dt}` p{i} on l.parenttype = '{dt}' and p{i}.name = l.parent"
		for i, dt in enumerate(CONSUMERS)
	)
	status = "coalesce(" + ", ".join(f"p{i}.docstatus" for i in range(len(CONSUMERS))) + ")"
	return [tuple(r) for r in frappe.db.sql(
		f"""select distinct l.parenttype, l.parent from `tab{LINE_DOCTYPE}` l {joins}
		where l.purchase_invoice = %s and {status} = 1
		order by l.parenttype, l.parent""",
		purchase_invoice,
	)]


def _rank(line, dims, cost_center, projects):
	"""Project first (R231), then the other matching dimensions, then the
	consumer's cost centre, then the earliest line."""
	project = sum(1 for d in projects if dims.get(d) and line.get(d) == dims.get(d))
	other = sum(1 for d, v in dims.items() if d not in projects and v and line.get(d) == v)
	return (-project, -other, 0 if cost_center and line.cost_center == cost_center else 1, line.first)


FREE_CAPACITY_HOOK = "asset_consumption_free_capacity"


def _free_capacity(invoice, account, lines):
	"""{(invoice row, cost centre): amount} of the lines another ledger
	limits - what a line can still give without taking value a settlement
	already took or holds (chief r6 S-2). Declared by the app that owns
	that ledger through the `asset_consumption_free_capacity` hook (a
	function taking the candidate line rows and returning {index: amount}
	for the lines it limits); a line no app limits is absent. Never named
	here, so this app stays free of any project app (D-013)."""
	hooks = frappe.get_hooks(FREE_CAPACITY_HOOK) or []
	if not hooks or not lines:
		return {}
	rows = [
		{
			"purchase_invoice": invoice,
			"purchase_invoice_item": ln.voucher_detail_no,
			"expense_account": account,
			"cost_center": ln.cost_center,
			"dimensions": json.dumps({d: ln.get(d) for d in _dimensions()}, sort_keys=True),
		}
		for ln in lines
	]
	out = {}
	for path in hooks:
		for idx, amount in (frappe.get_attr(path)(rows) or {}).items():
			ln = lines[int(idx)]
			key = (ln.voucher_detail_no or None, ln.cost_center or None)
			out[key] = min(flt(amount), out.get(key, flt(amount)))
	return out


def plan(requests, company, exclude_parent=None, prior=None, prefer_free=False):
	"""The line rows `requests` consume: ([row dict], [(request, amount not
	placed)]). `requests` are (source row, purchase invoice, expense
	account, amount, {dimension: value}, cost centre); `prior` holds
	{(invoice, account, invoice row, cost centre): amount} taken by
	consumers not yet recorded (the upgrade's earlier legacy repairs). The
	lines and other consumers are read once per (invoice, account).

	With `prefer_free` (a new document), lines of one rank tier (the same
	project match) are filled first up to what no settlement took or holds
	(`_free_capacity`), then up to their capacity - so a free line of the
	invoice is consumed before a settled one (chief r6 S-2); the project
	tier order is never crossed. The upgrade's legacy allocation keeps the
	plain order."""
	rows, short = [], []
	taken = dict(prior or {})
	projects = project_fields()
	lines_of, elsewhere_of, free_of = {}, {}, {}
	for request in requests:
		source_row, invoice, account, amount, dims, cost_center = request
		remaining = _round(amount, company)
		if remaining <= 0:
			continue
		if (invoice, account) not in lines_of:
			lines_of[(invoice, account)] = invoice_lines(invoice, account)
			elsewhere_of[(invoice, account)] = consumed_elsewhere(invoice, account, exclude_parent=exclude_parent)
			free_of[(invoice, account)] = _free_capacity(invoice, account, lines_of[(invoice, account)]) if prefer_free else {}
		elsewhere, free = elsewhere_of[(invoice, account)], free_of[(invoice, account)]
		ranked = sorted(lines_of[(invoice, account)], key=lambda ln: _rank(ln, dims, cost_center, projects))
		tiers = {}
		for line in ranked:
			tiers.setdefault(_rank(line, dims, cost_center, projects)[0], []).append(line)
		takes = {}
		for tier in tiers.values():
			for limited in ((True, False) if free else (False,)):
				for line in tier:
					if remaining <= 0:
						break
					key = (invoice, account, line.voucher_detail_no or None, line.cost_center or None)
					capacity = flt(line.amount) - elsewhere.get(key[2:], 0) - taken.get(key, 0)
					if limited and key[2:] in free:
						capacity = min(capacity, free[key[2:]] - taken.get(key, 0))
					take = _round(min(capacity, remaining), company)
					if take <= 0:
						continue
					if key not in takes:
						takes[key] = (line, 0)
					takes[key] = (line, takes[key][1] + take)
					taken[key] = taken.get(key, 0) + take
					remaining = _round(remaining - take, company)
		for key, (line, take) in takes.items():
			rows.append({
				"source_row": source_row,
				"purchase_invoice": invoice,
				"purchase_invoice_item": line.voucher_detail_no,
				"expense_account": account,
				"cost_center": line.cost_center,
				"amount": _round(take, company),
				"dimensions": json.dumps({d: line.get(d) for d in _dimensions()}, sort_keys=True),
			})
		if remaining > 0:
			short.append((request, remaining))
	return rows, short


def repair_cover(repair):
	"""What the invoices a repair names would still cover were it submitted
	again: per (invoice, account) of its invoice rows, the lesser of its
	cost there and the lines' live net less what other submitted
	consumers took - the cap `allocate` applies (the remedy the project
	ledger's upgrade note words, chief r6 S-8)."""
	cost = {}
	for row in frappe.get_all("Asset Repair Purchase Invoice", filters={"parent": repair, "parenttype": "Asset Repair"},
			fields=["purchase_invoice", "expense_account", "repair_cost"]):
		if row.purchase_invoice and row.expense_account:
			pair = (row.purchase_invoice, row.expense_account)
			cost[pair] = flt(cost.get(pair)) + flt(row.repair_cost)
	total = 0
	for (invoice, account), amount in cost.items():
		net = sum(flt(ln.amount) for ln in invoice_lines(invoice, account))
		left = net - sum(consumed_elsewhere(invoice, account, exclude_parent=repair).values())
		total += min(amount, max(left, 0))
	return flt(total, 2)


def allocate(doc, requests):
	"""Fill `doc.consumed_cost_lines` from `requests` (see `plan`) - one
	line row per consumed invoice line. Refuses an amount the invoice's
	lines on that account cannot cover."""
	doc.set(TABLE, [row for row in (doc.get(TABLE) or []) if row.get("stock_entry")])
	if not requests:
		return
	rows, short = plan(requests, doc.company, exclude_parent=doc.name, prefer_free=True)
	for row in rows:
		# named as appended: the rows are added in before_submit, after
		# Frappe named the document's children, and a reader running in
		# the same event must be able to tell them apart (cross-app r4
		# MUST); still inserted with the document (`__islocal`)
		doc.append(TABLE, row).name = frappe.generate_hash(length=10)
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
