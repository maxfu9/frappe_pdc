import frappe


def execute():
	if not frappe.db.exists("DocType", "PDC Settings"):
		return

	defaults = {
		"manager_roles": "Accounts Manager\nSystem Manager",
		"notify_accounts_managers": 1,
		"notify_customers": 1,
		"require_clearance_approval": 0,
		"allow_early_clearance": 0,
		"reminder_days_before_maturity": 0,
		"notify_overdue_pdcs": 1,
		"overdue_grace_days": 1,
		"notify_bounced_pdcs": 1,
		"notify_replacement_followup": 1,
		"replacement_followup_days": 3,
	}
	for fieldname, value in defaults.items():
		has_row = frappe.db.exists("Singles", {"doctype": "PDC Settings", "field": fieldname})
		current_value = frappe.db.get_single_value("PDC Settings", fieldname)
		if not has_row or current_value in (None, ""):
			frappe.db.set_single_value("PDC Settings", fieldname, value)
