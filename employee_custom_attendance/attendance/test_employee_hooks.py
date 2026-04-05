import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today


def make_employee(attendance_system="Standard"):
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
	emp = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": "Hook",
			"last_name": "Test",
			"company": company,
			"date_of_joining": "2025-01-01",
			"date_of_birth": "1990-01-01",
			"gender": "Male",
			"custom_attendance_system": attendance_system,
		}
	)
	emp.insert(ignore_permissions=True)
	return emp


def get_flex_assignment(employee):
	return frappe.db.get_value(
		"Shift Assignment",
		{
			"employee": employee,
			"shift_type": "Flexible Hours",
			"docstatus": 1,
		},
		["name", "end_date"],
		as_dict=True,
	)


class TestEmployeeHook(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_flex_assignment_created_when_set_to_flexible(self):
		emp = make_employee("Standard")
		emp.custom_attendance_system = "Flexible Hours"
		emp.save()

		assignment = get_flex_assignment(emp.name)
		self.assertIsNotNone(assignment)
		self.assertIsNone(assignment.end_date)

	def test_flex_assignment_ended_when_reverted_to_standard(self):
		emp = make_employee("Flexible Hours")
		# trigger hook to create assignment
		from employee_custom_attendance.attendance.employee_hooks import on_employee_update

		on_employee_update(emp, None)

		emp.custom_attendance_system = "Standard"
		on_employee_update(emp, None)

		assignment = get_flex_assignment(emp.name)
		self.assertIsNotNone(assignment)
		self.assertEqual(str(assignment.end_date), today())

	def test_no_duplicate_assignment_on_repeated_save(self):
		emp = make_employee("Flexible Hours")
		from employee_custom_attendance.attendance.employee_hooks import on_employee_update

		on_employee_update(emp, None)
		on_employee_update(emp, None)  # called twice

		count = frappe.db.count(
			"Shift Assignment",
			{"employee": emp.name, "shift_type": "Flexible Hours", "docstatus": 1},
		)
		self.assertEqual(count, 1)
