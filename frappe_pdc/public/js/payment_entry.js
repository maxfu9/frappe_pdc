frappe.ui.form.on('Payment Entry', {
    refresh: async function (frm) {
        align_accounting_links(frm);
        frm.set_df_property('pdc_status', 'read_only', 1);
        frm.toggle_display('pdc_status', Boolean(frm.doc.is_pdc));
        if (!frm.doc.is_pdc && !frm.doc.pdc_status) {
            frm.set_intro('');
            frm.set_df_property('pdc_status', 'description', '');
        }

        if (frm.is_new() || (!frm.doc.is_pdc && !frm.doc.pdc_status)) {
            return;
        }

        const docname = frm.doc.name;
        const pdc = await frappe.xcall('frappe_pdc.pdc.get_pdc_name', { payment_entry: docname });
        if (frm.doc.name !== docname) {
            return;
        }

        if (frm.doc.docstatus === 0 && !frm.is_dirty()) {
            if (pdc) {
                frm.page.set_primary_action(__('View PDC Record'), () => frappe.set_route('Form', 'PDC', pdc));
            } else if (frm.doc.is_pdc) {
                set_pdc_registration_action(frm);
            }
        }

        if (!pdc) {
            return;
        }

        frm.add_custom_button(__('View PDC Record'), () => frappe.set_route('Form', 'PDC', pdc), __('Links'));
        if (!frm.doc.is_pdc || frm.doc.docstatus !== 0) {
            return;
        }

        const actions = await frappe.xcall('frappe_pdc.pdc.get_pdc_actions', { pdc_name: pdc });
        if (frm.doc.name !== docname) {
            return;
        }
        if (actions.clearance_block) {
            frm.set_intro(frappe.utils.escape_html(actions.clearance_block), 'orange');
        }
        if (actions.clear) {
            frm.add_custom_button(__('Clear PDC'), () => {
                frappe.prompt([
                    { label: __('Amount to Clear'), fieldname: 'amt', fieldtype: 'Currency', precision: actions.precision, default: actions.remaining, reqd: 1 },
                    { label: __('Mode of Payment'), fieldname: 'mode_of_payment', fieldtype: 'Link', options: 'Mode of Payment', reqd: 1 },
                    { label: __('Clearance Posting Date'), fieldname: 'posting_date', fieldtype: 'Date', default: frappe.datetime.get_today(), reqd: 1 }
                ], async (values) => {
                    await frappe.xcall('frappe_pdc.pdc.clear_pdc', {
                        payment_entry_name: docname,
                        clear_amount: values.amt,
                        mode_of_payment: values.mode_of_payment,
                        posting_date: values.posting_date
                    }, 'POST', { freeze: true });
                    frappe.show_alert({ message: __('Clearance Processed'), indicator: 'green' });
                    frm.reload_doc();
                }, __('Clear Cheque'), __('Submit'));
            }, __('Actions'));
        }
        if (actions.bounce) {
            frm.add_custom_button(__('Mark as Bounced'), () => {
                frappe.prompt([
                    { label: __('Bounced Reason'), fieldname: 'reason', fieldtype: 'Small Text', reqd: 1 }
                ], async (values) => {
                    await frappe.xcall('frappe_pdc.pdc.mark_pdc_bounced', {
                        payment_entry_name: docname, reason: values.reason
                    }, 'POST', { freeze: true });
                    frappe.show_alert({ message: __('PDC status updated'), indicator: 'red' });
                    frappe.set_route('Form', 'PDC', pdc);
                }, __('Reason for Bouncing'), __('Submit'));
            }, __('Actions'));
        }
    },
    validate: function (frm) {
        if (frm.doc.is_pdc) {
            if (!frm.doc.reference_date) {
                frappe.msgprint(__('Please enter the Cheque/Reference Date for the PDC.'));
                frappe.validated = false;
            }
            if (!frm.doc.reference_no) {
                frappe.msgprint(__('Please enter the Cheque/Reference No for the PDC.'));
                frappe.validated = false;
            }

            // Force check unallocated amount strictly
            let total_allocated = 0;
            (frm.doc.references || []).forEach(ref => {
                total_allocated += flt(ref.allocated_amount);
            });

            if (total_allocated > 0 && Math.abs(flt(frm.doc.paid_amount) - total_allocated) > 0.01) {
                frappe.msgprint(__('For PDCs, the total Paid Amount ({0}) must match the sum of allocated invoices ({1}). Syncing now...', [frm.doc.paid_amount, total_allocated]));
                frm.set_value('paid_amount', total_allocated);
                frm.set_value('received_amount', total_allocated);
                frappe.validated = false;
            }
        }
    },
    is_pdc: function (frm) {
        frm.toggle_reqd('reference_date', frm.doc.is_pdc);
        frm.toggle_reqd('reference_no', frm.doc.is_pdc);
        frm.toggle_display('pdc_status', !!frm.doc.is_pdc);
        if (!frm.doc.is_pdc) {
            frm.set_intro('');
            frm.set_df_property('pdc_status', 'description', '');
        }
        if (frm.doc.is_pdc) {
            sync_pdc_amount(frm);
        }
        frm.refresh();
    },
    paid_amount: function (frm) {
        if (frm.doc.is_pdc && frm.doc.paid_amount !== frm.doc.received_amount) {
            frm.set_value('received_amount', frm.doc.paid_amount);
        }
    },
    received_amount: function (frm) {
        if (frm.doc.is_pdc && frm.doc.received_amount !== frm.doc.paid_amount) {
            frm.set_value('paid_amount', frm.doc.received_amount);
        }
    }
});

function align_accounting_links(frm) {
    const area = frm.dashboard?.transactions_area?.[0];
    const journal = area?.querySelector('.document-link[data-doctype="Journal Entry"]');
    const bank = area?.querySelector('.document-link[data-doctype="Bank Transaction"]');
    if (!journal || !bank) return;

    const accounting = journal.parentElement;
    const bankGroup = bank.parentElement;
    journal.after(bank);
    if (bankGroup !== accounting && !bankGroup.querySelector('.document-link')) {
        bankGroup.remove();
    }

    accounting.classList.replace('col-md-4', 'col-md-8');
    Object.assign(accounting.style, {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 200px), 1fr))',
        columnGap: '16px',
        maxWidth: 'min(100%, 560px)',
    });
    const heading = accounting.querySelector(':scope > .form-link-title');
    if (heading) heading.style.gridColumn = '1 / -1';
}

function set_pdc_registration_action(frm) {
    frm.page.set_primary_action(__('Register PDC'), async () => {
        if (frm.is_dirty()) {
            frappe.msgprint(__('Please save the Payment Entry before registering PDC.'));
            return;
        }

        const pdc = await frappe.xcall('frappe_pdc.pdc.register_pdc', {
            payment_entry_name: frm.doc.name
        }, 'POST', { freeze: true });
        if (pdc) {
            frappe.show_alert({ message: __('PDC Registered Successfully'), indicator: 'green' });
            frappe.set_route('Form', 'PDC', pdc);
        }
    });
}

// Helper to sync PDC amounts
function sync_pdc_amount(frm) {
    if (frm.doc.is_pdc && frm.doc.docstatus === 0) {
        let total_allocated = 0;
        (frm.doc.references || []).forEach(ref => {
            total_allocated += flt(ref.allocated_amount);
        });

        if (total_allocated > 0 && Math.abs(flt(frm.doc.paid_amount) - total_allocated) > 0.001) {
            frm.set_value('paid_amount', total_allocated);
            frm.set_value('received_amount', total_allocated);
            // Re-trigger standard PE totals calculation if needed
            if (frm.doc.paid_amount !== total_allocated) {
                frm.refresh_field('paid_amount');
                frm.refresh_field('received_amount');
            }
        }
    }
}

// Watch for allocation changes
frappe.ui.form.on('Payment Entry Reference', {
    allocated_amount: function (frm, cdt, cdn) {
        if (frm.doc.is_pdc) {
            sync_pdc_amount(frm);
        }
    },
    references_remove: function (frm) {
        if (frm.doc.is_pdc) {
            sync_pdc_amount(frm);
        }
    }
});
