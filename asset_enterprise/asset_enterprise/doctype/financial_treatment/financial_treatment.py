from frappe.model.document import Document


class FinancialTreatment(Document):
	"""Append-only record of every asset financial impact (GA-0005-01 §3.1).

	Created exclusively by the TCC (asset_enterprise.tcc). in_create=1 hides
	the manual New button; status transitions only via tcc.apply/tcc.reverse.
	"""

	pass
