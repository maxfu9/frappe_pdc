frappe.query_reports["PDC Reconciliation"] = {
    filters: [
        { fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
        { fieldname: "pdc", label: __("PDC"), fieldtype: "Link", options: "PDC" },
        { fieldname: "from_date", label: __("Cheque Date From"), fieldtype: "Date" },
        { fieldname: "to_date", label: __("Cheque Date Through"), fieldtype: "Date" },
        { fieldname: "exceptions_only", label: __("Exceptions Only"), fieldtype: "Check", default: 1 },
    ],
};
