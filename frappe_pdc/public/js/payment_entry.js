frappe.ui.form.on('Payment Entry', {
    refresh: function (frm) {
        frm.set_df_property('pdc_status', 'read_only', 1);

        // --- PDC REGISTRATION WORKFLOW ---
        if (frm.doc.is_pdc && frm.doc.docstatus === 0) {
            // Hide custom Register PDC if it was accidentally added as custom button
            frm.remove_custom_button(__('Register PDC'));

            if (!frm.is_new()) {
                // Definitively replace SUBMIT with REGISTER PDC as primary action
                frm.page.set_primary_action(__('Register PDC'), function () {
                    if (frm.is_dirty()) {
                        frappe.msgprint(__('Please save the Payment Entry before registering PDC.'));
                        return;
                    }
                    frappe.call({
                        method: 'frappe_pdc.pdc.register_pdc',
                        args: { payment_entry_name: frm.doc.name },
                        callback: function (r) {
                            if (r.message) {
                                frappe.show_alert({ message: __('PDC Registered Successfully'), indicator: 'green' });
                                frm.reload_doc();
                                frappe.set_route('Form', 'PDC', r.message);
                            }
                        }
                    });
                });
            } else {
                // If new, ensure primary action is 'Save' (standard Frappe behavior)
                // but we hide Submit precisely when it would normally show up.
            }
        }

        if (frm.doc.is_pdc) {
            const status_colors = {
                "Pending": "orange",
                "Ready for Clearance": "yellow",
                "Handed Over": "blue",
                "Partially Cleared": "purple",
                "Partially Bounced": "orange",
                "Cleared": "green",
                "Bounced": "red"
            };

            if (frm.doc.pdc_status) {
                frm.set_df_property('pdc_status', 'description',
                    `<b style="color:${status_colors[frm.doc.pdc_status] || 'black'}">${frm.doc.pdc_status}</b>`);
                frm.set_intro(
                    __('PDC Status: {0}', [`<b style="color:${status_colors[frm.doc.pdc_status] || 'black'}">${__(frm.doc.pdc_status)}</b>`]),
                    status_colors[frm.doc.pdc_status] || 'blue'
                );
            }

            // Show Link if PDC exists
            frappe.call({
                method: 'frappe_pdc.pdc.get_pdc_name',
                args: { payment_entry: frm.doc.name },
                callback: function (r) {
                    if (r.message && !frm.custom_buttons[__('View PDC Record')]) {
                        frm.add_custom_button(__('View PDC Record'), function () {
                            frappe.set_route('Form', 'PDC', r.message);
                        }, __('Links'));

                        if (!['Cleared', 'Bounced', 'Partially Bounced'].includes(frm.doc.pdc_status)) {
                            frm.add_custom_button(__('Clear PDC'), function () {
                                frappe.db.get_value('PDC', r.message, ['amount', 'cleared_amount'], (pdc) => {
                                    let rem = flt(pdc.amount) - flt(pdc.cleared_amount);
                                    frappe.prompt([
                                        { label: __('Amount to Clear'), fieldname: 'amt', fieldtype: 'Currency', default: rem, reqd: 1 }
                                    ], (values) => {
                                        frappe.call({
                                            method: 'frappe_pdc.pdc.clear_pdc',
                                            args: { payment_entry_name: frm.doc.name, clear_amount: values.amt },
                                            callback: function (r) {
                                                if (!r.exc) {
                                                    frappe.show_alert({ message: __('PDC Cleared'), indicator: 'green' });
                                                    frm.reload_doc();
                                                }
                                            }
                                        });
                                    }, __('Clearing Amount'), __('Submit'));
                                });
                            }, __('Actions'));

                            frm.add_custom_button(__('Mark as Bounced'), function () {
                                frappe.prompt([
                                    { label: __('Bounced Reason'), fieldname: 'reason', fieldtype: 'Small Text', reqd: 1 }
                                ], (values) => {
                                    frappe.call({
                                        method: 'frappe_pdc.pdc.mark_pdc_bounced',
                                        args: { payment_entry_name: frm.doc.name, reason: values.reason },
                                        callback: function (r) {
                                            if (!r.exc) {
                                                const status = r.message?.customer_payment_entry || '';
                                                const msg = status ? __('PDC updated ({0})', [status]) : __('PDC status updated');
                                                frappe.show_alert({ message: msg, indicator: 'red' });
                                                frm.reload_doc();
                                            }
                                        }
                                    });
                                }, __('Reason for Bouncing'), __('Submit'));
                            }, __('Actions'));
                        }
                    }
                }
            });
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
