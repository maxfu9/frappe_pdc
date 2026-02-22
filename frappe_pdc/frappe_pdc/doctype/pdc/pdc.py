import frappe
from frappe.model.document import Document

class PDC(Document):
    def autoname(self):
        # Initial name is Customer Name
        # If customer name already exists, append a series
        base_name = self.customer
        if frappe.db.exists("PDC", base_name):
            self.name = frappe.model.naming.make_autoname(base_name + "-.#####")
        else:
            self.name = base_name

    def on_cancel(self):
        # 1. Cancel Supplier Payment Entry if handed over
        if self.supplier_payment_entry:
            supp_pe_status = frappe.db.get_value("Payment Entry", self.supplier_payment_entry, "docstatus")
            if supp_pe_status == 1:
                supp_pe = frappe.get_doc("Payment Entry", self.supplier_payment_entry)
                supp_pe.cancel()

        # 2. Cancel original Customer Payment Entry
        if self.payment_entry:
            pe_status = frappe.db.get_value("Payment Entry", self.payment_entry, "docstatus")
            if pe_status == 1:
                pe_doc = frappe.get_doc("Payment Entry", self.payment_entry)
                pe_doc.cancel()
