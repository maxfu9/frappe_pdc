frappe.query_reports["PDC Clearance Queue"] = {
    filters: [
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
        },
        {
            fieldname: "due_through",
            label: __("Due Through"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
            reqd: 1,
        },
        {
            fieldname: "timing",
            label: __("Timing"),
            fieldtype: "Select",
            options: ["", "Overdue", "Due Today", "Upcoming"],
        },
        {
            fieldname: "work_status",
            label: __("Clearance Status"),
            fieldtype: "Select",
            options: ["", "Ready to Clear", "Awaiting Approval", "Blocked", "Not Yet Due"],
        },
    ],
};
