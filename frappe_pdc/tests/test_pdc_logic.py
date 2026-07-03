import frappe
from frappe.tests import IntegrationTestCase

from frappe_pdc.pdc import validate_pdc_status_transition


class TestPDCLogic(IntegrationTestCase):
	def test_valid_status_transition(self):
		validate_pdc_status_transition("Pending", "Ready for Clearance")
		validate_pdc_status_transition("Approved for Clearance", "Cleared")

	def test_invalid_status_transition(self):
		with self.assertRaises(frappe.ValidationError):
			validate_pdc_status_transition("Cleared", "Pending")
