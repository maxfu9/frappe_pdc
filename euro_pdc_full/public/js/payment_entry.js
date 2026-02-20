frappe.ui.form.on("Payment Entry", {
    refresh(frm) {
        if (frm.doc.docstatus === 1 && frm.doc.is_pdc && frm.doc.pdc_status === "Approved for Clearance") {
            frm.add_custom_button("Clear PDC", function () {
                frappe.call({
                    method: "euro_pdc_full.pdc.clear_pdc",
                    args: { payment_entry_name: frm.doc.name },
                    callback: function() {
                        frm.reload_doc();
                        frappe.msgprint("✅ PDC Cleared Successfully");
                    }
                });
            }, "Actions");
        }
        if (frm.doc.docstatus === 1 && frm.doc.is_pdc &&
            frm.doc.pdc_status !== "Cleared" && frm.doc.pdc_status !== "Bounced") {
            frm.add_custom_button("Mark as Bounced", function () {
                frappe.prompt(
                    [{"fieldname":"reason","fieldtype":"Data","label":"Reason for Bounce","reqd":0}],
                    function(values){
                        frappe.call({
                            method: "euro_pdc_full.pdc.mark_pdc_bounced",
                            args: { payment_entry_name: frm.doc.name, reason: values.reason },
                            callback: function(){ frm.reload_doc(); frappe.msgprint("⚠️ PDC marked as Bounced"); }
                        });
                    },
                    "Confirm Bounce",
                    "Submit"
                );
            }, "Actions");
        }
    }
});
