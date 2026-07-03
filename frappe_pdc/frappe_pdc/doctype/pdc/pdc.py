import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

FINAL_STATUSES = {"Cleared", "Bounced", "Partially Bounced"}
ALLOWED_TRANSITIONS = {
    "Pending": {
        "Ready for Clearance",
        "Approved for Clearance",
        "Handed Over",
        "Partially Cleared",
        "Cleared",
        "Bounced",
    },
    "Ready for Clearance": {
        "Approved for Clearance",
        "Handed Over",
        "Partially Cleared",
        "Cleared",
        "Bounced",
    },
    "Approved for Clearance": {"Handed Over", "Partially Cleared", "Cleared", "Bounced"},
    "Handed Over": {"Partially Cleared", "Cleared", "Bounced"},
    "Partially Cleared": {"Cleared", "Partially Bounced"},
    "Cleared": set(),
    "Bounced": set(),
    "Partially Bounced": set(),
}

class PDC(Document):
    def autoname(self):
        # Initial name is Customer Name
        # If customer name already exists, append a series
        base_name = self.customer
        if frappe.db.exists("PDC", base_name):
            self.name = frappe.model.naming.make_autoname(base_name + "-.#####")
        else:
            self.name = base_name

    def validate(self):
        self.set_display_title()
        self.validate_amounts()
        self.validate_payment_entry_links()
        self.validate_duplicate_cheque()
        self.validate_accounts()
        self.validate_replacement_links()
        self.validate_bounce_charges_invoice()
        self.validate_status_transition()

    def set_display_title(self):
        if self.meta.has_field("pdc_title"):
            parts = [self.customer, self.handed_over_to_supplier, self.cheque_no]
            self.pdc_title = " - ".join([part for part in parts if part])

    def validate_amounts(self):
        if flt(self.amount) <= 0:
            frappe.throw(_("PDC amount must be greater than zero."))

        if flt(self.cleared_amount) < 0:
            frappe.throw(_("Cleared Amount cannot be negative."))

        if flt(self.cleared_amount) > flt(self.amount) + 0.01:
            frappe.throw(_("Cleared Amount cannot exceed Total Amount."))

        if flt(self.bounce_charges) < 0:
            frappe.throw(_("Bounce Charges cannot be negative."))

        if self.pdc_status == "Cleared" and abs(flt(self.cleared_amount) - flt(self.amount)) > 0.01:
            frappe.throw(_("PDC can only be marked Cleared when the full amount is cleared."))

    def validate_payment_entry_links(self):
        if self.payment_entry:
            pe = frappe.get_doc("Payment Entry", self.payment_entry)
            if pe.party_type != "Customer" or pe.party != self.customer:
                frappe.throw(_("Linked customer Payment Entry does not match the PDC customer."))
            if pe.company != self.company:
                frappe.throw(_("Linked customer Payment Entry company does not match the PDC company."))

        if self.supplier_payment_entry:
            pe = frappe.get_doc("Payment Entry", self.supplier_payment_entry)
            if pe.party_type != "Supplier":
                frappe.throw(_("Linked supplier Payment Entry must be a supplier payment."))
            if pe.party != self.handed_over_to_supplier:
                frappe.throw(_("Linked supplier Payment Entry does not match the endorsed supplier."))
            if pe.company != self.company:
                frappe.throw(_("Linked supplier Payment Entry company does not match the PDC company."))

    def validate_duplicate_cheque(self):
        filters = {
            "cheque_no": self.cheque_no,
            "reference_date": self.reference_date,
            "company": self.company,
            "docstatus": ["<", 2],
            "name": ["!=", self.name],
        }
        if self.bank_account:
            filters["bank_account"] = self.bank_account
        elif self.bank_name:
            filters["bank_name"] = self.bank_name

        duplicate = frappe.get_list("PDC", filters=filters, fields=["name"], limit=1)
        if duplicate:
            frappe.throw(
                _("Cheque/Reference No {0} with date {1} is already registered in PDC {2}.").format(
                    self.cheque_no, self.reference_date, duplicate[0].name
                )
            )

    def validate_accounts(self):
        for fieldname in ("pdc_receivable_account", "clearance_bank_account"):
            account = self.get(fieldname)
            if not account:
                continue

            if not frappe.db.exists("Account", account):
                frappe.throw(_("{0} {1} does not exist.").format(self.meta.get_label(fieldname), account))
            if frappe.db.get_value("Account", account, "is_group"):
                frappe.throw(_("{0} cannot be a group account.").format(self.meta.get_label(fieldname)))

            account_company = frappe.db.get_value("Account", account, "company")
            if self.company and account_company and account_company != self.company:
                frappe.throw(
                    _("{0} {1} does not belong to company {2}.").format(
                        self.meta.get_label(fieldname), account, self.company
                    )
                )

    def validate_replacement_links(self):
        for fieldname in ("replacement_pdc", "replaces_pdc"):
            linked_pdc = self.get(fieldname)
            if not linked_pdc:
                continue

            if linked_pdc == self.name:
                frappe.throw(_("{0} cannot link to the same PDC.").format(self.meta.get_label(fieldname)))

            linked = frappe.db.get_value("PDC", linked_pdc, ["company", "customer"], as_dict=True)
            if not linked:
                frappe.throw(_("Linked PDC {0} does not exist.").format(linked_pdc))
            if linked.company != self.company or linked.customer != self.customer:
                frappe.throw(_("Linked replacement PDC must have the same customer and company."))

    def validate_bounce_charges_invoice(self):
        if not self.bounce_charges_invoice:
            return

        invoice = frappe.db.get_value(
            "Sales Invoice",
            self.bounce_charges_invoice,
            ["company", "customer", "docstatus"],
            as_dict=True,
        )
        if not invoice:
            frappe.throw(_("Bounce Charges Invoice {0} does not exist.").format(self.bounce_charges_invoice))
        if invoice.company != self.company or invoice.customer != self.customer:
            frappe.throw(_("Bounce Charges Invoice must have the same customer and company as the PDC."))
        if invoice.docstatus == 2:
            frappe.throw(_("Cancelled Sales Invoice cannot be linked as Bounce Charges Invoice."))

    def validate_status_transition(self):
        previous = self.get_doc_before_save()
        if not previous:
            return

        if previous.pdc_status in FINAL_STATUSES and self.pdc_status != previous.pdc_status:
            frappe.throw(_("Finalized PDC status cannot be changed."))

        if previous.pdc_status != self.pdc_status:
            allowed = ALLOWED_TRANSITIONS.get(previous.pdc_status, set())
            if self.pdc_status not in allowed:
                frappe.throw(
                    _("PDC status cannot move from {0} to {1}.").format(
                        previous.pdc_status or _("empty"), self.pdc_status or _("empty")
                    )
                )

        if flt(self.cleared_amount) + 0.01 < flt(previous.cleared_amount):
            frappe.throw(_("Cleared Amount cannot be reduced."))

    def on_cancel(self):
        from frappe_pdc.pdc import cancel_pdc_clearance_vouchers

        cancel_pdc_clearance_vouchers(self)

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
