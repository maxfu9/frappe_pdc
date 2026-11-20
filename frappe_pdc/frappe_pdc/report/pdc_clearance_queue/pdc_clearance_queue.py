from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from frappe_pdc.pdc import PDC_FINAL_STATUSES, _clearance_block, get_pdc_settings


def execute(filters=None):
    filters = frappe._dict(filters or {})
    current_date = getdate(today())
    pdc_filters = {
        "docstatus": 1,
        "pdc_status": ["not in", sorted(PDC_FINAL_STATUSES)],
        "reference_date": ["<=", getdate(filters.due_through or current_date)],
    }
    if filters.company:
        pdc_filters["company"] = filters.company

    # Only read child records and linked payment metadata for visible PDCs.
    pdcs = frappe.get_list(
        "PDC",
        filters=pdc_filters,
        fields=[
            "name", "company", "customer", "cheque_no", "reference_date", "pdc_status",
            "docstatus", "amount", "cleared_amount", "payment_entry", "supplier_payment_entry",
            "handed_over_to_supplier", "handover_date", "clearance_bank_account", "pdc_receivable_account",
        ],
        order_by="reference_date asc, name asc",
        limit_page_length=0,
    )
    if not pdcs:
        return get_columns(), []

    settings = get_pdc_settings()
    pdc_names = [pdc.name for pdc in pdcs]
    payments = get_linked_payments(pdcs)
    payment_statuses = {name: payment.docstatus for name, payment in payments.items()}
    invoices = get_pending_invoices(pdc_names)
    approved = set()
    if int(settings.get("require_clearance_approval") or 0):
        approved = set(frappe.get_all(
            "PDC Activity",
            filters={
                "parenttype": "PDC",
                "parentfield": "status_history",
                "parent": ["in", pdc_names],
                "action": "Approved for Clearance",
            },
            pluck="parent",
        ))
    companies = {pdc.company for pdc in pdcs}
    default_currencies = dict(frappe.get_all(
        "Company",
        filters={"name": ["in", sorted(companies)]},
        fields=["name", "default_currency"],
        as_list=True,
    ))

    rows = []
    for pdc in pdcs:
        days_due = (current_date - getdate(pdc.reference_date)).days
        timing = "Overdue" if days_due > 0 else "Due Today" if days_due == 0 else "Upcoming"
        if filters.timing and filters.timing != timing:
            continue

        block = _clearance_block(
            pdc,
            current_date,
            settings=settings,
            approved=pdc.name in approved or pdc.pdc_status == "Approved for Clearance",
            payment_statuses=payment_statuses,
        )
        remaining = flt(pdc.amount) - flt(pdc.cleared_amount)
        if not block and remaining <= 0:
            block = ("amount", _("There is no remaining cheque balance to clear."))
        work_status = "Ready to Clear"
        if block:
            work_status = {"approval": "Awaiting Approval", "maturity": "Not Yet Due"}.get(block[0], "Blocked")
        if filters.work_status and filters.work_status != work_status:
            continue

        receipt = payments.get(pdc.payment_entry)
        currency = receipt.paid_from_account_currency if receipt else None
        bank_account = pdc.clearance_bank_account
        if not pdc.pdc_receivable_account and not bank_account and receipt:
            bank_account = receipt.paid_to
        rows.append({
            **pdc,
            "timing": _(timing),
            "work_status": _(work_status),
            "days_due": days_due,
            "remaining": remaining,
            "currency": currency or default_currencies.get(pdc.company),
            "clearance_bank_account": None if pdc.supplier_payment_entry else bank_account,
            "blocking_reason": block[1] if block else None,
            "pending_invoices": ", ".join(invoices[pdc.name]),
        })
    return get_columns(), rows


def get_linked_payments(pdcs):
    names = {
        name
        for pdc in pdcs
        for name in (pdc.payment_entry, pdc.supplier_payment_entry)
        if name
    }
    if not names:
        return {}
    return {
        row.name: row
        for row in frappe.get_all(
            "Payment Entry",
            filters={"name": ["in", sorted(names)]},
            fields=["name", "docstatus", "paid_from_account_currency", "paid_to"],
        )
    }


def get_pending_invoices(pdc_names):
    invoices = defaultdict(dict)
    for row in frappe.get_all(
        "PDC Reference",
        filters={
            "parenttype": "PDC",
            "parentfield": ["in", ["references_table", "purchase_references"]],
            "parent": ["in", pdc_names],
            "reference_doctype": ["in", ["Sales Invoice", "Purchase Invoice"]],
        },
        fields=["parent", "reference_name", "allocated_amount", "cleared_amount"],
        order_by="parent asc, parentfield desc, idx asc",
    ):
        if flt(row.allocated_amount) > flt(row.cleared_amount):
            invoices[row.parent][row.reference_name] = None
    return invoices


def get_columns():
    return [
        {"fieldname": "name", "label": _("PDC"), "fieldtype": "Link", "options": "PDC", "width": 155},
        {"fieldname": "timing", "label": _("Timing"), "fieldtype": "Data", "width": 95},
        {"fieldname": "work_status", "label": _("Clearance Status"), "fieldtype": "Data", "width": 145},
        {"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 160},
        {"fieldname": "reference_date", "label": _("Cheque Date"), "fieldtype": "Date", "width": 110},
        {"fieldname": "remaining", "label": _("Remaining"), "fieldtype": "Currency", "options": "currency", "width": 110},
        {"fieldname": "clearance_bank_account", "label": _("Clearance Bank Account"), "fieldtype": "Link", "options": "Account", "width": 170},
        {"fieldname": "handed_over_to_supplier", "label": _("Endorsed Supplier"), "fieldtype": "Link", "options": "Supplier", "width": 150},
        {"fieldname": "blocking_reason", "label": _("Blocking Reason"), "fieldtype": "Data", "width": 300},
        {"fieldname": "pending_invoices", "label": _("Pending Invoices"), "fieldtype": "Data", "width": 200},
        {"fieldname": "cheque_no", "label": _("Cheque No"), "fieldtype": "Data", "width": 115},
        {"fieldname": "pdc_status", "label": _("PDC Status"), "fieldtype": "Data", "width": 145},
        {"fieldname": "days_due", "label": _("Days Due"), "fieldtype": "Int", "width": 80},
        {"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "options": "currency", "width": 110},
        {"fieldname": "cleared_amount", "label": _("Cleared"), "fieldtype": "Currency", "options": "currency", "width": 110},
        {"fieldname": "company", "label": _("Company"), "fieldtype": "Link", "options": "Company", "width": 150},
        {"fieldname": "payment_entry", "label": _("Customer Payment Entry"), "fieldtype": "Link", "options": "Payment Entry", "width": 160},
        {"fieldname": "supplier_payment_entry", "label": _("Supplier Payment Entry"), "fieldtype": "Link", "options": "Payment Entry", "width": 160},
        {"fieldname": "pdc_receivable_account", "label": _("PDC Receivable Account"), "fieldtype": "Link", "options": "Account", "width": 170},
        {"fieldname": "currency", "label": _("Currency"), "fieldtype": "Link", "options": "Currency", "width": 85},
    ]
