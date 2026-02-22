frappe.ui.form.on('PDC', {
    refresh: function (frm) {
        const status_colors = {
            'Pending': 'orange',
            'Ready for Clearance': 'yellow',
            'Approved for Clearance': 'blue',
            'Handed Over': 'blue',
            'Partially Cleared': 'purple',
            'Partially Bounced': 'orange',
            'Cleared': 'green',
            'Bounced': 'red'
        };
        frm.set_df_property('pdc_status', 'read_only', 1);
        if (frm.doc.pdc_status) {
            frm.set_df_property(
                'pdc_status',
                'description',
                `<b style="color:${status_colors[frm.doc.pdc_status] || 'black'}">${frm.doc.pdc_status}</b>`
            );
            frm.set_intro(
                __('PDC Status: {0}', [`<b style="color:${status_colors[frm.doc.pdc_status] || 'black'}">${__(frm.doc.pdc_status)}</b>`]),
                status_colors[frm.doc.pdc_status] || 'blue'
            );
        }

        if (frm.doc.docstatus === 1) {
            if (!['Cleared', 'Bounced', 'Partially Bounced'].includes(frm.doc.pdc_status)) {

                // Action Buttons
                frm.add_custom_button(__('Clear Cheque'), function () {
                    let total_amt = flt(frm.doc.amount);
                    let cleared_amt = flt(frm.doc.cleared_amount);
                    let max_amt = Math.round((total_amt - cleared_amt) * 100) / 100;

                    if (max_amt <= 0) {
                        frappe.msgprint(__('This cheque is already fully cleared according to the amounts.'));
                        return;
                    }

                    frappe.prompt([
                        { label: __('Amount to Clear'), fieldname: 'amt', fieldtype: 'Currency', default: max_amt, reqd: 1 }
                    ], (values) => {
                        let entered_amt = flt(values.amt);
                        if (entered_amt <= 0) {
                            frappe.msgprint(__('Amount must be greater than zero'));
                            return;
                        }
                        if (entered_amt > max_amt + 0.01) {
                            frappe.msgprint(__('Amount cannot exceed remaining balance of {0}', [max_amt]));
                            return;
                        }
                        frappe.call({
                            method: 'frappe_pdc.pdc.clear_pdc',
                            args: {
                                payment_entry_name: frm.doc.payment_entry,
                                clear_amount: entered_amt
                            },
                            callback: function (r) {
                                if (!r.exc) {
                                    frappe.show_alert({ message: __('Clearance Processed'), indicator: 'green' });
                                    frm.reload_doc();
                                }
                            }
                        });
                    }, __('Clearing Amount'), __('Submit'));
                }, __('Actions'));

                frm.add_custom_button(__('Handover to Supplier'), function () {
                    frappe.prompt([
                        { label: __('Supplier'), fieldname: 'supplier', fieldtype: 'Link', options: 'Supplier', reqd: 1 },
                        { label: __('Handover Date'), fieldname: 'date', fieldtype: 'Date', default: frappe.datetime.get_today(), reqd: 1 }
                    ], (values) => {
                        frappe.call({
                            method: 'frappe_pdc.pdc.handover_pdc',
                            args: {
                                payment_entry_name: frm.doc.payment_entry,
                                supplier: values.supplier,
                                handover_date: values.date
                            },
                            callback: function (r) {
                                if (!r.exc) {
                                    frappe.show_alert({ message: __('Handed Over to Supplier'), indicator: 'blue' });
                                    frm.reload_doc();
                                }
                            }
                        });
                    }, __('Handover Details'), __('Submit'));
                }, __('Actions'));

                frm.add_custom_button(__('Mark as Bounced'), function () {
                    frappe.prompt([
                        { label: __('Bounced Reason'), fieldname: 'reason', fieldtype: 'Small Text', reqd: 1 }
                    ], (values) => {
                        frappe.call({
                            method: 'frappe_pdc.pdc.mark_pdc_bounced',
                            args: {
                                payment_entry_name: frm.doc.payment_entry,
                                reason: values.reason
                            },
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

        // Dashboard Summary
        if (frm.doc.docstatus === 1) {
            let total = flt(frm.doc.amount);
            let cleared = flt(frm.doc.cleared_amount);
            let percent = total > 0 ? (cleared / total) * 100 : 0;
            let color = percent >= 100 ? 'green' : (percent > 0 ? 'orange' : 'grey');

            let html = `
                <div style="padding: 20px; background: white; border-radius: 12px; border: 1px solid #d1d5db; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); margin-bottom: 25px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                        <span style="font-size: 1.1em; font-weight: 700; color: #1e293b;">${__('Clearance Progress')}</span>
                        <span style="font-size: 1em; font-weight: 800; color: ${color};">${percent.toFixed(1)}%</span>
                    </div>
                    
                    <div style="width: 100%; background: #f1f5f9; height: 12px; border-radius: 6px; overflow: hidden; margin-bottom: 20px; border: 1px solid #e2e8f0;">
                        <div style="width: ${percent}%; height: 100%; background: ${color}; transition: width 0.8s ease-out; box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);"></div>
                    </div>
                    
                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; border-top: 1px solid #f1f5f9; padding-top: 15px; margin-top: 5px;">
                        <div style="padding: 10px; text-align: center;">
                            <div style="font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; margin-bottom: 4px; font-weight: 600;">${__('Total Amount')}</div>
                            <div style="font-size: 1.25rem; font-weight: 700; color: #0f172a;">${format_currency(total, frm.doc.currency)}</div>
                        </div>
                        <div style="padding: 10px; text-align: center; border-left: 1px solid #f1f5f9; border-right: 1px solid #f1f5f9;">
                            <div style="font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; margin-bottom: 4px; font-weight: 600;">${__('Cleared')}</div>
                            <div style="font-size: 1.25rem; font-weight: 700; color: #10b981;">${format_currency(cleared, frm.doc.currency)}</div>
                        </div>
                        <div style="padding: 10px; text-align: center;">
                            <div style="font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; margin-bottom: 4px; font-weight: 600;">${__('Remaining')}</div>
                            <div style="font-size: 1.25rem; font-weight: 700; color: ${total - cleared > 0 ? '#ef4444' : '#10b981'};">${format_currency(total - cleared, frm.doc.currency)}</div>
                        </div>
                    </div>
                </div>
            `;
            frm.dashboard.clear_sections();
            frm.dashboard.add_section(html);
        }

        if (frm.doc.payment_entry) {
            frm.add_custom_button(__('View Payment Entry'), () => {
                frappe.set_route('Form', 'Payment Entry', frm.doc.payment_entry);
            }, __('Links'));
        }
    },
    setup: function (frm) {
        // Set query for bank accounts if needed
    }
});
