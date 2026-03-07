# frappe_pdc/hooks.py

# App Metadata
app_name = "frappe_pdc"
app_title = "Frappe PDC"
app_publisher = "EuroPlast"
app_description = "ERPNext PDC module"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = "hello@europlast.pk"
app_license = "MIT"

# DocType Event Hooks
doc_events = {
    "Payment Entry": {
        "on_update": "frappe_pdc.pdc.handle_payment_entry_update",
        "before_submit": "frappe_pdc.pdc.handle_before_submit",
        "on_submit": "frappe_pdc.pdc.handle_pdc_submission",
        "on_cancel": "frappe_pdc.pdc.handle_pdc_cancellation"
    },
    "PDC": {
        "on_cancel": "frappe_pdc.pdc.handle_pdc_cancelled"
    }
}

# Custom JS for Payment Entry Form
doctype_js = {
    "Payment Entry": "public/js/payment_entry.js",
    "PDC": "public/js/pdc.js",
}

# List view customizations
doctype_list_js = {
    "PDC": "public/js/pdc_list.js"
}

# Fixtures
fixtures = [
    {"dt": "Custom Field", "filters": [["module", "=", "Frappe PDC"]]},
    {"dt": "DocType", "filters": [["name", "=", "PDC"]]},
    {"dt": "DocType", "filters": [["name", "=", "PDC Reference"]]},
    {"dt": "Report", "filters": [["module", "=", "Frappe PDC"]]},
    {"dt": "Dashboard", "filters": [["module", "=", "Frappe PDC"]]},
    {"dt": "Dashboard Chart", "filters": [["module", "=", "Frappe PDC"]]}
]

# Scheduler Events
scheduler_events = {
    "daily": [
        "frappe_pdc.pdc.mark_matured_pdc"
    ]
}
