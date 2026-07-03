import frappe
from frappe.tests import IntegrationTestCase

from frappe_pdc.pdc import _append_clearance_voucher, validate_pdc_status_transition


class _FakeMeta:
	def has_field(self, fieldname):
		return fieldname == "clearance_vouchers"


class _FakePDC:
	meta = _FakeMeta()

	def __init__(self):
		self.clearance_vouchers = []

	def get(self, fieldname):
		return getattr(self, fieldname)

	def append(self, fieldname, value):
		row = frappe._dict(value)
		getattr(self, fieldname).append(row)
		return row


class TestPDCLogic(IntegrationTestCase):
	def test_valid_status_transition(self):
		validate_pdc_status_transition("Pending", "Ready for Clearance")
		validate_pdc_status_transition("Approved for Clearance", "Cleared")

	def test_invalid_status_transition(self):
		with self.assertRaises(frappe.ValidationError):
			validate_pdc_status_transition("Cleared", "Pending")

	def test_append_clearance_voucher_ignores_duplicates(self):
		pdc = _FakePDC()
		_append_clearance_voucher(pdc, "Payment Entry", "PE-0001", 100, "Customer", "CUST-0001")
		_append_clearance_voucher(pdc, "Payment Entry", "PE-0001", 100, "Customer", "CUST-0001")

		self.assertEqual(len(pdc.clearance_vouchers), 1)
		self.assertEqual(pdc.clearance_vouchers[0].voucher_no, "PE-0001")
