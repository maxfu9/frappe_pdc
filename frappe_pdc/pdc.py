import frappe
from frappe.utils import flt, now_datetime, today
from frappe import _

PDC_MANAGER_DEFAULT_ROLES = ("Accounts Manager", "System Manager")
PDC_FINAL_STATUSES = {"Cleared", "Bounced", "Partially Bounced"}


def _split_roles(value):
    roles = []
    for role in (value or "").replace(",", "\n").splitlines():
        role = role.strip()
        if role:
            roles.append(role)
    return roles


def get_pdc_settings():
    try:
        return frappe.get_single("PDC Settings")
    except Exception:
        return frappe._dict()


def get_pdc_manager_roles():
    settings = get_pdc_settings()
    return _split_roles(getattr(settings, "manager_roles", None)) or list(PDC_MANAGER_DEFAULT_ROLES)


def _settings_flag(fieldname, default=0):
    settings = get_pdc_settings()
    value = getattr(settings, fieldname, None)
    return int(default if value is None else value)


def _require_pdc_manager(action):
    if frappe.session.user == "Administrator":
        return

    allowed_roles = set(get_pdc_manager_roles())
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not allowed_roles.intersection(user_roles):
        frappe.throw(
            _("Only users with one of these roles can {0}: {1}").format(
                action, ", ".join(sorted(allowed_roles))
            ),
            frappe.PermissionError,
        )


def _require_doc_permission(doc, permtype="read"):
    if not frappe.has_permission(doc.doctype, ptype=permtype, doc=doc):
        frappe.throw(
            _("You do not have {0} permission for {1} {2}.").format(
                permtype, doc.doctype, doc.name
            ),
            frappe.PermissionError,
        )


def _get_payment_entry(payment_entry_name, permtype="read"):
    doc = frappe.get_doc("Payment Entry", payment_entry_name)
    _require_doc_permission(doc, permtype)
    return doc


def _get_pdc_by_payment_entry(payment_entry_name, lock=False):
    if lock:
        rows = frappe.db.sql(
            """
            select name
            from `tabPDC`
            where payment_entry = %s and docstatus < 2
            for update
            """,
            (payment_entry_name,),
            as_dict=True,
        )
    else:
        rows = frappe.get_list(
            "PDC",
            filters={"payment_entry": payment_entry_name, "docstatus": ["<", 2]},
            fields=["name"],
            limit=1,
        )

    if not rows:
        frappe.throw(_("PDC record not found for Payment Entry {0}.").format(payment_entry_name))

    pdc_doc = frappe.get_doc("PDC", rows[0].name)
    _require_doc_permission(pdc_doc, "read")
    return pdc_doc


def _save_with_pdc_bypass(doc):
    # All callers are internal PDC workflow paths after role and source-document checks.
    # The bypass is needed so submitted PDC metadata and linked Payment Entries can stay in sync.
    doc.save(ignore_permissions=True)


def _insert_with_pdc_bypass(doc):
    # All callers are internal PDC workflow paths after role and source-document checks.
    # The bypass is needed to create system-generated split Payment Entries atomically.
    doc.insert(ignore_permissions=True)


def _delete_with_pdc_bypass(doctype, name):
    # All callers are internal PDC workflow paths after role and source-document checks.
    # The bypass is limited to draft Payment Entries created or controlled by the PDC workflow.
    frappe.delete_doc(doctype, name, ignore_permissions=True)


def _append_pdc_activity(
    pdc_doc,
    action,
    old_status=None,
    new_status=None,
    details=None,
    reference_doctype=None,
    reference_name=None,
):
    if not pdc_doc.meta.has_field("status_history"):
        return

    pdc_doc.append(
        "status_history",
        {
            "action": action,
            "old_status": old_status,
            "new_status": new_status,
            "user": frappe.session.user,
            "timestamp": now_datetime(),
            "details": details,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
        },
    )


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


def handle_payment_entry_update(doc, method=None):
    """
    Keep linked PDC references in sync when a draft Payment Entry references change.
    - Customer PE -> PDC.references_table
    - Supplier PE -> PDC.purchase_references
    """
    if doc.docstatus != 0:
        return

    if getattr(doc.flags, "skip_pdc_sync", False):
        return

    pdc_name = frappe.db.get_value("PDC", {"payment_entry": doc.name}, "name")
    table_field = "references_table"
    is_customer_side = True

    if not pdc_name:
        pdc_name = frappe.db.get_value("PDC", {"supplier_payment_entry": doc.name}, "name")
        table_field = "purchase_references"
        is_customer_side = False

    if not pdc_name:
        return

    pdc_doc = frappe.get_doc("PDC", pdc_name)
    if pdc_doc.docstatus == 2:
        return

    existing_rows = {
        (row.reference_doctype, row.reference_name): row
        for row in (pdc_doc.get(table_field) or [])
    }

    pdc_doc.set(table_field, [])
    references_payload = []

    for ref in doc.references:
        key = (ref.reference_doctype, ref.reference_name)
        prev = existing_rows.get(key)
        prev_cleared = flt(prev.cleared_amount) if prev else 0
        alloc = flt(ref.allocated_amount)
        cleared = min(prev_cleared, alloc) if alloc > 0 else 0
        status = "Cleared" if alloc > 0 and abs(cleared - alloc) < 0.01 else ("Partially Cleared" if cleared > 0 else "Pending")

        row = {
            "reference_doctype": ref.reference_doctype,
            "reference_name": ref.reference_name,
            "total_amount": ref.total_amount,
            "outstanding_amount": ref.outstanding_amount,
            "allocated_amount": ref.allocated_amount,
            "party_type": doc.party_type,
            "party": doc.party,
            "cleared_amount": cleared,
            "status": status,
        }
        pdc_doc.append(table_field, row)

        if is_customer_side:
            references_payload.append(
                {
                    "reference_doctype": ref.reference_doctype,
                    "reference_name": ref.reference_name,
                    "total_amount": ref.total_amount,
                    "outstanding_amount": ref.outstanding_amount,
                    "allocated_amount": ref.allocated_amount,
                    "party_type": doc.party_type,
                    "party": doc.party,
                }
            )

    if is_customer_side:
        pdc_doc.amount = doc.paid_amount
        pdc_doc.references = frappe.as_json(references_payload)

    _save_with_pdc_bypass(pdc_doc)


@frappe.whitelist()
def register_pdc(payment_entry_name):
    """
    Creates a PDC record from a DRAFT Payment Entry.
    Does not submit the Payment Entry, so zero GL entry.
    """
    _require_pdc_manager(_("register PDCs"))
    doc = _get_payment_entry(payment_entry_name, "write")
    if not doc.is_pdc:
        frappe.throw(_("This Payment Entry is not marked as PDC"))

    if doc.party_type != "Customer":
        frappe.throw(_("PDC registration is only supported for Customer receipts."))
    
    if doc.docstatus != 0:
        frappe.throw(_("PDC can only be registered for Draft Payment Entries"))

    if not doc.reference_no or not doc.reference_date:
        frappe.throw(_("Cheque/Reference No and Cheque/Reference Date are required to register PDC."))

    duplicate_filters = {
        "cheque_no": doc.reference_no,
        "reference_date": doc.reference_date,
        "company": doc.company,
        "docstatus": ["<", 2],
        "payment_entry": ["!=", doc.name],
    }
    if doc.bank_account_no:
        duplicate_filters["bank_account"] = doc.bank_account_no
    elif doc.bank:
        duplicate_filters["bank_name"] = doc.bank

    duplicate_pdc = frappe.get_list(
        "PDC",
        filters=duplicate_filters,
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

    existing = frappe.get_list("PDC", filters={"payment_entry": doc.name}, fields=["name"])
    pdc_doc = frappe.get_doc("PDC", existing[0].name) if existing else frappe.new_doc("PDC")
    if existing:
        _require_doc_permission(pdc_doc, "write")

    # Map fields
    old_status = pdc_doc.pdc_status
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
    _append_pdc_activity(
        pdc_doc,
        "Registered",
        old_status=old_status,
        new_status="Pending",
        reference_doctype="Payment Entry",
        reference_name=doc.name,
    )
    _save_with_pdc_bypass(pdc_doc)
    if pdc_doc.docstatus == 0:
        pdc_doc.submit()
    
    return pdc_doc.name


def handle_pdc_submission(doc, method=None):
    """
    Triggered ONLY when the Payment Entry is finally submitted (during clearance).
    We don't need to create the PDC here anymore as it's created via 'register_pdc'.
    """
    pass


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
    pe_doc = _get_payment_entry(payment_entry, "read")
    pdc = frappe.db.get_value("PDC", {"payment_entry": pe_doc.name}, "name")
    if not pdc:
        pdc = frappe.db.get_value("PDC", {"supplier_payment_entry": pe_doc.name}, "name")
    if pdc:
        pdc_doc = frappe.get_doc("PDC", pdc)
        _require_doc_permission(pdc_doc, "read")
    return pdc


# -----------------------------
@frappe.whitelist()
def clear_pdc(payment_entry_name, clear_amount=None, mode_of_payment=None):
    """
    Marks both Payment Entry and PDC as Cleared (or Partially Cleared) and reconciles invoices.
    Optimized for single-save transaction integrity.
    """
    # 1. FETCH & VALIDATE
    _require_pdc_manager(_("clear PDCs"))
    pdc_doc = _get_pdc_by_payment_entry(payment_entry_name, lock=True)
    _require_doc_permission(pdc_doc, "write")
    old_status = pdc_doc.pdc_status

    if pdc_doc.pdc_status in PDC_FINAL_STATUSES:
        frappe.throw(_("This PDC is already finalized and cannot be cleared."))
    if _settings_flag("require_clearance_approval") and pdc_doc.pdc_status != "Approved for Clearance":
        frappe.throw(_("This PDC must be approved for clearance before clearing."))
    
    current_pe_name = pdc_doc.payment_entry
    pe_doc = _get_payment_entry(current_pe_name, "write")
    pe_doc.flags.skip_pdc_sync = True
    
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
        cleared_pe.flags.skip_pdc_sync = True
        if mode_of_payment:
            cleared_pe.mode_of_payment = mode_of_payment
        _insert_with_pdc_bypass(cleared_pe)
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
        _save_with_pdc_bypass(pe_doc)
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
        _save_with_pdc_bypass(pe_doc)
        pe_doc.submit()

    # --- SUPPLIER SIDE (if handed over) ---
    if pdc_doc.supplier_payment_entry:
        supp_pe = _get_payment_entry(pdc_doc.supplier_payment_entry, "write")
        supp_pe.flags.skip_pdc_sync = True
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
                cleared_supp_pe.flags.skip_pdc_sync = True
                if mode_of_payment:
                    cleared_supp_pe.mode_of_payment = mode_of_payment
                _insert_with_pdc_bypass(cleared_supp_pe)
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
                _save_with_pdc_bypass(supp_pe)
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
                _save_with_pdc_bypass(supp_pe)
                supp_pe.submit()

    # 4. FINAL SAVE
    pdc_doc.cleared_amount = new_total_cleared
    pdc_doc.pdc_status = new_status
    _append_pdc_activity(
        pdc_doc,
        "Cleared" if new_status == "Cleared" else "Partially Cleared",
        old_status=old_status,
        new_status=new_status,
        details=_("Cleared amount: {0}").format(clear_amount),
        reference_doctype="Payment Entry",
        reference_name=pe_doc.name,
    )
    _save_with_pdc_bypass(pdc_doc)
    
    # Sync status
    frappe.db.set_value("Payment Entry", pe_doc.name, "pdc_status", new_status, update_modified=False)
    if pdc_doc.supplier_payment_entry:
        frappe.db.set_value("Payment Entry", pdc_doc.supplier_payment_entry, "pdc_status", new_status, update_modified=False)

    if new_status == "Cleared":
        update_linked_pdc_events_status(pdc_doc.name, "Completed")


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
            _save_with_pdc_bypass(event_doc)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Failed to set Event {event.name} to {target_status} for PDC {pdc_name}",
            )


def get_unpaid_invoices(party, party_type, company):
    doctype = "Purchase Invoice" if party_type == "Supplier" else "Sales Invoice"
    filter_field = "supplier" if party_type == "Supplier" else "customer"
    results = frappe.get_list(doctype,
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
    _require_pdc_manager(_("handover PDCs"))
    pdc_doc = _get_pdc_by_payment_entry(payment_entry_name, lock=True)
    _require_doc_permission(pdc_doc, "write")
    if pdc_doc.pdc_status in PDC_FINAL_STATUSES:
        frappe.throw(_("Finalized PDCs cannot be handed over."))
    if not frappe.db.exists("Supplier", supplier):
        frappe.throw(_("Supplier {0} does not exist.").format(supplier))
    if not handover_date:
        frappe.throw(_("Handover Date is required."))

    old_status = pdc_doc.pdc_status
    pdc_name = pdc_doc.name
    pdc_amt = pdc_doc.amount
    company = pdc_doc.company

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
    orig_pe = _get_payment_entry(payment_entry_name, "write")
    supp_pe.mode_of_payment = orig_pe.mode_of_payment
    supp_pe.paid_from = orig_pe.paid_to
    supp_pe.paid_from_account_currency = orig_pe.paid_to_account_currency
    
    # Fetch Supplier default payable account
    supp_pe.paid_to = frappe.get_value("Party Account", {"parent": supplier, "company": company}, "account") or \
                      frappe.get_value("Company", company, "default_payable_account")
    
    # Populate references_table with Supplier Invoices (APPEND to existing Customer Invoices)
    # Populate purchase_references with Supplier Invoices
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
    _save_with_pdc_bypass(supp_pe)
    
    pdc_doc.supplier_payment_entry = supp_pe.name
    _append_pdc_activity(
        pdc_doc,
        "Handed Over",
        old_status=old_status,
        new_status="Handed Over",
        details=_("Handed over to supplier {0} on {1}").format(supplier, handover_date),
        reference_doctype="Payment Entry",
        reference_name=supp_pe.name,
    )
    _save_with_pdc_bypass(pdc_doc)
    
    frappe.db.set_value("Payment Entry", payment_entry_name, "pdc_status", "Handed Over")
    return supp_pe.name


@frappe.whitelist()
def mark_pdc_bounced(payment_entry_name, reason=None):
    _require_pdc_manager(_("mark PDCs as bounced"))
    pdc_doc = _get_pdc_by_payment_entry(payment_entry_name, lock=True)
    _require_doc_permission(pdc_doc, "write")
    old_status = pdc_doc.pdc_status

    if pdc_doc.pdc_status == "Cleared":
        frappe.throw(_("This PDC is already cleared and cannot be marked as bounced."))
    if pdc_doc.pdc_status in {"Bounced", "Partially Bounced"}:
        frappe.throw(_("This PDC is already marked as bounced."))

    target_status = "Partially Bounced" if flt(pdc_doc.cleared_amount) > 0 else "Bounced"

    def handle_payment_entry_on_bounce(pe_name, link_field):
        if not pe_name or not frappe.db.exists("Payment Entry", pe_name):
            return "missing"

        pe_doc = _get_payment_entry(pe_name, "write")

        # Draft PE can be removed entirely because no GL is posted yet.
        if pe_doc.docstatus == 0:
            try:
                # Unlink first to satisfy linked-doc delete checks.
                frappe.db.set_value("PDC", pdc_doc.name, link_field, None, update_modified=False)
                _delete_with_pdc_bypass("Payment Entry", pe_name)
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

    for fieldname, value in pdc_updates.items():
        pdc_doc.set(fieldname, value)
    _append_pdc_activity(
        pdc_doc,
        target_status,
        old_status=old_status,
        new_status=target_status,
        details=reason,
        reference_doctype="Payment Entry",
        reference_name=payment_entry_name,
    )
    _save_with_pdc_bypass(pdc_doc)
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
        fields=["name", "payment_entry", "customer", "amount", "reference_date", "cheque_no", "pdc_status"],
    )

    for pdc in pdcs:
        pdc_doc = frappe.get_doc("PDC", pdc.name)
        old_status = pdc_doc.pdc_status
        pdc_doc.pdc_status = "Ready for Clearance"
        _append_pdc_activity(
            pdc_doc,
            "Marked Matured",
            old_status=old_status,
            new_status="Ready for Clearance",
            details=_("Matured on {0}").format(today_date),
        )
        _save_with_pdc_bypass(pdc_doc)
        if pdc.payment_entry:
            frappe.db.set_value("Payment Entry", pdc.payment_entry, "pdc_status", "Ready for Clearance")
        if _settings_flag("notify_accounts_managers", default=1):
            notify_accounts_manager(pdc)
        if _settings_flag("notify_customers", default=1):
            notify_customer(pdc)


@frappe.whitelist()
def approve_pdc_clearance(pdc_name=None, payment_entry_name=None):
    _require_pdc_manager(_("approve PDC clearance"))

    if pdc_name:
        rows = frappe.db.sql(
            "select name from `tabPDC` where name = %s and docstatus = 1 for update",
            (pdc_name,),
            as_dict=True,
        )
        if not rows:
            frappe.throw(_("Submitted PDC {0} was not found.").format(pdc_name))
        pdc_doc = frappe.get_doc("PDC", rows[0].name)
        _require_doc_permission(pdc_doc, "write")
    elif payment_entry_name:
        pdc_doc = _get_pdc_by_payment_entry(payment_entry_name, lock=True)
        _require_doc_permission(pdc_doc, "write")
    else:
        frappe.throw(_("PDC or Payment Entry is required."))

    if pdc_doc.pdc_status in PDC_FINAL_STATUSES:
        frappe.throw(_("Finalized PDCs cannot be approved for clearance."))
    if pdc_doc.pdc_status not in {"Ready for Clearance", "Pending"}:
        frappe.throw(_("Only Pending or Ready for Clearance PDCs can be approved."))

    old_status = pdc_doc.pdc_status
    pdc_doc.pdc_status = "Approved for Clearance"
    _append_pdc_activity(
        pdc_doc,
        "Approved for Clearance",
        old_status=old_status,
        new_status="Approved for Clearance",
    )
    _save_with_pdc_bypass(pdc_doc)
    if pdc_doc.payment_entry:
        frappe.db.set_value("Payment Entry", pdc_doc.payment_entry, "pdc_status", "Approved for Clearance")
    return pdc_doc.name


def get_accounts_managers():
    users = set()
    for role in get_pdc_manager_roles():
        users.update(frappe.get_all("Has Role", filters={"role": role}, fields=["parent"], pluck="parent"))
    return sorted(users)


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
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Only System Manager can install or update PDC Client Scripts."), frappe.PermissionError)

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
        doc.save()
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
        doc.insert()

    return doc.name
