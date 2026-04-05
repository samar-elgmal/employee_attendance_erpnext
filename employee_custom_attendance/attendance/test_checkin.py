import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime


def make_employee(attendance_system="Standard"):
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
	emp = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": "Test",
			"last_name": "Flex",
			"company": company,
			"date_of_joining": "2025-01-01",
			"date_of_birth": "1990-01-01",
			"gender": "Male",
			"custom_attendance_system": attendance_system,
		}
	)
	emp.insert(ignore_permissions=True)
	return emp


class TestCheckinHook(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_flex_employee_gets_skip_auto_attendance(self):
		emp = make_employee("Flexible Hours")

		checkin = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": emp.name,
				"time": now_datetime(),
				"log_type": "IN",
				"shift": "Flexible Hours",  # simulate HRMS having set this
			}
		)
		# call our hook directly
		from employee_custom_attendance.attendance.checkin import on_checkin_validate

		on_checkin_validate(checkin, None)

		self.assertEqual(checkin.skip_auto_attendance, 1)

	def test_standard_employee_not_affected(self):
		emp = make_employee("Standard")

		checkin = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": emp.name,
				"time": now_datetime(),
				"log_type": "IN",
				"shift": "Morning Shift",
			}
		)
		from employee_custom_attendance.attendance.checkin import on_checkin_validate

		on_checkin_validate(checkin, None)

		self.assertNotEqual(checkin.skip_auto_attendance, 1)
