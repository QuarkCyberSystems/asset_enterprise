"""Reversal by counter-document — GAP-030 (AVA), GAP-014 (Capitalized
Maintenance), GAP-033 (capitalized Asset Repair).

A posted asset transaction is never cancelled. It is reversed: a
counter-document of the same doctype posts the mirror on a chosen date
and pairs the Financial Treatments, and the original STAYS SUBMITTED,
flagged Reversed (GAP-030 step 4: "Both stay docstatus=1; the source is
not cancelled in the framework sense"). Its ledger entries stay posted —
the immutable ledger, GA-0001-01.

Until 19/10/2026 the reversal was triggered from on_cancel, so the
framework left the original Cancelled even though nothing in its ledger
was (client, ACC-AVA-2026-00046). The trigger is now this contract, on
qcs_platform's reversal model (§5.7): the module's adapter refuses native
cancel WITH a route (Reverse; platform.AssetLedgerAdapter.can_cancel),
and the counter-document carries the platform's pair
(`is_cancellation` / `cancellation_against`) next to this module's own
links, so the platform's banner, veto and "Reversed by" read it too.

A controller mixes in CounterDocumentReversal and supplies:

    reversal_link_field       the source's "reversed by" link field
    reversal_of_field         the counter-document's back-link field
    reversal_label            how the document is named in messages
    reversal_eligible()       None, or why this document is not reversed
                              this way (then a plain cancel applies)
    reversal_gates()          refusals that depend on the asset (VR-042...)
    build_reversal(date)      the unsaved counter-document

and the shared steps — date gate, permission, insert/submit, the
Reversed stamp — live here, once.
"""

import frappe
from frappe import _
from frappe.utils import getdate, nowdate


class CounterDocumentReversal:
	reversal_link_field = None
	reversal_of_field = None
	reversal_label = None

	# ------------------------------------------------------------ contract
	def reversal_eligible(self):
		return None

	def reversal_gates(self):
		pass

	def build_reversal(self, posting_date):
		raise NotImplementedError

	def reversal_source_date(self):
		return self.get("posting_date") or self.get("date")

	# -------------------------------------------------------------- shared
	def is_counter_document(self):
		return bool(self.get(self.reversal_of_field))

	def reverse(self, posting_date=None, check_permission=True):
		"""Reverse this document on `posting_date` (default today) and
		return the counter-document's name."""
		from asset_enterprise.api import assert_reversal_date

		label = _(self.reversal_label or self.doctype)
		if self.docstatus != 1:
			frappe.throw(_("Only a submitted {0} can be reversed.").format(label))
		if self.is_counter_document():
			frappe.throw(
				_("{0} is itself a reversal and cannot be reversed. Submit a fresh {1} instead.").format(
					self.name, label
				),
				title=_("Reversal Entry"),
			)
		if self.get(self.reversal_link_field):
			frappe.throw(
				_("{0} is already reversed by {1}.").format(self.name, self.get(self.reversal_link_field)),
				title=_("Already Reversed"),
			)
		reason = self.reversal_eligible()
		if reason:
			frappe.throw(reason, title=_("Not Reversible"))
		if check_permission:
			# the same right that could cancel it before
			frappe.has_permission(self.doctype, "cancel", self, throw=True)
		date = getdate(assert_reversal_date(self.company, self.reversal_source_date(), posting_date, label))
		self.reversal_gates()

		counter = self.build_reversal(date)
		counter.set(self.reversal_of_field, self.name)
		if counter.meta.has_field("cancellation_against"):
			# qcs_platform's reversal pair (§5.7): its veto, banner and
			# reversed_by read this
			counter.is_cancellation = 1
			counter.cancellation_against = self.name
		counter.flags.ignore_permissions = True
		counter.insert()
		counter.submit()

		self.db_set({self.reversal_link_field: counter.name, "reversed_on": date}, update_modified=False)
		self.after_reversal(counter)
		return counter.name

	def reversal_asset(self):
		return self.get("asset") or self.get("target_asset")

	def after_reversal(self, counter):
		"""The asset's history says what happened (a counter-document that
		records its own row overrides this)."""
		from asset_enterprise.tcc import add_snapshot_activity

		add_snapshot_activity(
			self.reversal_asset(),
			_("{0} {1} reversed by {2} on {3}; its ledger entries remain posted.").format(
				_(self.reversal_label or self.doctype), self.name, counter.name,
				frappe.utils.formatdate(self.get("reversed_on") or counter.get("posting_date") or counter.get("date")),
			),
			transaction_type="Reversal",
		)

	def cancel_refusal(self):
		"""Why native cancel is refused, for the platform adapter:
		(title, message, offers_reverse) — or None when this document is
		not reversed this way and cancels natively (reversal_eligible)."""
		if self.reversal_eligible():
			return None
		label = _(self.reversal_label or self.doctype)
		if self.is_counter_document():
			return (
				_("Reversal Entry"),
				_("{0} is a reversal entry and is final. To redo the transaction, submit a fresh {1}.").format(
					self.name, label
				),
				False,
			)
		reversed_by = self.get(self.reversal_link_field)
		if reversed_by:
			return (
				_("Already Reversed"),
				_("{0} has been reversed by {1}; both stay on the record and its ledger entries remain posted.").format(
					self.name, reversed_by
				),
				False,
			)
		return (
			_("Use Reverse"),
			_(
				"{0} cannot be cancelled: its ledger entries are immutable. Reverse it — a reversal "
				"entry posts the mirror on the date you choose, and {0} stays on the record, marked "
				"Reversed."
			).format(self.name),
			True,
		)


# The source's "reversed by" link per doctype — the one place the fact
# "this document has been reversed" is read from (a reversed document is
# still docstatus 1).
REVERSAL_LINKS = {
	"Asset Value Adjustment": "reversed_by_ava",
	"Asset Capitalization": "reversed_by_capitalization",
	"Asset Repair": "reversed_by_repair",
}
REVERSIBLE_DOCTYPES = tuple(REVERSAL_LINKS)


def standing_condition(doctype, alias):
	"""SQL: the row aliased `alias` (of `doctype`) stands — submitted and
	not reversed. Every reader that used docstatus = 1 for "still in force"
	uses this instead."""
	return f"({alias}.docstatus = 1 and ifnull({alias}.`{REVERSAL_LINKS[doctype]}`, '') = '')"


def standing_filters(doctype):
	"""frappe filters for the same rule."""
	return {"docstatus": 1, REVERSAL_LINKS[doctype]: ("is", "not set")}


def is_standing(doctype, name):
	return bool(frappe.db.exists(doctype, {"name": name, **standing_filters(doctype)}))


def reverse(doctype, name, posting_date=None, check_permission=True):
	"""Reverse a posted asset document by its counter-document. The one
	entry point — the desk, the asset module and other apps all come here."""
	if doctype not in REVERSIBLE_DOCTYPES:
		frappe.throw(_("{0} is not reversed by counter-document.").format(doctype))
	doc = frappe.get_doc(doctype, name)
	if not isinstance(doc, CounterDocumentReversal):
		frappe.throw(_("{0} is not set up for reversal on this site.").format(doctype))
	return doc.reverse(posting_date=posting_date or nowdate(), check_permission=check_permission)
