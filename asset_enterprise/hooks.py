app_name = "asset_enterprise"
app_title = "Asset Enterprise"
app_publisher = "QuarkCyberSystems"
app_description = "Enterprise Fixed Asset Management for Al Badia Cement"
app_email = "vivek@quarkcs.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]

# GA-0005-01 v2.14 — controller overrides (build plan §2.1).
# Phase 0: pass-through subclasses; behavior lands per phase.
override_doctype_class = {
	"Asset": "asset_enterprise.overrides.asset.EnterpriseAsset",
	"Asset Repair": "asset_enterprise.overrides.asset_repair.EnterpriseAssetRepair",
	"Asset Value Adjustment": "asset_enterprise.overrides.ava.EnterpriseAVA",
	"Asset Capitalization": "asset_enterprise.overrides.asset_capitalization.EnterpriseAssetCapitalization",
	"Asset Depreciation Schedule": "asset_enterprise.overrides.schedule.EnterpriseSchedule",
	"Asset Movement": "asset_enterprise.overrides.asset_movement.EnterpriseAssetMovement",
}

# GA-0005-01 v2.14 — endpoint replacements (build plan §2.2, Phase 6).
# Wrappers delegate to core when the Asset Settings master switch is off.
override_whitelisted_methods = {
	"erpnext.assets.doctype.asset.depreciation.restore_asset": "asset_enterprise.restore.restore_asset",
	"erpnext.assets.doctype.asset.depreciation.scrap_asset": "asset_enterprise.disposal.scrap_asset",
}

# GA-0005-01 v2.14 — form JS (Phase 8, §9.6 — backend-authoritative).
app_include_js = "/assets/asset_enterprise/js/system_only_option.js"

doctype_js = {
	"Asset": "public/js/asset.js",
	# GAP-014 / VR-037: re-scope the Target Asset picker per
	# transaction_type — core allows only draft composite assets, which
	# no valid Capitalized Maintenance target ever is.
	"Asset Capitalization": "public/js/asset_capitalization.js",
	# GAP-012: scope the Asset Allocation picker to assets this invoice
	# can cover, and surface the section on fixed-asset invoices.
	"Purchase Invoice": "public/js/purchase_invoice.js",
	# Reversal AVAs must announce themselves (client, 19/08): banner on
	# the reversal and on the reversed original.
	"Asset Value Adjustment": "public/js/asset_value_adjustment.js",
	# Client, 20/08: show the disposal account and cost centre resolved
	# from the Scrapping Type, and unlock the cost centre when that type
	# allows it to be changed.
	"Scrap Transaction": "public/js/scrap_transaction.js",
	# A cost-centre transfer posts nothing itself; its effect lands in
	# later depreciation entries. Spell that out before submit.
	"Asset Movement": "public/js/asset_movement.js",
	# A Reversal Repair is raised by cancelling a capitalized repair.
	"Asset Repair": "public/js/asset_repair.js",
}

# §4.8: an asset carries the live schedule plus one frozen copy per
# value change — the list must say which is which.
doctype_list_js = {
	"Asset Depreciation Schedule": "public/js/asset_depreciation_schedule_list.js",
	# the badge on both list and form reads from this map — without an
	# entry, a new status silently displays as the document state
	"Asset": "public/js/asset_list.js",
	# reversal AVAs badge as "Reversal", reversed originals as "Reversed"
	"Asset Value Adjustment": "public/js/asset_value_adjustment_list.js",
}

# GA-0005-01 v2.14 — PR/PI asset flows (Phase 7, GAP-004/GAP-012).
doc_events = {
	"Purchase Receipt": {
		"on_submit": "asset_enterprise.invoice_diff.pr_on_submit",
		"before_cancel": "asset_enterprise.invoice_diff.pr_before_cancel",
	},
	"Journal Entry": {
		# GAP-023 / TC-038: fill the Asset accounting dimension from the
		# row's own Asset reference so the General Ledger can be filtered
		# and grouped by asset. Rows pointing at an already-cancelled
		# asset are left alone — the dimension is a Link field.
		"validate": "asset_enterprise.invoice_diff.stamp_asset_dimension",
	},
	"Purchase Invoice": {
		"validate": "asset_enterprise.invoice_diff.pi_validate",
		"on_submit": "asset_enterprise.invoice_diff.pi_on_submit",
		"on_cancel": "asset_enterprise.invoice_diff.pi_on_cancel",
	},
	# GAP-010 / VR-011 (Phase 11): sale disposals honor the
	# prevent-disposal-before-full-invoicing control.
	"Sales Invoice": {
		"validate": "asset_enterprise.invoice_diff.si_validate",
	},
	# Immutable ledger: a posted asset transaction is never deleted — it
	# is reversed. Deleting one left the supersession trail naming a
	# document that no longer existed (UAT, 16/08/2026).
	"Asset Capitalization": {
		"on_trash": "asset_enterprise.immutability.block_deletion_of_posted_document",
	},
	"Asset Repair": {
		"on_trash": "asset_enterprise.immutability.block_deletion_of_posted_document",
	},
	"Asset Value Adjustment": {
		"on_trash": "asset_enterprise.immutability.block_deletion_of_posted_document",
	},
	"Scrap Transaction": {
		"on_trash": "asset_enterprise.immutability.block_deletion_of_posted_document",
	},
}

# Upgrade guard (build plan §2.3 / §6): every bench migrate re-verifies
# that erpnext still exposes our override targets with compatible
# signatures. A bench update that moves a target fails the migrate
# loudly instead of silently dropping our behavior.
after_migrate = [
	"asset_enterprise.overrides.patches.verify_patch_targets",
	"asset_enterprise.setup.install.after_migrate",
]

after_install = "asset_enterprise.setup.install.after_install"

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "asset_enterprise",
# 		"logo": "/assets/asset_enterprise/logo.png",
# 		"title": "Asset Enterprise",
# 		"route": "/asset_enterprise",
# 		"has_permission": "asset_enterprise.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/asset_enterprise/css/asset_enterprise.css"
# app_include_js = "/assets/asset_enterprise/js/asset_enterprise.js"

# include js, css files in header of web template
# web_include_css = "/assets/asset_enterprise/css/asset_enterprise.css"
# web_include_js = "/assets/asset_enterprise/js/asset_enterprise.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "asset_enterprise/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "asset_enterprise/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "asset_enterprise.utils.jinja_methods",
# 	"filters": "asset_enterprise.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "asset_enterprise.install.before_install"
# after_install = "asset_enterprise.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "asset_enterprise.uninstall.before_uninstall"
# after_uninstall = "asset_enterprise.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "asset_enterprise.utils.before_app_install"
# after_app_install = "asset_enterprise.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "asset_enterprise.utils.before_app_uninstall"
# after_app_uninstall = "asset_enterprise.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "asset_enterprise.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "asset_enterprise.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"asset_enterprise.tasks.all"
# 	],
# 	"daily": [
# 		"asset_enterprise.tasks.daily"
# 	],
# 	"hourly": [
# 		"asset_enterprise.tasks.hourly"
# 	],
# 	"weekly": [
# 		"asset_enterprise.tasks.weekly"
# 	],
# 	"monthly": [
# 		"asset_enterprise.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "asset_enterprise.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "asset_enterprise.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "asset_enterprise.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "asset_enterprise.task.get_dashboard_data"
# }

# Connections tab: surface the one-to-many side (Financial Treatments,
# triggered schedule generations, reversal counterpart) — the causal
# one-to-one links stay as hard fields on the form.
override_doctype_dashboards = {
	"Asset Value Adjustment": "asset_enterprise.dashboards.ava_dashboard",
}

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# GAP-031: schedules are NEVER cancelled — they are superseded. Without
# this, the desk's "Cancel All Documents" dialog (fed by the
# triggered_by dynamic link) offered to cancel the Active schedule
# together with the AVA (client, 19/08, ACC-AVA-2026-00002).
# "Asset" joins the schedule here (client, 24/08): the desk's "Cancel All
# Documents" cascade must never cancel an asset on its own. Reversal is
# performed by the owning document's before_cancel — see
# invoice_diff.pr_before_cancel (GAP-004.4) — so the gates and the TCC
# reversal always run.
auto_cancel_exempted_doctypes = ["Asset Depreciation Schedule", "Asset"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["asset_enterprise.utils.before_request"]
# after_request = ["asset_enterprise.utils.after_request"]

# Job Events
# ----------
# before_job = ["asset_enterprise.utils.before_job"]
# after_job = ["asset_enterprise.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"asset_enterprise.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

