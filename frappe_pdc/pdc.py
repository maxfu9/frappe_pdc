import frappe
from frappe.utils import flt, today
from frappe import _

# -----------------------------
# Payment Entry Hook
# -----------------------------
def handle_before_submit(doc, method=None):
    """
    Called before submitting a Payment Entry.
    Prevents standard submission if 'Is PDC' is checked but no PDC record is linked.
    Allows submission for 'spin-off' PEs created during partial clearance.
    """
    if doc.is_pdc:
        # Check for bypass flag (used during partial clearance spin-off)
        if getattr(doc.flags, "ignore_pdc_check", False):
            return

        # Check if a PDC record exists and is linked
        pdc_exists = frappe.db.exists("PDC", {"payment_entry": doc.name})
        
        # If no PDC exists, it means they are bypassing the 'Register PDC' workflow
        if not pdc_exists:
            frappe.throw(_("This is a PDC. Please click 'Register PDC' in Draft mode before submitting."))


@frappe.whitelist()
def register_pdc(payment_entry_name):
    """
    Creates a PDC record from a DRAFT Payment Entry.
    Does not submit the Payment Entry, so zero GL entry.
    """
    doc = frappe.get_doc("Payment Entry", payment_entry_name)
    if not doc.is_pdc:
        frappe.throw(_("This Payment Entry is not marked as PDC"))

    if doc.party_type != "Customer":
        frappe.throw(_("PDC registration is only supported for Customer receipts."))
    
    if doc.docstatus != 0:
        frappe.throw(_("PDC can only be registered for Draft Payment Entries"))

    if not doc.reference_no or not doc.reference_date:
        frappe.throw(_("Cheque/Reference No and Cheque/Reference Date are required to register PDC."))

    duplicate_pdc = frappe.get_all(
        "PDC",
        filters={
            "cheque_no": doc.reference_no,
            "reference_date": doc.reference_date,
            "docstatus": ["<", 2],
            "payment_entry": ["!=", doc.name],
        },
        fields=["name", "payment_entry"],
        limit=1,
    )
    if duplicate_pdc:
        dup = duplicate_pdc[0]
        frappe.throw(
            _(
                "Cheque/Reference No {0} with date {1} is already registered in PDC {2} (Payment Entry {3})."
            ).format(doc.reference_no, doc.reference_date, dup.name, dup.payment_entry)
        )

    # Validate allocations match amount
    total_allocated = sum(flt(r.allocated_amount) for r in doc.references)
    if abs(flt(doc.paid_amount) - total_allocated) > 0.01:
        frappe.throw(_("Paid Amount ({0}) must match Total Allocated ({1}) before registering PDC").format(doc.paid_amount, total_allocated))

    existing = frappe.get_all("PDC", filters={"payment_entry": doc.name}, fields=["name"])
    pdc_doc = frappe.get_doc("PDC", existing[0].name) if existing else frappe.new_doc("PDC")

    # Map fields
    pdc_doc.payment_entry = doc.name
    pdc_doc.customer = doc.party
    pdc_doc.company = doc.company
    pdc_doc.amount = doc.paid_amount
    pdc_doc.reference_date = doc.reference_date
    pdc_doc.cheque_no = doc.reference_no
    pdc_doc.bank_name = doc.bank
    pdc_doc.bank_account = doc.bank_account_no
    
    # Store references in JSON and Table
    references = []
    pdc_doc.set("references_table", [])
    for ref in doc.references:
        ref_data = {
            "reference_doctype": ref.reference_doctype,
            "reference_name": ref.reference_name,
            "total_amount": ref.total_amount,
            "outstanding_amount": ref.outstanding_amount,
            "allocated_amount": ref.allocated_amount,
            "party_type": doc.party_type,
            "party": doc.party
        }
        references.append(ref_data)
        pdc_doc.append("references_table", ref_data)
    
    pdc_doc.references = frappe.as_json(references)
    pdc_doc.pdc_status = "Pending"
    pdc_doc.save(ignore_permissions=True)
    if pdc_doc.docstatus == 0:
        pdc_doc.submit()
    
    return pdc_doc.name


@frappe.whitelist()
def handle_pdc_submission(doc, method=None):
    """
    Triggered ONLY when the Payment Entry is finally submitted (during clearance).
    We don't need to create the PDC here anymore as it's created via 'register_pdc'.
    """
    pass


@frappe.whitelist()
def handle_pdc_cancellation(doc, method=None):
    """
    Triggered when a Payment Entry is cancelled.
    Cancels the linked PDC record.
    """
    if doc.is_pdc:
        pdc_list = frappe.get_all("PDC", filters={"payment_entry": doc.name}, fields=["name", "docstatus"])
        for pdc in pdc_list:
            if pdc.docstatus == 1:
                pdc_doc = frappe.get_doc("PDC", pdc.name)
                pdc_doc.cancel()
                update_linked_pdc_events_status(pdc_doc.name, "Cancelled")


@frappe.whitelist()
def handle_pdc_cancelled(doc, method=None):
    """
    Triggered when a PDC is cancelled directly from the PDC form.
    """
    update_linked_pdc_events_status(doc.name, "Cancelled")


@frappe.whitelist()
def get_pdc_name(payment_entry):
    """
    Finds the PDC record name linked to a Payment Entry (either as customer or supplier PE).
    """
    pdc = frappe.db.get_value("PDC", {"payment_entry": payment_entry}, "name")
    if not pdc:
        pdc = frappe.db.get_value("PDC", {"supplier_payment_entry": payment_entry}, "name")
    return pdc


# -----------------------------
@frappe.whitelist()
def clear_pdc(payment_entry_name, clear_amount=None, mode_of_payment=None):
    """
    Marks both Payment Entry and PDC as Cleared (or Partially Cleared) and reconciles invoices.
    Optimized for single-save transaction integrity.
    """
    # 1. FETCH & VALIDATE
    pdc_list = frappe.get_all("PDC", filters={"payment_entry": payment_entry_name}, 
                              fields=["name", "amount", "cleared_amount", "supplier_payment_entry", "handed_over_to_supplier"])
    if not pdc_list:
        frappe.throw(_("PDC record not found for this Payment Entry. Please refresh the page."))
    
    pdc_doc = frappe.get_doc("PDC", pdc_list[0].name)
    if pdc_doc.pdc_status in {"Cleared", "Bounced", "Partially Bounced"}:
        frappe.throw(_("This PDC is already finalized and cannot be cleared."))
    
    current_pe_name = pdc_doc.payment_entry
    pe_doc = frappe.get_doc("Payment Entry", current_pe_name)
    
    if pe_doc.docstatus != 0:
        frappe.throw(_("The linked Payment Entry {0} is already submitted. Please refresh the PDC record.").format(current_pe_name))
    
    total_pdc_amount = flt(pdc_doc.amount)
    previously_cleared = flt(pdc_doc.cleared_amount)
    remaining_to_clear = flt(total_pdc_amount - previously_cleared, 2)
    
    if clear_amount is None:
        clear_amount = remaining_to_clear
    else:
        clear_amount = flt(clear_amount)

    if clear_amount <= 0:
        frappe.throw(_("Clearance amount must be greater than zero."))
    if clear_amount > remaining_to_clear + 0.01:
        frappe.throw(_("Clearance amount ({0}) exceeds remaining PDC amount ({1})").format(clear_amount, remaining_to_clear))
    if mode_of_payment and not frappe.db.exists("Mode of Payment", mode_of_payment):
        frappe.throw(_("Mode of Payment {0} does not exist.").format(mode_of_payment))

    # 2. IN-MEMORY UPDATES & DELTA TRACKING (No Saves yet)
    to_distribute = clear_amount
    customer_deltas = {} # To track what we cleared in THIS call
    for row in pdc_doc.references_table:
        if to_distribute <= 0: break
        rem = flt(row.allocated_amount) - flt(row.cleared_amount)
        if rem > 0:
            alloc = min(to_distribute, rem)
            row.cleared_amount += alloc
            row.status = "Cleared" if abs(row.cleared_amount - row.allocated_amount) < 0.01 else "Partially Cleared"
            customer_deltas[row.name] = alloc
            to_distribute -= alloc

    supplier_deltas = {}
    if pdc_doc.supplier_payment_entry and pdc_doc.purchase_references:
        to_distribute_supp = clear_amount
        for row in pdc_doc.purchase_references:
            if to_distribute_supp <= 0: break
            rem = flt(row.allocated_amount) - flt(row.cleared_amount)
            if rem > 0:
                alloc = min(to_distribute_supp, rem)
                row.cleared_amount += alloc
                row.status = "Cleared" if abs(row.cleared_amount - row.allocated_amount) < 0.01 else "Partially Cleared"
                supplier_deltas[row.name] = alloc
                to_distribute_supp -= alloc

    new_total_cleared = previously_cleared + clear_amount
    new_status = "Cleared" if abs(new_total_cleared - total_pdc_amount) < 0.01 else "Partially Cleared"
    remaining_balance = total_pdc_amount - new_total_cleared

    # 3. SETTLE PAYMENT ENTRIES
    # --- CUSTOMER SIDE (Master: pe_doc) ---
    if abs(remaining_balance) > 0.01:
        # A. Create the SPIN-OFF PE for the CLEARED portion
        cleared_pe = frappe.copy_doc(pe_doc)
        cleared_pe.docstatus = 0
        cleared_pe.name = None
        cleared_pe.paid_amount = clear_amount
        cleared_pe.received_amount = clear_amount
        cleared_pe.set("references", [])
        
        for row in pdc_doc.references_table:
            delta = customer_deltas.get(row.name, 0)
            if delta > 0:
                cleared_pe.append("references", {
                    "reference_doctype": row.reference_doctype,
                    "reference_name": row.reference_name,
                    "allocated_amount": delta,
                    "total_amount": row.total_amount,
                    "outstanding_amount": row.outstanding_amount
                })
        cleared_pe.flags.ignore_pdc_check = True
        cleared_pe.insert(ignore_permissions=True)
        if mode_of_payment:
            cleared_pe.mode_of_payment = mode_of_payment
        cleared_pe.submit()
        
        # B. Adjust the ORIGINAL PE (Master Draft) to the REMAINDER
        pe_doc.paid_amount = remaining_balance
        pe_doc.received_amount = remaining_balance
        pe_doc.set("references", [])
        for row in pdc_doc.references_table:
            rem_on_row = flt(row.allocated_amount) - flt(row.cleared_amount)
            if rem_on_row > 0:
                pe_doc.append("references", {
                    "reference_doctype": row.reference_doctype,
                    "reference_name": row.reference_name,
                    "allocated_amount": rem_on_row,
                    "total_amount": row.total_amount,
                    "outstanding_amount": row.outstanding_amount
                })
        pe_doc.save(ignore_permissions=True)
        pdc_doc.payment_entry = pe_doc.name
    else:
        # FULL CLEARANCE
        pe_doc.paid_amount = remaining_to_clear 
        pe_doc.received_amount = remaining_to_clear
        pe_doc.set("references", [])
        for row in pdc_doc.references_table:
            delta = customer_deltas.get(row.name, 0)
            if delta > 0:
                pe_doc.append("references", {
                    "reference_doctype": row.reference_doctype,
                    "reference_name": row.reference_name,
                    "allocated_amount": delta,
                    "total_amount": row.total_amount,
                    "outstanding_amount": row.outstanding_amount
                })
        if mode_of_payment:
            pe_doc.mode_of_payment = mode_of_payment
        pe_doc.save(ignore_permissions=True)
        pe_doc.submit()

    # --- SUPPLIER SIDE (if handed over) ---
    if pdc_doc.supplier_payment_entry:
        supp_pe = frappe.get_doc("Payment Entry", pdc_doc.supplier_payment_entry)
        if supp_pe.docstatus == 0:
            if abs(remaining_balance) > 0.01:
                # SPIN-OFF Supplier PE
                cleared_supp_pe = frappe.copy_doc(supp_pe)
                cleared_supp_pe.docstatus = 0
                cleared_supp_pe.name = None
                cleared_supp_pe.paid_amount = clear_amount
                cleared_supp_pe.received_amount = clear_amount
                cleared_supp_pe.set("references", [])
                
                for row in pdc_doc.purchase_references:
                    delta = supplier_deltas.get(row.name, 0)
                    if delta > 0:
                        cleared_supp_pe.append("references", {
                            "reference_doctype": row.reference_doctype,
                            "reference_name": row.reference_name,
                            "allocated_amount": delta,
                            "total_amount": row.total_amount,
                            "outstanding_amount": row.outstanding_amount
                        })
                cleared_supp_pe.flags.ignore_pdc_check = True
                cleared_supp_pe.insert(ignore_permissions=True)
                if mode_of_payment:
                    cleared_supp_pe.mode_of_payment = mode_of_payment
                cleared_supp_pe.submit()
                
                # Adjust Master Supplier Draft
                supp_pe.paid_amount = remaining_balance
                supp_pe.received_amount = remaining_balance
                supp_pe.set("references", [])
                for row in pdc_doc.purchase_references:
                    rem_on_row = flt(row.allocated_amount) - flt(row.cleared_amount)
                    if rem_on_row > 0:
                        supp_pe.append("references", {
                            "reference_doctype": row.reference_doctype,
                            "reference_name": row.reference_name,
                            "allocated_amount": rem_on_row,
                            "total_amount": row.total_amount,
                            "outstanding_amount": row.outstanding_amount
                        })
                supp_pe.save(ignore_permissions=True)
                pdc_doc.supplier_payment_entry = supp_pe.name
            else:
                # FULL CLEARANCE Supplier
                supp_pe.paid_amount = remaining_to_clear
                supp_pe.received_amount = remaining_to_clear
                supp_pe.set("references", [])
                for row in pdc_doc.purchase_references:
                    delta = supplier_deltas.get(row.name, 0)
                    if delta > 0:
                        supp_pe.append("references", {
                            "reference_doctype": row.reference_doctype,
                            "reference_name": row.reference_name,
                            "allocated_amount": delta,
                            "total_amount": row.total_amount,
                            "outstanding_amount": row.outstanding_amount
                        })
                if mode_of_payment:
                    supp_pe.mode_of_payment = mode_of_payment
                supp_pe.save(ignore_permissions=True)
                supp_pe.submit()

    # 4. FINAL SAVE
    pdc_doc.cleared_amount = new_total_cleared
    pdc_doc.pdc_status = new_status
    pdc_doc.save(ignore_permissions=True)
    
    # Sync status
    frappe.db.set_value("Payment Entry", pe_doc.name, "pdc_status", new_status, update_modified=False)
    if pdc_doc.supplier_payment_entry:
        frappe.db.set_value("Payment Entry", pdc_doc.supplier_payment_entry, "pdc_status", new_status, update_modified=False)

    if new_status == "Cleared":
        update_linked_pdc_events_status(pdc_doc.name, "Completed")

    frappe.db.commit()


def update_linked_pdc_events_status(pdc_name, target_status):
    """
    Update status of all Event records linked to this PDC.
    Uses doc.save() so standard Event hooks (including Google sync logic) can run.
    """
    if target_status not in {"Open", "Completed", "Closed", "Cancelled"}:
        frappe.throw(_("Invalid Event status: {0}").format(target_status))

    events = frappe.get_all(
        "Event",
        filters={
            "reference_doctype": "PDC",
            "reference_docname": pdc_name,
            "docstatus": ["<", 2],
            "status": ["!=", target_status],
        },
        fields=["name"],
    )

    for event in events:
        try:
            event_doc = frappe.get_doc("Event", event.name)
            event_doc.status = target_status
            event_doc.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Failed to set Event {event.name} to {target_status} for PDC {pdc_name}",
            )


def get_unpaid_invoices(party, party_type, company):
    doctype = "Purchase Invoice" if party_type == "Supplier" else "Sales Invoice"
    filter_field = "supplier" if party_type == "Supplier" else "customer"
    results = frappe.get_all(doctype, 
        filters={
            filter_field: party,
            "company": company,
            "docstatus": 1,
            "outstanding_amount": [">", 0]
        },
        fields=["name", "outstanding_amount", "grand_total"],
        order_by="posting_date asc"
    )
    for res in results:
        res.doctype = doctype
    return results


def reconcile_payment(pe_doc, ref, amt_to_reconcile):
    from erpnext.accounts.utils import reconcile_against_document
    
    outstanding = frappe.db.get_value(ref["reference_doctype"], ref["reference_name"], "outstanding_amount")
    if not outstanding or outstanding <= 0:
        return

    amt = flt(min(amt_to_reconcile, outstanding))
    if amt <= 0: return
    
    party_account = frappe.db.get_value(
        ref["reference_doctype"], 
        ref["reference_name"], 
        "debit_to" if ref["reference_doctype"] == "Sales Invoice" else "credit_to"
    )
    grand_total = frappe.db.get_value(ref["reference_doctype"], ref["reference_name"], "grand_total")

    try:
        reconcile_against_document([frappe._dict({
            "voucher_type": "Payment Entry",
            "voucher_no": pe_doc.name,
            "voucher_detail_no": None,
            "party_type": ref.get("party_type") or pe_doc.party_type,
            "party": ref.get("party") or pe_doc.party,
            "unreconciled_amount": pe_doc.unallocated_amount,
            "unadjusted_amount": pe_doc.unallocated_amount,
            "against_voucher_type": ref["reference_doctype"],
            "against_voucher": ref["reference_name"],
            "allocated_amount": amt,
            "grand_total": grand_total,
            "outstanding_amount": outstanding,
            "account": party_account,
            "difference_amount": 0,
            "difference_posting_date": pe_doc.posting_date,
            "exchange_rate": pe_doc.target_exchange_rate if pe_doc.payment_type == "Receive" else pe_doc.source_exchange_rate,
            "precision": frappe.get_precision("Payment Entry Reference", "allocated_amount")
        })])
        pe_doc.load_from_db() 
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "PDC Reconciliation Error")
        frappe.msgprint(_("Warning: Could not fully reconcile {0}: {1}").format(ref['reference_name'], str(e)))


@frappe.whitelist()
def handover_pdc(payment_entry_name, supplier, handover_date):
    """
    Marks PDC as 'Handed Over' and creates a corresponding Pay-type Payment Entry for the supplier.
    """
    pdc_list = frappe.get_all("PDC", filters={"payment_entry": payment_entry_name}, fields=["name", "amount", "company", "bank_name", "bank_account"])
    if not pdc_list:
        frappe.throw(_("PDC record not found"))
    
    pdc_name = pdc_list[0].name
    pdc_amt = pdc_list[0].amount
    company = pdc_list[0].company

    # Create Supplier Payment Entry (Type: Pay)
    supp_pe = frappe.new_doc("Payment Entry")
    supp_pe.payment_type = "Pay"
    supp_pe.party_type = "Supplier"
    supp_pe.party = supplier
    supp_pe.company = company
    supp_pe.paid_amount = pdc_amt
    supp_pe.received_amount = pdc_amt
    supp_pe.reference_no = frappe.db.get_value("PDC", pdc_name, "cheque_no")
    supp_pe.reference_date = handover_date
    supp_pe.pdc_status = "Handed Over"
    # This Payment Entry should remain a normal supplier payout and must not be forced
    # through the customer PDC registration gate.
    supp_pe.is_pdc = 0
    
    # We need to set accounts.
    # For a 'Pay' PE:
    # Paid From: Usually a Bank/Cash account.
    # Paid To: Supplier Payable account.
    
    # In an endorsement, we are effectively paying the supplier with the cheque we received.
    # This is a bit non-standard. Usually, it's:
    # Dr Supplier, Cr Customer? (No, PE doesn't do that directly).
    # Standard flow: 
    # 1. Received from Customer: Dr PDC/Bank Account, Cr Customer.
    # 2. Endorsed to Supplier: Dr Supplier, Cr PDC/Bank Account.
    
    # Let's use the same bank account/PDC account as the original PE.
    orig_pe = frappe.get_doc("Payment Entry", payment_entry_name)
    supp_pe.mode_of_payment = orig_pe.mode_of_payment
    supp_pe.paid_from = orig_pe.paid_to
    supp_pe.paid_from_account_currency = orig_pe.paid_to_account_currency
    
    # Fetch Supplier default payable account
    supp_pe.paid_to = frappe.get_value("Party Account", {"parent": supplier, "company": company}, "account") or \
                      frappe.get_value("Company", company, "default_payable_account")
    
    # Populate references_table with Supplier Invoices (APPEND to existing Customer Invoices)
    # Populate purchase_references with Supplier Invoices
    pdc_doc = frappe.get_doc("PDC", pdc_name)
    pdc_doc.pdc_status = "Handed Over"
    pdc_doc.handed_over_to_supplier = supplier
    pdc_doc.handover_date = handover_date
    pdc_doc.supplier_payment_entry = None 
    
    pdc_doc.set("purchase_references", [])
    unpaid_invoices = get_unpaid_invoices(supplier, "Supplier", company)
    
    to_distribute = pdc_amt
    for inv in unpaid_invoices:
        if to_distribute <= 0: break
        alloc = min(to_distribute, inv.outstanding_amount)
        
        # Add to PDC Purchase Table
        pdc_doc.append("purchase_references", {
            "reference_doctype": inv.doctype,
            "reference_name": inv.name,
            "total_amount": inv.grand_total,
            "outstanding_amount": inv.outstanding_amount,
            "allocated_amount": alloc,
            "party_type": "Supplier",
            "party": supplier
        })
        
        # Add to Supplier Payment Entry (Native)
        supp_pe.append("references", {
            "reference_doctype": inv.doctype,
            "reference_name": inv.name,
            "allocated_amount": alloc,
            "total_amount": inv.grand_total,
            "outstanding_amount": inv.outstanding_amount
        })
        to_distribute -= alloc
    
    # Keep the supplier PE in Draft at handover time.
    # It will be submitted/cancelled later based on actual clearance/bounce outcome.
    supp_pe.save(ignore_permissions=True)
    
    pdc_doc.supplier_payment_entry = supp_pe.name
    pdc_doc.save(ignore_permissions=True)

    # Rename PDC to "Customer - Supplier"
    customer = frappe.db.get_value("PDC", pdc_name, "customer")
    new_pdc_name = f"{customer} - {supplier}"
    
    # Handle duplicates if renaming
    if frappe.db.exists("PDC", new_pdc_name):
        new_pdc_name = frappe.model.naming.make_autoname(new_pdc_name + "-.#####")
    
    frappe.rename_doc("PDC", pdc_name, new_pdc_name, force=True)
    
    frappe.db.set_value("Payment Entry", payment_entry_name, "pdc_status", "Handed Over")
    frappe.db.commit()
    return supp_pe.name


@frappe.whitelist()
def mark_pdc_bounced(payment_entry_name, reason=None):
    pdc_list = frappe.get_all("PDC", filters={"payment_entry": payment_entry_name}, fields=["name", "pdc_status"])
    if not pdc_list:
        frappe.throw(_("PDC record not found"))
    
    if pdc_list[0].pdc_status == "Cleared":
        frappe.throw(_("This PDC is already cleared and cannot be marked as bounced."))
    if pdc_list[0].pdc_status in {"Bounced", "Partially Bounced"}:
        frappe.throw(_("This PDC is already marked as bounced."))

    pdc_doc = frappe.get_doc("PDC", pdc_list[0].name)
    target_status = "Partially Bounced" if flt(pdc_doc.cleared_amount) > 0 else "Bounced"

    def handle_payment_entry_on_bounce(pe_name, link_field):
        if not pe_name or not frappe.db.exists("Payment Entry", pe_name):
            return "missing"

        pe_doc = frappe.get_doc("Payment Entry", pe_name)

        # Draft PE can be removed entirely because no GL is posted yet.
        if pe_doc.docstatus == 0:
            try:
                # Unlink first to satisfy linked-doc delete checks.
                frappe.db.set_value("PDC", pdc_doc.name, link_field, None, update_modified=False)
                frappe.delete_doc("Payment Entry", pe_name, ignore_permissions=True)
                return "deleted"
            except Exception:
                # Keep working by marking status if deletion is blocked by links.
                frappe.log_error(frappe.get_traceback(), f"PDC Bounce PE Delete Failed: {pe_name}")
                frappe.db.set_value("PDC", pdc_doc.name, link_field, pe_name, update_modified=False)
                frappe.db.set_value("Payment Entry", pe_name, "pdc_status", target_status, update_modified=False)
                return "kept-draft"

        # Submitted PE must be cancelled to reverse accounting impact.
        if pe_doc.docstatus == 1:
            # Unlink first to satisfy linked-doc cancel checks.
            frappe.db.set_value("PDC", pdc_doc.name, link_field, None, update_modified=False)
            pe_doc.cancel()
            # Keep audit trail by re-linking the cancelled entry.
            frappe.db.set_value("PDC", pdc_doc.name, link_field, pe_name, update_modified=False)
            frappe.db.set_value("Payment Entry", pe_name, "pdc_status", target_status, update_modified=False)
            return "cancelled"

        # Already cancelled
        frappe.db.set_value("Payment Entry", pe_name, "pdc_status", target_status, update_modified=False)
        return "already-cancelled"

    customer_pe_action = handle_payment_entry_on_bounce(pdc_doc.payment_entry, "payment_entry")
    supplier_pe_action = handle_payment_entry_on_bounce(
        pdc_doc.supplier_payment_entry, "supplier_payment_entry"
    )

    pdc_updates = {
        "pdc_status": target_status,
        "bounced_reason": reason,
    }
    if customer_pe_action == "deleted":
        pdc_updates["payment_entry"] = None
    if supplier_pe_action == "deleted":
        pdc_updates["supplier_payment_entry"] = None

    frappe.db.set_value("PDC", pdc_doc.name, pdc_updates)
    frappe.db.commit()
    return {
        "customer_payment_entry": customer_pe_action,
        "supplier_payment_entry": supplier_pe_action,
    }

# -----------------------------
# Daily Scheduler
# -----------------------------
def mark_matured_pdc():
    today_date = today()
    pdcs = frappe.get_all(
        "PDC",
        filters={"docstatus": 1, "reference_date": ["<=", today_date], "pdc_status": "Pending"},
        fields=["name", "payment_entry", "customer", "amount", "reference_date", "cheque_no"],
    )

    for pdc in pdcs:
        frappe.db.set_value("PDC", pdc.name, "pdc_status", "Ready for Clearance")
        if pdc.payment_entry:
            frappe.db.set_value("Payment Entry", pdc.payment_entry, "pdc_status", "Ready for Clearance")
        notify_accounts_manager(pdc)
        notify_customer(pdc)
    frappe.db.commit()


def get_accounts_managers():
    return frappe.get_all("Has Role", filters={"role": "Accounts Manager"}, fields=["parent"], pluck="parent")


def notify_accounts_manager(pdc):
    managers = get_accounts_managers()
    if not managers: return
    subject = _("PDC Ready for Clearance: {0}").format(pdc.name)
    message = f"""
        <div style="font-family: sans-serif; padding: 20px;">
            <h2 style="color: #444;">{_('Post-Dated Cheque Reminder')}</h2>
            <p>{_('The following PDC is dated today and is ready for clearance:')}</p>
            <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
                <tr><td style="padding: 8px; border: 1px solid #ddd;"><b>PDC Number</b></td><td style="padding: 8px; border: 1px solid #ddd;">{pdc.name}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd;"><b>Customer</b></td><td style="padding: 8px; border: 1px solid #ddd;">{pdc.customer}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd;"><b>Cheque No</b></td><td style="padding: 8px; border: 1px solid #ddd;">{pdc.cheque_no or 'N/A'}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd;"><b>Amount</b></td><td style="padding: 8px; border: 1px solid #ddd;">{frappe.format_value(pdc.amount, {'fieldtype': 'Currency'})}</td></tr>
                <tr><td style="padding: 8px; border: 1px solid #ddd;"><b>Payment Entry</b></td><td style="padding: 8px; border: 1px solid #ddd;">{pdc.payment_entry}</td></tr>
            </table>
            <p style="margin-top: 20px;">{_('Please take appropriate action in the system.')}</p>
        </div>
    """
    frappe.sendmail(recipients=managers, subject=subject, message=message)


def notify_customer(pdc):
    customer_email = frappe.db.get_value("Customer", pdc.customer, "email_id")
    if not customer_email:
        # Fallback to primary contact
        contact_email = frappe.db.sql("""
            select email_id from tabContact c 
            join `tabDynamic Link` dl on dl.parent = c.name
            where dl.link_doctype = 'Customer' and dl.link_name = %s and c.is_primary_contact = 1
        """, (pdc.customer,))
        if contact_email:
            customer_email = contact_email[0][0]

    if not customer_email: return
    subject = _("Cheque Presentation Reminder - {0}").format(pdc.name)
    message = f"""
        <div style="font-family: sans-serif; padding: 20px; line-height: 1.6;">
            <p>{_('Dear Valued Customer,')}</p>
            <p>{_('This is a polite reminder that your cheque')} <b>#{pdc.cheque_no or ''}</b> {_('for the amount of')} 
            <b>{frappe.format_value(pdc.amount, {'fieldtype': 'Currency'})}</b> {_('is dated today,')} 
            <b>{frappe.format_date(pdc.reference_date)}</b>, {_('and will be presented for clearance.')}</p>
            <p>{_('Please ensure sufficient funds are available in your account.')}</p>
            <p>{_('Thank you for your business!')}</p>
        </div>
    """
    frappe.sendmail(recipients=[customer_email], subject=subject, message=message)


@frappe.whitelist()
def ensure_pdc_calendar_client_script():
    script = """
frappe.ui.form.on('PDC', {
    refresh(frm) {
        if (frm.__pdc_calendar_btn_added) return;
        frm.__pdc_calendar_btn_added = true;

        frm.add_custom_button(__('Add to Calendar'), () => {
            if (!frm.doc.reference_date) {
                frappe.msgprint(__('Reference Date is required to create a calendar event.'));
                return;
            }

            const title = __('PDC Follow-up: {0}', [frm.doc.cheque_no || frm.doc.name]);
            const date = frm.doc.reference_date;
            const ymd = date.split('-').join('');
            const ymd_next = frappe.datetime.add_days(date, 1).split('-').join('');
            const details = [
                __('PDC: {0}', [frm.doc.name]),
                __('Cheque/Reference No: {0}', [frm.doc.cheque_no || __('N/A')]),
                __('Customer: {0}', [frm.doc.customer || __('N/A')]),
                __('Amount: {0}', [format_currency(flt(frm.doc.amount), frm.doc.currency)]),
                __('Status: {0}', [frm.doc.pdc_status || __('N/A')])
            ].join('\\n');

            const google = `https://calendar.google.com/calendar/render?action=TEMPLATE&text=${encodeURIComponent(title)}&dates=${ymd}/${ymd_next}&details=${encodeURIComponent(details)}`;
            const outlook = `https://outlook.live.com/calendar/0/deeplink/compose?path=/calendar/action/compose&rru=addevent&subject=${encodeURIComponent(title)}&startdt=${date}&enddt=${frappe.datetime.add_days(date, 1)}&allday=true&body=${encodeURIComponent(details)}`;
            const yahoo = `https://calendar.yahoo.com/?v=60&view=d&type=20&title=${encodeURIComponent(title)}&st=${ymd}&et=${ymd_next}&desc=${encodeURIComponent(details)}`;

            const d = new frappe.ui.Dialog({
                title: __('Add to Calendar'),
                fields: [{
                    fieldtype: 'HTML',
                    fieldname: 'calendar_html',
                    options: `
                        <div style="line-height:1.8;">
                            <div><b>${__('Reference Date')}:</b> ${frappe.datetime.str_to_user(date)}</div>
                            <div><b>${__('Event Title')}:</b> ${frappe.utils.escape_html(title)}</div>
                            <div style="margin-top: 10px; display:flex; gap:8px; flex-wrap:wrap;">
                                <a class="btn btn-sm btn-default" target="_blank" rel="noopener noreferrer" href="${google}">${__('Google Calendar')}</a>
                                <a class="btn btn-sm btn-default" target="_blank" rel="noopener noreferrer" href="${outlook}">${__('Outlook Calendar')}</a>
                                <a class="btn btn-sm btn-default" target="_blank" rel="noopener noreferrer" href="${yahoo}">${__('Yahoo Calendar')}</a>
                            </div>
                        </div>
                    `
                }],
                primary_action_label: __('Close'),
                primary_action() { d.hide(); }
            });
            d.show();
        });
    }
});
"""

    target = frappe.db.get_value("Client Script", {"name": "PDC Add to Calendar"}, "name")
    if target:
        doc = frappe.get_doc("Client Script", target)
        doc.dt = "PDC"
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": "PDC Add to Calendar",
                "dt": "PDC",
                "view": "Form",
                "enabled": 1,
                "script": script,
                "script_type": "Client",
            }
        )
        doc.insert(ignore_permissions=True)

    return doc.name
