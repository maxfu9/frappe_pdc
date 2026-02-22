import frappe

def cleanup():
    # Fields to remove
    to_delete = ["cheque_no", "cheque_date", "references"] 
    
    for fieldname in to_delete:
        custom_field_name = f"Payment Entry-{fieldname}"
        if frappe.db.exists("Custom Field", custom_field_name):
            print(f"Deleting {custom_field_name}")
            frappe.delete_doc("Custom Field", custom_field_name)
    
    frappe.db.commit()

if __name__ == "__main__":
    cleanup()
