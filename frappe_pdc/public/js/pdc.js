if (!frappe._frappe_pdc_form_script_registered) {
frappe._frappe_pdc_form_script_registered = true;

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

        toggle_calendar_button(frm);

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
                        { label: __('Amount to Clear'), fieldname: 'amt', fieldtype: 'Currency', default: max_amt, reqd: 1 },
                        { label: __('Mode of Payment'), fieldname: 'mode_of_payment', fieldtype: 'Link', options: 'Mode of Payment', reqd: 1 }
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
                                clear_amount: entered_amt,
                                mode_of_payment: values.mode_of_payment
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

        // Dashboard Summary (guarded so UI errors here don't block custom buttons)
        try {
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

                if (frm.dashboard && frm.dashboard.clear_sections && frm.dashboard.add_section) {
                    frm.dashboard.clear_sections();
                    frm.dashboard.add_section(html);
                }
            }
        } catch (e) {
            console.error('PDC dashboard render failed:', e);
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
}

function open_calendar_dialog(frm) {
    if (!frm.doc.reference_date) {
        frappe.msgprint(__('Reference Date is required to create an ERPNext Event.'));
        return;
    }

    const default_title = __('PDC Follow-up: {0}', [frm.doc.cheque_no || frm.doc.name]);
    const default_date = frm.doc.reference_date;
    const default_details = [
        __('PDC: {0}', [frm.doc.name]),
        __('Cheque/Reference No: {0}', [frm.doc.cheque_no || __('N/A')]),
        __('Customer: {0}', [frm.doc.customer || __('N/A')]),
        __('Amount: {0}', [format_currency(flt(frm.doc.amount), frm.doc.currency)]),
        __('Status: {0}', [frm.doc.pdc_status || __('N/A')]),
        frm.doc.payment_entry ? __('Payment Entry: {0}', [frm.doc.payment_entry]) : ''
    ].filter(Boolean).join('\n');

    const d = new frappe.ui.Dialog({
        title: __('Add to Calendar'),
        fields: [
            {
                fieldtype: 'Data',
                fieldname: 'event_title',
                label: __('Event Title'),
                reqd: 1,
                default: default_title
            },
            {
                fieldtype: 'Date',
                fieldname: 'event_date',
                label: __('Reference Date'),
                reqd: 1,
                default: default_date
            },
            {
                fieldtype: 'Check',
                fieldname: 'all_day',
                label: __('All Day'),
                default: 1
            },
            {
                fieldtype: 'Time',
                fieldname: 'start_time',
                label: __('Start Time'),
                default: '09:00:00',
                depends_on: 'eval:!doc.all_day'
            },
            {
                fieldtype: 'Time',
                fieldname: 'end_time',
                label: __('End Time'),
                default: '10:00:00',
                depends_on: 'eval:!doc.all_day'
            },
            {
                fieldtype: 'Small Text',
                fieldname: 'event_details',
                label: __('Event Notes'),
                default: default_details
            },
            {
                fieldtype: 'Check',
                fieldname: 'sync_with_google_calendar',
                label: __('Sync with Google Calendar'),
                default: 1
            },
            {
                fieldtype: 'Link',
                fieldname: 'google_calendar',
                label: __('Google Calendar'),
                options: 'Google Calendar',
                depends_on: 'eval:doc.sync_with_google_calendar'
            },
            {
                fieldtype: 'HTML',
                fieldname: 'calendar_preview'
            }
        ],
        primary_action_label: __('Create ERPNext Event'),
        primary_action() {
            const vals = d.get_values();
            if (!vals) return;
            create_erpnext_event(frm, vals);
            d.hide();
        }
    });

    d.show();

    const refresh_preview = () => {
        const vals = d.get_values() || {};
        if (!vals.event_title || !vals.event_date) {
            return;
        }

        const event_data = build_event_payload(vals);
        d.__event_data = event_data;
        d.fields_dict.calendar_preview.$wrapper.html(`
            <div style="line-height:1.8;">
                <div><b>${__('Reference Date')}:</b> ${frappe.datetime.str_to_user(vals.event_date)}</div>
                <div><b>${__('Mode')}:</b> ${event_data.all_day ? __('All Day') : __('Timed Event')}</div>
                <div><b>${__('Start')}:</b> ${event_data.starts_on}</div>
                <div><b>${__('End')}:</b> ${event_data.ends_on}</div>
                <div style="margin-top: 10px; color: #6b7280;">${__('Click "Create ERPNext Event" to open a prefilled Event form.')}</div>
            </div>
        `);
    };

    ['event_title', 'event_date', 'all_day', 'start_time', 'end_time', 'event_details'].forEach((fieldname) => {
        const field = d.fields_dict[fieldname];
        if (field && field.$input) {
            field.$input.on('change input', refresh_preview);
        }
    });

    refresh_preview();

}

function create_erpnext_event(frm, values) {
    const event_data = build_event_payload(values);
    const subject = with_docname_in_subject(frm.doc.name, values.event_title);

    if (values.sync_with_google_calendar && !values.google_calendar) {
        frappe.msgprint(__('Please select a Google Calendar to enable sync.'));
        return;
    }

    frappe.call({
        method: 'frappe.client.insert',
        args: {
            doc: {
                doctype: 'Event',
                subject,
                starts_on: event_data.starts_on,
                ends_on: event_data.ends_on,
                all_day: event_data.all_day ? 1 : 0,
                description: values.event_details || __('Created from PDC {0}', [frm.doc.name]),
                event_type: 'Private',
                reference_doctype: 'PDC',
                reference_docname: frm.doc.name,
                sync_with_google_calendar: values.sync_with_google_calendar ? 1 : 0,
                google_calendar: values.google_calendar || null
            }
        },
                callback: function (r) {
                    if (!r.exc && r.message && r.message.name) {
                        frappe.show_alert({
                            message: __('Event {0} created', [r.message.name]),
                            indicator: 'green'
                        });
                        frm.remove_custom_button(__('Add to Calendar'));
                        frappe.set_route('Form', 'Event', r.message.name);
                    }
                }
            });
}

function toggle_calendar_button(frm) {
    frm.remove_custom_button(__('Add to Calendar'));
    if (!frm.doc.name || frm.doc.__islocal) {
        return;
    }

    frappe.call({
        method: 'frappe.client.get_count',
        args: {
            doctype: 'Event',
            filters: {
                reference_doctype: 'PDC',
                reference_docname: frm.doc.name,
                docstatus: ['<', 2]
            }
        },
        callback: function (r) {
            const count = Number(r.message || 0);
            if (!count) {
                frm.add_custom_button(__('Add to Calendar'), () => {
                    open_calendar_dialog(frm);
                });
            }
        }
    });
}

function build_event_payload(values) {
    const date = values.event_date;
    const all_day = !!values.all_day;
    const start_time = normalize_time(values.start_time || '09:00:00');
    const end_time = normalize_time(values.end_time || '10:00:00');

    const starts_on = all_day
        ? `${date} 00:00:00`
        : `${date} ${start_time.slice(0, 2)}:${start_time.slice(2, 4)}:${start_time.slice(4, 6)}`;
    const ends_on = all_day
        ? `${frappe.datetime.add_days(date, 1)} 00:00:00`
        : `${date} ${end_time.slice(0, 2)}:${end_time.slice(2, 4)}:${end_time.slice(4, 6)}`;

    return {
        all_day,
        starts_on,
        ends_on
    };
}

function normalize_time(time_value) {
    const parts = (time_value || '09:00:00').split(':');
    const hh = (parts[0] || '09').padStart(2, '0');
    const mm = (parts[1] || '00').padStart(2, '0');
    const ss = (parts[2] || '00').padStart(2, '0');
    return `${hh}${mm}${ss}`;
}

function with_docname_in_subject(docname, subject) {
    const token = `[${docname}]`;
    const clean = (subject || '').trim();
    if (!clean) return token;
    if (clean.includes(token)) return clean;
    return `${token} ${clean}`;
}

// External calendar links/exports intentionally removed in favor of ERPNext Event creation.
