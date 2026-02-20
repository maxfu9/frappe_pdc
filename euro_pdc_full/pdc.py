import frappe
from frappe.utils import today, getdate
from frappe_whatsapp.api import send_whatsapp_message

def handle_pdc_submission(doc, method):
    if doc.is_pdc and doc.mode_of_payment == "Post Dated Cheque":
        if getdate(doc.cheque_date) > getdate(today()):
            doc.pdc_status = "Pending"
        create_or_update_pdc(doc)

def create_or_update_pdc(pe):
    if not pe.is_pdc: return
    existing = frappe.get_all("PDC", filters={"payment_entry": pe.name}, fields=["name"])
    if existing:
        pdc = frappe.get_doc("PDC", existing[0].name)
    else:
        pdc = frappe.new_doc("PDC")
        pdc.payment_entry = pe.name
    pdc.customer = pe.party
    pdc.invoice = pe.invoice_reference
    pdc.amount = pe.paid_amount
    pdc.reference_date = pe.cheque_date
    pdc.pdc_status = pe.pdc_status
    pdc.save(ignore_permissions=True)

@frappe.whitelist()
def mark_pdc_bounced(payment_entry_name, reason=None):
    pe = frappe.get_doc("Payment Entry", payment_entry_name)
    if pe.pdc_status == "Cleared": frappe.throw("Cannot mark cleared PDC as bounced")
    pe.db_set("pdc_status","Bounced")
    existing = frappe.get_all("PDC", filters={"payment_entry": payment_entry_name}, fields=["name"])
    if existing:
        pdc = frappe.get_doc("PDC", existing[0].name)
        pdc.db_set("pdc_status","Bounced")
        if reason: pdc.db_set("bounced_reason",reason)

@frappe.whitelist()
def clear_pdc(payment_entry_name):
    pe = frappe.get_doc("Payment Entry", payment_entry_name)
    if pe.pdc_status != "Approved for Clearance": frappe.throw("PDC must be approved first")
    pe.db_set("pdc_status","Cleared")
    existing = frappe.get_all("PDC", filters={"payment_entry": payment_entry_name}, fields=["name"])
    if existing:
        pdc = frappe.get_doc("PDC", existing[0].name)
        pdc.db_set("pdc_status","Cleared")
