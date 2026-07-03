import frappe


def execute():
	if frappe.db.exists("DocType", "PDC Settings"):
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
			if frappe.db.get_single_value("PDC Settings", fieldname) in (None, ""):
				frappe.db.set_single_value("PDC Settings", fieldname, value)

	if frappe.db.exists("DocType", "PDC") and frappe.db.has_column("PDC", "pdc_title"):
		for pdc in frappe.get_all(
			"PDC",
			fields=["name", "customer", "handed_over_to_supplier", "cheque_no", "pdc_title"],
		):
			if pdc.pdc_title:
				continue

			parts = [pdc.customer, pdc.handed_over_to_supplier, pdc.cheque_no]
			title = " - ".join([part for part in parts if part])
			if title:
				frappe.db.set_value("PDC", pdc.name, "pdc_title", title, update_modified=False)
