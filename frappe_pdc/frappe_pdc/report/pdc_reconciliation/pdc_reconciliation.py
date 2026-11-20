from collections import defaultdict
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, getdate


def execute(filters=None):
    filters = frappe._dict(filters or {})
    conditions = [["docstatus", "=", 1]]
    if filters.company:
        conditions.append(["company", "=", filters.company])
    if filters.pdc:
        conditions.append(["name", "=", filters.pdc])
    if filters.from_date:
        conditions.append(["reference_date", ">=", getdate(filters.from_date)])
    if filters.to_date:
        conditions.append(["reference_date", "<=", getdate(filters.to_date)])
    pdcs = frappe.get_list(
        "PDC", filters=conditions,
        fields=[
            "name", "company", "customer", "reference_date", "pdc_status", "cleared_amount",
            "payment_entry", "supplier_payment_entry", "handed_over_to_supplier",
            "pdc_receivable_account", "last_clearance_journal_entry",
        ],
        order_by="reference_date desc, name asc", limit_page_length=0,
    )
    if not pdcs:
        return get_columns(), []

    voucher_keys = get_voucher_keys(pdcs)
    keys = set().union(*voucher_keys.values())
    vouchers = get_vouchers(keys)
    ledger = get_ledger(keys)
    matches = get_statement_matches(keys)

    rows = []
    for pdc in pdcs:
        row = reconcile_pdc(pdc, voucher_keys[pdc.name], vouchers, ledger, matches)
        if row and (not cint(filters.get("exceptions_only", 1)) or row["exception"]):
            rows.append(row)
    return get_columns(), rows


def get_voucher_keys(pdcs):
    keys_by_pdc = {pdc.name: set() for pdc in pdcs}
    for row in frappe.get_all(
        "PDC Clearance Voucher",
        filters={
            "parenttype": "PDC", "parentfield": "clearance_vouchers",
            "parent": ["in", list(keys_by_pdc)],
            "voucher_type": ["in", ["Payment Entry", "Journal Entry"]],
        },
        fields=["parent", "voucher_type", "voucher_no"],
    ):
        keys_by_pdc[row.parent].add((row.voucher_type, row.voucher_no))
    for pdc in pdcs:
        for doctype, name in (
            ("Payment Entry", pdc.payment_entry),
            ("Payment Entry", pdc.supplier_payment_entry),
            ("Journal Entry", pdc.last_clearance_journal_entry),
        ):
            if name:
                keys_by_pdc[pdc.name].add((doctype, name))
    return keys_by_pdc


def get_vouchers(keys):
    vouchers = {}
    for doctype, extra_fields in (
        ("Payment Entry", ["party_type", "party", "paid_amount", "paid_from_account_currency"]),
        ("Journal Entry", []),
    ):
        names = sorted(name for kind, name in keys if kind == doctype)
        if names:
            for row in frappe.get_all(
                doctype, filters={"name": ["in", names]},
                fields=["name", "company", "docstatus", "clearance_date", *extra_fields],
            ):
                vouchers[(doctype, row.name)] = row
    return vouchers


def get_ledger(keys):
    ledger = defaultdict(list)
    if not keys:
        return ledger
    for row in frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": ["in", ["Payment Entry", "Journal Entry"]],
            "voucher_no": ["in", sorted({name for _, name in keys})], "is_cancelled": 0,
        },
        fields=[
            "voucher_type", "voucher_no", "account", "account_currency",
            "debit_in_account_currency", "credit_in_account_currency",
        ],
    ):
        ledger[(row.voucher_type, row.voucher_no)].append(row)
    accounts = {row.account for lines in ledger.values() for row in lines}
    banks = set(frappe.get_all(
        "Account", filters={"name": ["in", sorted(accounts)], "account_type": "Bank"}, pluck="name",
    )) if accounts else set()
    for lines in ledger.values():
        for row in lines:
            row.is_bank = row.account in banks
    return ledger


def get_statement_matches(keys):
    matches = defaultdict(list)
    if not keys:
        return matches
    payments = frappe.get_all(
        "Bank Transaction Payments",
        filters={
            "parenttype": "Bank Transaction", "parentfield": "payment_entries",
            "payment_document": ["in", ["Payment Entry", "Journal Entry"]],
            "payment_entry": ["in", sorted({name for _, name in keys})],
            "allocated_amount": [">", 0],
        },
        fields=["parent", "payment_document", "payment_entry", "allocated_amount"],
    )
    if not payments or not frappe.has_permission("Bank Transaction", "read"):
        return matches
    statements = {row.name: row for row in frappe.get_list(
        "Bank Transaction",
        filters={"name": ["in", sorted({row.parent for row in payments})], "docstatus": 1, "deposit": [">", 0]},
        fields=["name", "reference_number", "date", "currency", "bank_account"],
        limit_page_length=0,
    )}
    accounts = {row.bank_account for row in statements.values() if row.bank_account}
    bank_accounts = dict(frappe.get_all(
        "Bank Account", filters={"name": ["in", sorted(accounts)]}, fields=["name", "account"], as_list=True,
    )) if accounts else {}
    for payment in payments:
        statement = statements.get(payment.parent)
        if statement:
            matches[(payment.payment_document, payment.payment_entry)].append(
                (statement, bank_accounts.get(statement.bank_account), amount(payment.allocated_amount))
            )
    return matches


def amount(value):
    return Decimal(str(value or 0))


def reconcile_pdc(pdc, keys, vouchers, ledger, matches):
    cleared = amount(pdc.cleared_amount)
    endorsed = bool(pdc.handed_over_to_supplier or pdc.supplier_payment_entry)
    issues = []
    customer_total = supplier_total = bank_total = matched = Decimal(0)
    customer_currencies = set()
    bank_currencies = set()
    bank_keys = set()
    references = set()
    newest_statement = None
    posted = set()
    master = {pdc.payment_entry, pdc.supplier_payment_entry}

    for key in sorted(keys):
        kind, name = key
        voucher = vouchers.get(key)
        if not voucher or voucher.company != pdc.company:
            issues.append(_("Missing or wrong-company voucher {0}").format(name))
            continue
        if voucher.docstatus != 1:
            if name not in master or pdc.pdc_status == "Cleared":
                issues.append(_("Voucher {0} is not submitted").format(name))
            continue
        posted.add(name)
        if not ledger[key]:
            issues.append(_("Voucher {0} has no active ledger entries").format(name))

        if kind == "Payment Entry":
            if voucher.party_type == "Customer" and voucher.party == pdc.customer:
                customer_total += amount(voucher.paid_amount)
                customer_currencies.add(voucher.paid_from_account_currency)
            elif voucher.party_type == "Supplier" and voucher.party == pdc.handed_over_to_supplier:
                supplier_total += amount(voucher.paid_amount)
            else:
                issues.append(_("Payment Entry {0} has an unexpected party").format(name))

        if endorsed or (pdc.pdc_receivable_account and kind == "Payment Entry"):
            continue
        debits = defaultdict(Decimal)
        for line in ledger[key]:
            if line.is_bank and line.account != pdc.pdc_receivable_account:
                debits[(line.account, line.account_currency)] += (
                    amount(line.debit_in_account_currency) - amount(line.credit_in_account_currency)
                )
        for (account, currency), debit in debits.items():
            if debit <= 0:
                continue
            bank_total += debit
            bank_keys.add(key)
            bank_currencies.add(currency)
            for statement, ledger_account, allocation in matches[key]:
                if ledger_account == account and statement.currency == currency:
                    matched += allocation
                    references.add(f"{statement.reference_number} ({statement.name})" if statement.reference_number else statement.name)
                    newest_statement = max(newest_statement or statement.date, statement.date)

    if not cleared and pdc.pdc_status in {"Cleared", "Partially Cleared", "Partially Bounced"}:
        issues.append(_("PDC status shows clearance but its cleared amount is zero"))
    if not cleared and not posted and not issues:
        return None
    if customer_total != cleared:
        issues.append(_("Customer payments differ from the PDC cleared amount"))
    if endorsed and supplier_total != cleared:
        issues.append(_("Supplier payments differ from the PDC cleared amount"))

    currency = next(iter(customer_currencies)) if len(customer_currencies) == 1 else None
    comparable = bool(currency and (not bank_currencies or bank_currencies == {currency}))
    if not endorsed and cleared:
        if not bank_keys:
            issues.append(_("No bank debit was posted"))
        elif not comparable:
            issues.append(_("Bank and customer amounts use different currencies"))
        elif bank_total != cleared:
            issues.append(_("Bank postings differ from the PDC cleared amount"))
    if bank_keys and matched > bank_total:
        issues.append(_("Statement allocations exceed the posted bank amount"))

    if endorsed:
        bank_status = _("Supplier Handover")
    elif not bank_keys:
        bank_status = _("No Bank Posting")
    elif matched == bank_total:
        bank_status = _("Statement Matched")
    elif matched:
        bank_status = _("Partially Matched") if matched < bank_total else _("Overallocated")
    elif all(vouchers[key].clearance_date for key in bank_keys):
        bank_status = _("Manually Cleared")
    else:
        bank_status = _("Awaiting Statement Match")

    return {
        **pdc,
        "currency": currency,
        "customer_posted": customer_total if currency else None,
        "bank_posted": bank_total if comparable and bank_keys else None,
        "bank_matched": matched if comparable and bank_keys else None,
        "bank_status": bank_status,
        "statement_references": ", ".join(sorted(references)),
        "statement_date": newest_statement,
        "vouchers": ", ".join(sorted(posted)),
        "issues": "; ".join(issues),
        "exception": bool(issues or (bank_keys and matched < bank_total)),
    }


def get_columns():
    return [
        {"fieldname": "name", "label": _("PDC"), "fieldtype": "Link", "options": "PDC", "width": 160},
        {"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 160},
        {"fieldname": "bank_status", "label": _("Bank Status"), "fieldtype": "Data", "width": 175},
        {"fieldname": "issues", "label": _("Voucher Exceptions"), "fieldtype": "Data", "width": 320},
        {"fieldname": "cleared_amount", "label": _("PDC Cleared"), "fieldtype": "Currency", "options": "currency", "width": 115},
        {"fieldname": "customer_posted", "label": _("Customer Posted"), "fieldtype": "Currency", "options": "currency", "width": 135},
        {"fieldname": "bank_posted", "label": _("Bank Posted"), "fieldtype": "Currency", "options": "currency", "width": 115},
        {"fieldname": "bank_matched", "label": _("Statement Matched"), "fieldtype": "Currency", "options": "currency", "width": 145},
        {"fieldname": "statement_references", "label": _("Statement References"), "fieldtype": "Data", "width": 240},
        {"fieldname": "statement_date", "label": _("Latest Statement Date"), "fieldtype": "Date", "width": 145},
        {"fieldname": "vouchers", "label": _("Posted Vouchers"), "fieldtype": "Data", "width": 205},
        {"fieldname": "reference_date", "label": _("Cheque Date"), "fieldtype": "Date", "width": 110},
        {"fieldname": "company", "label": _("Company"), "fieldtype": "Link", "options": "Company", "width": 150},
        {"fieldname": "currency", "label": _("Currency"), "fieldtype": "Link", "options": "Currency", "width": 80},
    ]
