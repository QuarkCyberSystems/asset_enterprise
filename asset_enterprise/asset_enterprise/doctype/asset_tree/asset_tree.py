"""Asset Tree — GA-0005-01 GAP-009 (§9.2: "Tree doctype + report").

Native Frappe tree (NestedSet) mirroring the Asset.parent_asset
hierarchy: one node per participating asset, browsable via the
standard Tree View (expand/collapse), with the Asset Tree report and
the Asset-form panel as the value views. Nodes are maintained
automatically from Asset saves — direct edits are limited to the
description; structure follows the assets.

No GL impact (grouping only); group transfer stays blocked (VR-010,
enforced on Asset Movement).
"""

import frappe
from frappe import _
from frappe.utils.nestedset import NestedSet


class AssetTree(NestedSet):
	nsm_parent_field = "parent_asset_tree"

	def validate(self):
		if self.child_asset and not self.flags.via_sync:
			# Structure is derived from Asset.parent_asset — keep the
			# node's parent consistent with the asset link.
			asset_parent = frappe.db.get_value("Asset", self.child_asset, "parent_asset")
			self.parent_asset = asset_parent
			self.parent_asset_tree = (
				frappe.db.get_value("Asset Tree", {"child_asset": asset_parent}, "name")
				if asset_parent
				else None
			)

	def on_update(self):
		NestedSet.on_update(self)
		self.db_set(
			"is_group",
			1 if frappe.db.exists("Asset Tree", {"parent_asset_tree": self.name}) else 0,
			update_modified=False,
		)


def sync_asset_tree(asset_name):
	"""Upsert the Asset Tree nodes for one asset and its parent chain.
	Called from EnterpriseAsset on save/update; idempotent. An asset
	participates when it has a parent OR children (roots included)."""
	parent = frappe.db.get_value("Asset", asset_name, "parent_asset")
	node = frappe.db.get_value("Asset Tree", {"child_asset": asset_name}, "name")
	has_children = frappe.db.exists(
		"Asset", {"parent_asset": asset_name, "docstatus": ("<", 2)}
	)

	if not parent and not has_children:
		if node and not frappe.db.exists("Asset Tree", {"parent_asset_tree": node}):
			frappe.delete_doc("Asset Tree", node, force=1, ignore_permissions=True)
		return

	parent_node = None
	if parent:
		parent_node = frappe.db.get_value("Asset Tree", {"child_asset": parent}, "name")
		if not parent_node:
			sync_asset_tree(parent)  # builds the chain upwards
			parent_node = frappe.db.get_value("Asset Tree", {"child_asset": parent}, "name")

	if node:
		_reparent(node, parent_node, parent)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Asset Tree",
				"child_asset": asset_name,
				"parent_asset": parent,
				"parent_asset_tree": parent_node,
			}
		)
		doc.flags.ignore_permissions = True
		doc.flags.via_sync = True
		doc.insert()
	if parent_node:
		frappe.db.set_value("Asset Tree", parent_node, "is_group", 1, update_modified=False)


def _reparent(node, parent_node, parent_asset):
	doc = frappe.get_doc("Asset Tree", node)
	if doc.parent_asset_tree == parent_node and doc.parent_asset == parent_asset:
		return
	doc.parent_asset_tree = parent_node
	doc.parent_asset = parent_asset
	doc.flags.ignore_permissions = True
	doc.flags.via_sync = True
	doc.save()


def rebuild_all():
	"""Backfill nodes for every asset participating in a tree.
	Idempotent; runs on after_migrate."""
	parents = frappe.get_all(
		"Asset",
		filters={"parent_asset": ("!=", ""), "docstatus": ("<", 2)},
		fields=["name", "parent_asset"],
	)
	for row in parents:
		try:
			sync_asset_tree(row.name)
		except Exception:
			frappe.log_error(
				title=f"asset_tree sync failed: {row.name}", message=frappe.get_traceback()
			)


# Tree View uses frappe's generic nested-set browser (is_tree +
# nsm_parent_field + title_field cover it) — no custom source needed.
