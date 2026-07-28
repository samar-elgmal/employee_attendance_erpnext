from datetime import datetime

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate


def make_shift_type(name, start_time, end_time, grace_minutes=0):
	if frappe.db.exists("Shift Type", name):
		return frappe.get_doc("Shift Type", name)
	doc = frappe.get_doc(
		{
			"doctype": "Shift Type",
			"name": name,
			"start_time": start_time,
			"end_time": end_time,
			"begin_check_in_before_shift_start_time": 0,
			"allow_check_out_after_shift_end_time": grace_minutes,
			"enable_auto_attendance": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def make_employee():
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
	emp = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": "ShiftExpiry",
			"last_name": "Test",
			"company": company,
			"date_of_joining": "2025-01-01",
			"date_of_birth": "1990-01-01",
			"gender": "Male",
		}
	)
	emp.insert(ignore_permissions=True)
	return emp


def make_shift_assignment(employee, shift_type, start_date, end_date):
	doc = frappe.get_doc(
		{
			"doctype": "Shift Assignment",
			"employee": employee,
			"shift_type": shift_type,
			"start_date": start_date,
			"end_date": end_date,
			"status": "Active",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


class TestMarkExpiredShiftAssignmentsAsInactive(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_assignment_stays_active_within_checkout_grace_period(self):
		from employee_custom_attendance.attendance.shift_expiry import (
			mark_expired_shift_assignments_as_inactive,
		)

		make_shift_type("3 : 11 Test", "15:00:00", "23:00:00", grace_minutes=360)
		emp = make_employee()
		yesterday = add_days(getdate(), -1)
		assignment = make_shift_assignment(emp.name, "3 : 11 Test", yesterday, yesterday)

		# 00:30 the next day -> still inside the 360 min (until 05:00) grace window
		now = datetime.combine(getdate(), datetime.min.time()).replace(hour=0, minute=30)
		mark_expired_shift_assignments_as_inactive(now=now)

		self.assertEqual(frappe.db.get_value("Shift Assignment", assignment.name, "status"), "Active")

	def test_assignment_deactivated_after_checkout_grace_period_elapses(self):
		from employee_custom_attendance.attendance.shift_expiry import (
			mark_expired_shift_assignments_as_inactive,
		)

		make_shift_type("3 : 11 Test", "15:00:00", "23:00:00", grace_minutes=360)
		emp = make_employee()
		yesterday = add_days(getdate(), -1)
		assignment = make_shift_assignment(emp.name, "3 : 11 Test", yesterday, yesterday)

		# 06:00 the next day -> 1 hour past the 05:00 grace cutoff
		now = datetime.combine(getdate(), datetime.min.time()).replace(hour=6, minute=0)
		mark_expired_shift_assignments_as_inactive(now=now)

		self.assertEqual(frappe.db.get_value("Shift Assignment", assignment.name, "status"), "Inactive")

	def test_assignment_ending_days_ago_with_no_grace_is_deactivated(self):
		from employee_custom_attendance.attendance.shift_expiry import (
			mark_expired_shift_assignments_as_inactive,
		)

		make_shift_type("9 : 5 Test", "09:00:00", "17:00:00", grace_minutes=0)
		emp = make_employee()
		end_date = add_days(getdate(), -5)
		assignment = make_shift_assignment(emp.name, "9 : 5 Test", add_days(end_date, -1), end_date)

		mark_expired_shift_assignments_as_inactive()

		self.assertEqual(frappe.db.get_value("Shift Assignment", assignment.name, "status"), "Inactive")
