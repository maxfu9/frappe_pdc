import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.setup.setup_wizard.operations.install_fixtures import install as install_erpnext_fixtures
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from frappe_pdc.pdc import (
    approve_pdc_clearance,
    clear_pdc,
    create_replacement_pdc,
    get_pdc_actions,
    handover_pdc,
    mark_pdc_bounced,
    register_pdc,
)


class TestPDCWorkflow(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not frappe.db.exists("Warehouse Type", "Transit"):
            install_erpnext_fixtures("United States")
        cls.draft_date = getdate(add_days(today(), -1))
        cls.clearance_date = getdate(today())
        for year in {cls.draft_date.year, cls.clearance_date.year}:
            if not frappe.db.exists(
                "Fiscal Year",
                {"year_start_date": ["<=", f"{year}-01-01"], "year_end_date": [">=", f"{year}-12-31"]},
            ):
                frappe.get_doc({
                    "doctype": "Fiscal Year",
                    "year": f"PDC Test {year}",
                    "year_start_date": f"{year}-01-01",
                    "year_end_date": f"{year}-12-31",
                }).insert()

        suffix = frappe.generate_hash(length=4).upper()
        cls.company = frappe.get_doc({
            "doctype": "Company",
            "company_name": f"PDC Workflow Test {suffix}",
            "abbr": f"P{suffix}",
            "country": "United States",
            "default_currency": "USD",
            "create_chart_of_accounts_based_on": "Standard Template",
            "chart_of_accounts": "Standard",
        }).insert().reload()
        cls.bank_account = cls.company.default_bank_account
        if not cls.bank_account:
            bank_parent = frappe.db.get_value(
                "Account", {"company": cls.company.name, "account_type": "Bank", "is_group": 1}, "name"
            )
            cls.bank_account = cls.make_bank_account("PDC QA Bank", bank_parent)
        bank_parent = frappe.db.get_value("Account", cls.bank_account, "parent_account")
        cls.holding_account = cls.make_bank_account("PDC QA Holding", bank_parent)
        cls.customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": f"PDC QA Customer {suffix}",
            "customer_group": "Commercial",
            "territory": "United States",
        }).insert().name
        cls.supplier = frappe.get_doc({
            "doctype": "Supplier",
            "supplier_name": f"PDC QA Supplier {suffix}",
            "supplier_group": "Services",
        }).insert().name
        cls.item = frappe.get_doc({
            "doctype": "Item",
            "item_code": f"PDC QA Service {suffix}",
            "item_group": "Services",
            "stock_uom": "Nos",
            "is_stock_item": 0,
        }).insert().name
        cls.price_list = frappe.get_doc({
            "doctype": "Price List",
            "price_list_name": f"PDC QA Prices {suffix}",
            "currency": "USD",
            "enabled": 1,
            "selling": 1,
            "buying": 1,
        }).insert().name

    @classmethod
    def make_bank_account(cls, name, parent):
        return frappe.get_doc({
            "doctype": "Account",
            "account_name": name,
            "company": cls.company.name,
            "parent_account": parent,
            "account_type": "Bank",
            "account_currency": "USD",
        }).insert().name

    def setUp(self):
        super().setUp()
        frappe.db.set_single_value("PDC Settings", {
            "pdc_receivable_account": self.holding_account,
            "require_clearance_approval": 1,
            "allow_early_clearance": 0,
        })

    def make_invoice(self, doctype, amount):
        if doctype == "Sales Invoice":
            party = {"customer": self.customer, "selling_price_list": self.price_list}
        else:
            party = {"supplier": self.supplier, "buying_price_list": self.price_list}
        invoice = frappe.get_doc({
            "doctype": doctype,
            "company": self.company.name,
            "posting_date": self.draft_date,
            "set_posting_time": 1,
            "due_date": today(),
            "currency": "USD",
            "conversion_rate": 1,
            "price_list_currency": "USD",
            "plc_conversion_rate": 1,
            **party,
            "items": [{
                "item_code": self.item,
                "qty": 1,
                "rate": amount,
                "expense_account": self.company.default_expense_account,
                "cost_center": self.company.cost_center,
            }],
        })
        invoice.set_missing_values()
        invoice.insert()
        return invoice.submit()

    def make_receipt(self, *invoices, is_pdc=True):
        first, *remaining = invoices
        entry = get_payment_entry("Sales Invoice", first.name, bank_account=self.bank_account)
        entry.posting_date = self.draft_date
        entry.reference_date = self.draft_date
        entry.reference_no = frappe.generate_hash(length=12)
        entry.is_pdc = int(is_pdc)
        for invoice in remaining:
            self.add_reference(entry, invoice)
        entry.paid_amount = entry.received_amount = sum(invoice.grand_total for invoice in invoices)
        return entry.insert()

    def add_reference(self, entry, invoice):
        entry.append("references", {
            "reference_doctype": invoice.doctype,
            "reference_name": invoice.name,
            "total_amount": invoice.grand_total,
            "outstanding_amount": invoice.outstanding_amount,
            "allocated_amount": invoice.outstanding_amount,
        })

    def assert_posted_vouchers(self, pdc, count):
        pdc.reload()
        self.assertEqual(len(pdc.clearance_vouchers), count)
        for voucher in pdc.clearance_vouchers:
            entry = frappe.get_doc(voucher.voucher_type, voucher.voucher_no)
            self.assertEqual(entry.docstatus, 1)
            self.assertEqual(getdate(entry.posting_date), self.clearance_date)
        ledger = frappe.get_all(
            "GL Entry",
            filters={"voucher_no": ["in", [row.voucher_no for row in pdc.clearance_vouchers]], "is_cancelled": 0},
            fields=["account", "debit", "credit", "posting_date"],
        )
        self.assertTrue(ledger)
        self.assertTrue(all(getdate(row.posting_date) == self.clearance_date for row in ledger))
        return ledger

    def account_balance(self, ledger, account):
        return sum(row.debit - row.credit for row in ledger if row.account == account)

    def test_partial_clearance_preserves_posted_allocations_when_draft_is_edited(self):
        first = self.make_invoice("Sales Invoice", 40)
        second = self.make_invoice("Sales Invoice", 60)
        replacement = self.make_invoice("Sales Invoice", 60)
        entry = self.make_receipt(first, second)

        with self.assertRaisesRegex(frappe.ValidationError, "Register this cheque"):
            entry.submit()
        pdc = frappe.get_doc("PDC", register_pdc(entry.name))
        self.assertEqual(register_pdc(entry.name), pdc.name)
        with self.assertRaisesRegex(frappe.ValidationError, "Open the PDC record"):
            entry.reload().submit()
        self.assertFalse(get_pdc_actions(pdc.name)["clear"])
        with self.assertRaisesRegex(frappe.ValidationError, "approved"):
            clear_pdc(entry.name, 40)

        pdc.clearance_bank_account = self.bank_account
        pdc.save()
        approve_pdc_clearance(pdc_name=pdc.name)
        clear_pdc(entry.name, 40, posting_date=self.clearance_date)
        self.assertEqual(first.reload().outstanding_amount, 0)
        self.assertEqual(second.reload().outstanding_amount, 60)
        self.assertEqual(entry.reload().paid_amount, 60)

        entry.set("references", [])
        self.add_reference(entry, replacement)
        entry.save()

        pdc.reload()
        self.assertEqual((pdc.amount, pdc.cleared_amount, pdc.pdc_status), (100, 40, "Partially Cleared"))
        self.assertEqual(
            {row.reference_name: (row.allocated_amount, row.cleared_amount) for row in pdc.references_table},
            {first.name: (40, 40), replacement.name: (60, 0)},
        )
        actions = get_pdc_actions(pdc.name)
        self.assertTrue(actions["clear"])
        self.assertEqual(actions["remaining"], 60)
        with self.assertRaisesRegex(frappe.ValidationError, "exceeds remaining"):
            clear_pdc(entry.name, 60.01)
        clear_pdc(entry.name)

        ledger = self.assert_posted_vouchers(pdc, 4)
        self.assertEqual((pdc.cleared_amount, pdc.pdc_status), (100, "Cleared"))
        self.assertEqual(replacement.reload().outstanding_amount, 0)
        self.assertEqual(second.reload().outstanding_amount, 60)
        self.assertEqual(self.account_balance(ledger, self.holding_account), 0)
        self.assertEqual(self.account_balance(ledger, self.bank_account), 100)

    def test_handover_keeps_approval_and_posts_both_sides(self):
        sale = self.make_invoice("Sales Invoice", 100)
        purchase = self.make_invoice("Purchase Invoice", 100)
        entry = self.make_receipt(sale)
        pdc = frappe.get_doc("PDC", register_pdc(entry.name))
        supplier_entry = handover_pdc(entry.name, self.supplier, self.draft_date)
        self.assertFalse(get_pdc_actions(pdc.name)["clear"])
        approve_pdc_clearance(pdc_name=pdc.name)
        self.assertEqual(pdc.reload().pdc_status, "Handed Over")
        self.assertTrue(get_pdc_actions(pdc.name)["clear"])
        with self.assertRaisesRegex(frappe.ValidationError, "already been handed over"):
            handover_pdc(entry.name, self.supplier, self.draft_date)
        with self.assertRaisesRegex(frappe.ValidationError, "Open the PDC record"):
            frappe.get_doc("Payment Entry", supplier_entry).submit()
        with self.assertRaisesRegex(frappe.ValidationError, "future"):
            clear_pdc(entry.name, posting_date=add_days(today(), 1))
        clear_pdc(entry.name, 99.99)
        self.assertEqual(pdc.reload().pdc_status, "Partially Cleared")
        self.assertAlmostEqual(sale.reload().outstanding_amount, 0.01)
        self.assertAlmostEqual(purchase.reload().outstanding_amount, 0.01)

        supplier_draft = frappe.get_doc("Payment Entry", supplier_entry)
        supplier_draft.custom_remarks = 1
        supplier_draft.remarks = "Supplier deposit confirmed"
        supplier_draft.save()
        self.assertEqual(pdc.reload().purchase_references[0].cleared_amount, 99.99)
        clear_pdc(entry.name)

        ledger = self.assert_posted_vouchers(pdc, 4)
        self.assertEqual(pdc.pdc_status, "Cleared")
        self.assertEqual(sale.reload().outstanding_amount, 0)
        self.assertEqual(purchase.reload().outstanding_amount, 0)
        self.assertAlmostEqual(self.account_balance(ledger, self.holding_account), 0)

    def test_ordinary_payment_entry_can_still_be_submitted(self):
        invoice = self.make_invoice("Sales Invoice", 20)
        entry = self.make_receipt(invoice, is_pdc=False).submit()
        self.assertEqual(entry.docstatus, 1)
        self.assertEqual(invoice.reload().outstanding_amount, 0)

    def test_future_cheque_actions_explain_maturity(self):
        invoice = self.make_invoice("Sales Invoice", 20)
        entry = self.make_receipt(invoice)
        entry.reference_date = add_days(today(), 1)
        entry.save()
        pdc_name = register_pdc(entry.name)
        approve_pdc_clearance(pdc_name=pdc_name)

        actions = get_pdc_actions(pdc_name)
        self.assertFalse(actions["clear"])
        self.assertIn("before its reference date", actions["clearance_block"])

    def test_replacement_clears_only_the_bounced_balance(self):
        invoice = self.make_invoice("Sales Invoice", 100)
        entry = self.make_receipt(invoice)
        original = frappe.get_doc("PDC", register_pdc(entry.name))
        original.clearance_bank_account = self.bank_account
        original.save()
        approve_pdc_clearance(pdc_name=original.name)
        clear_pdc(entry.name, 40)
        mark_pdc_bounced(entry.name, "Remaining amount was returned by the bank")

        original.reload()
        self.assertEqual((original.pdc_status, original.cleared_amount), ("Partially Bounced", 40))
        self.assertEqual(invoice.reload().outstanding_amount, 60)
        result = create_replacement_pdc(
            original.name, reference_no=frappe.generate_hash(length=12), reference_date=self.clearance_date
        )
        replacement = frappe.get_doc("PDC", result["pdc"])
        self.assertEqual(original.reload().replacement_pdc, replacement.name)
        self.assertEqual((replacement.amount, replacement.replaces_pdc), (60, original.name))
        self.assertFalse(get_pdc_actions(replacement.name)["clear"])
        replacement.clearance_bank_account = self.bank_account
        replacement.save()
        approve_pdc_clearance(pdc_name=replacement.name)
        clear_pdc(result["payment_entry"])
        self.assertEqual(invoice.reload().outstanding_amount, 0)

    def test_cancelling_a_partially_cleared_pdc_reverses_its_vouchers(self):
        invoice = self.make_invoice("Sales Invoice", 100)
        entry = self.make_receipt(invoice)
        pdc = frappe.get_doc("PDC", register_pdc(entry.name))
        pdc.clearance_bank_account = self.bank_account
        pdc.save()
        approve_pdc_clearance(pdc_name=pdc.name)
        clear_pdc(entry.name, 40)
        pdc.reload()
        self.assertEqual(invoice.reload().outstanding_amount, 60)
        pdc.cancel()

        self.assertEqual(pdc.docstatus, 2)
        for voucher in pdc.clearance_vouchers:
            self.assertEqual(frappe.db.get_value(voucher.voucher_type, voucher.voucher_no, "docstatus"), 2)
        self.assertEqual(invoice.reload().outstanding_amount, 100)
