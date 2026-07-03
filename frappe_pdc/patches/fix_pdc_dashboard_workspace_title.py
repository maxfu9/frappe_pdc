import frappe


def execute():
	if frappe.db.exists("Workspace", "PDC Dashboard"):
		frappe.db.set_value(
			"Workspace",
			"PDC Dashboard",
			{
				"title": "PDC Dashboard",
				"label": "PDC Dashboard",
			},
			update_modified=False,
		)
	frappe.clear_cache()
