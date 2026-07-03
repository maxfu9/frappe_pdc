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
