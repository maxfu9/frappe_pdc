import frappe


def execute():
	if not frappe.db.exists("Workspace", "PDC Dashboard"):
		return

	if not frappe.db.exists("Workspace", "PDC"):
		return

	for doctype in (
		"Workspace Link",
		"Workspace Chart",
		"Workspace Shortcut",
		"Workspace Quick List",
		"Workspace Number Card",
		"Workspace Custom Block",
		"Has Role",
	):
		frappe.db.delete(doctype, {"parenttype": "Workspace", "parent": "PDC"})

	frappe.db.delete("Workspace", {"name": "PDC"})
	frappe.clear_cache()
