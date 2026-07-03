import frappe
from frappe import _
from frappe.model.document import Document


class PDCSettings(Document):
	def validate(self):
		roles = [
			role.strip()
			for role in (self.manager_roles or "").replace(",", "\n").splitlines()
			if role.strip()
		]
		if not roles:
			frappe.throw(_("At least one PDC Manager Role is required."))

		for role in roles:
			if not frappe.db.exists("Role", role):
				frappe.throw(_("Role {0} does not exist.").format(role))

		for fieldname in ("pdc_receivable_account", "bounce_charges_account"):
			account = self.get(fieldname)
			if not account:
				continue

			if not frappe.db.exists("Account", account):
				frappe.throw(_("{0} {1} does not exist.").format(self.meta.get_label(fieldname), account))
			if frappe.db.get_value("Account", account, "is_group"):
				frappe.throw(_("{0} cannot be a group account.").format(self.meta.get_label(fieldname)))

		if self.bounce_charges_item:
			if not frappe.db.exists("Item", self.bounce_charges_item):
				frappe.throw(_("Bounce Charges Item {0} does not exist.").format(self.bounce_charges_item))
			if frappe.db.get_value("Item", self.bounce_charges_item, "disabled"):
				frappe.throw(_("Bounce Charges Item {0} is disabled.").format(self.bounce_charges_item))
			if frappe.db.get_value("Item", self.bounce_charges_item, "is_stock_item"):
				frappe.throw(_("Bounce Charges Item {0} must be a non-stock service item.").format(self.bounce_charges_item))
