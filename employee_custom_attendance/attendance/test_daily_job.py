from datetime import datetime

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_first_day, getdate, now_datetime, today


def make_flex_employee():
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
	emp = frappe.get_doc({
		"doctype": "Employee",
		"first_name": "FlexJob",
		"last_name": "Test",
		"company": company,
		"date_of_joining": "2025-01-01",
		"date_of_birth": "1990-01-01",
		"gender": "Male",
		"custom_attendance_system": "Flexible Hours",
	})
	emp.insert(ignore_permissions=True)
	return emp


def make_checkin(employee, dt, log_type):
	"""Create a saved Employee Checkin with skip_auto_attendance=1."""
	doc = frappe.get_doc({
		"doctype": "Employee Checkin",
		"employee": employee,
		"time": dt,
		"log_type": log_type,
		"shift": "Flexible Hours",
		"skip_auto_attendance": 1,
	})
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True)
	return doc


class TestDailyJobAttendance(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_present_when_hours_meet_minimum(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date
		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())

		make_checkin(emp.name, base.replace(hour=8), "IN")
		make_checkin(emp.name, base.replace(hour=16), "OUT")  # 8 hours

		process_employee_for_date(emp.name, date)

		att = frappe.db.get_value(
			"Attendance",
			{"employee": emp.name, "attendance_date": date},
			["status", "working_hours"],
			as_dict=True,
		)
		self.assertEqual(att.status, "Present")
		self.assertAlmostEqual(att.working_hours, 8.0, places=1)

	def test_absent_when_hours_below_minimum(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date
		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())

		make_checkin(emp.name, base.replace(hour=9), "IN")
		make_checkin(emp.name, base.replace(hour=14), "OUT")  # 5 hours < 7

		process_employee_for_date(emp.name, date)

		status = frappe.db.get_value(
			"Attendance",
			{"employee": emp.name, "attendance_date": date},
			"status",
		)
		self.assertEqual(status, "Absent")

	def test_hours_capped_at_daily_max(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date
		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())

		make_checkin(emp.name, base.replace(hour=7), "IN")
		make_checkin(emp.name, base.replace(hour=20), "OUT")  # 13 hours → capped at 11

		process_employee_for_date(emp.name, date)

		working_hours = frappe.db.get_value(
			"Attendance",
			{"employee": emp.name, "attendance_date": date},
			"working_hours",
		)
		self.assertLessEqual(working_hours, 11.0)

	def test_multiple_intervals_summed(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date
		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())

		# 8:00-12:00 (4h) + 13:00-17:00 (4h) = 8 total
		make_checkin(emp.name, base.replace(hour=8), "IN")
		make_checkin(emp.name, base.replace(hour=12), "OUT")
		make_checkin(emp.name, base.replace(hour=13), "IN")
		make_checkin(emp.name, base.replace(hour=17), "OUT")

		process_employee_for_date(emp.name, date)

		working_hours = frappe.db.get_value(
			"Attendance",
			{"employee": emp.name, "attendance_date": date},
			"working_hours",
		)
		self.assertAlmostEqual(working_hours, 8.0, places=1)

	def test_idempotent_second_run(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date
		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())
		make_checkin(emp.name, base.replace(hour=8), "IN")
		make_checkin(emp.name, base.replace(hour=16), "OUT")

		process_employee_for_date(emp.name, date)
		process_employee_for_date(emp.name, date)  # second run

		count = frappe.db.count(
			"Attendance",
			{"employee": emp.name, "attendance_date": date},
		)
		self.assertEqual(count, 1)

	def test_missing_fingerprint_marks_half_day_after_3(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date
		emp = make_flex_employee()
		company = frappe.db.get_value("Employee", emp.name, "company")
		# Simulate 3 prior no-checkin days this month
		month_start = get_first_day(today())
		for i in range(3):
			d = add_days(month_start, i)
			if getdate(d) < getdate(today()):
				att = frappe.get_doc({
					"doctype": "Attendance",
					"employee": emp.name,
					"attendance_date": d,
					"status": "Absent",
					"company": company,
					"custom_missing_fingerprint": 1,
				})
				att.insert(ignore_permissions=True)
				att.submit()

		# 4th day with no checkins
		date = getdate(today())
		process_employee_for_date(emp.name, date)

		status = frappe.db.get_value(
			"Attendance",
			{"employee": emp.name, "attendance_date": date},
			"status",
		)
		self.assertEqual(status, "Half Day")
