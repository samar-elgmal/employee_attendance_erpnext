from datetime import datetime

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_first_day, getdate, now_datetime, today


def make_flex_employee():
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
	emp = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": "FlexJob",
			"last_name": "Test",
			"company": company,
			"date_of_joining": "2025-01-01",
			"date_of_birth": "1990-01-01",
			"gender": "Male",
			"custom_attendance_system": "Flexible Hours",
		}
	)
	emp.insert(ignore_permissions=True)
	return emp


def make_holiday_list(name, holiday_date):
	hl = frappe.get_doc(
		{
			"doctype": "Holiday List",
			"holiday_list_name": name,
			"from_date": add_days(holiday_date, -30),
			"to_date": add_days(holiday_date, 30),
			"holidays": [{"holiday_date": holiday_date, "weekly_off": 1, "description": "Test Holiday"}],
		}
	)
	hl.insert(ignore_permissions=True)
	return hl


def make_approved_leave(employee, from_date, to_date, leave_type="Test Flex Leave Type"):
	"""Create an Approved, submitted Leave Application without going through
	submit() — mirrors a bulk import that sets docstatus=1 directly and never
	fires on_submit(), so Leave Application.update_attendance() never ran."""
	if not frappe.db.exists("Leave Type", leave_type):
		# include_holiday=1 skips the employee-holiday-list lookup in
		# update_attendance(), which would otherwise require a default Holiday
		# List on the employee/company that this test doesn't set up.
		frappe.get_doc(
			{"doctype": "Leave Type", "leave_type_name": leave_type, "include_holiday": 1}
		).insert(ignore_permissions=True)

	la = frappe.get_doc(
		{
			"doctype": "Leave Application",
			"employee": employee,
			"leave_type": leave_type,
			"from_date": from_date,
			"to_date": to_date,
			"company": frappe.db.get_value("Employee", employee, "company"),
			"status": "Approved",
		}
	)
	la.flags.ignore_validate = True
	la.insert(ignore_permissions=True)
	frappe.db.set_value("Leave Application", la.name, "docstatus", 1)
	return la.name


def make_approved_attendance_request(employee, from_date, to_date, reason="On Duty"):
	"""Create a submitted Attendance Request without going through submit() —
	mirrors a bulk import that sets docstatus=1 directly and never fires
	on_submit(), so Attendance Request.create_attendance_records() never ran."""
	company = frappe.db.get_value("Employee", employee, "company")
	ar = frappe.get_doc(
		{
			"doctype": "Attendance Request",
			"employee": employee,
			"company": company,
			"from_date": from_date,
			"to_date": to_date,
			"reason": reason,
			# Skips the employee-holiday-list lookup in should_mark_attendance(),
			# which would otherwise require a default Holiday List on the
			# employee/company that this test doesn't set up.
			"include_holidays": 1,
		}
	)
	ar.flags.ignore_validate = True
	ar.insert(ignore_permissions=True)
	frappe.db.set_value("Attendance Request", ar.name, "docstatus", 1)
	return ar.name


def make_checkin(employee, dt, log_type):
	"""Create a saved Employee Checkin with skip_auto_attendance=1."""
	doc = frappe.get_doc(
		{
			"doctype": "Employee Checkin",
			"employee": employee,
			"time": dt,
			"log_type": log_type,
			"shift": "Flexible Hours",
			"skip_auto_attendance": 1,
		}
	)
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

	def test_holiday_not_marked_absent_by_next_day_checkin_bleed(self):
		"""A holiday's checkin window extends to noon the next day to catch overnight
		shifts. That must not cause the next day's first checkin to be treated as
		"the employee showed up" and mark the holiday itself Absent."""
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date

		emp = make_flex_employee()
		date = getdate(today())
		holiday_list = make_holiday_list(f"Test Holiday List {emp.name}", date)
		frappe.db.set_value("Employee", emp.name, "holiday_list", holiday_list.name)

		next_day_base = datetime.combine(add_days(date, 1), datetime.min.time())
		make_checkin(emp.name, next_day_base.replace(hour=9), "IN")  # bleeds into date's window

		process_employee_for_date(emp.name, date)

		att = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": date}, "status"
		)
		self.assertIsNone(att)

	def test_approved_leave_creates_on_leave_attendance(self):
		"""A flex employee with an approved leave that never went through the
		normal submit() flow (e.g. bulk-imported) should still get an 'On Leave'
		Attendance instead of being silently skipped or later marked Absent."""
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date

		emp = make_flex_employee()
		date = getdate(today())
		make_approved_leave(emp.name, date, date)

		process_employee_for_date(emp.name, date)

		status = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": date}, "status"
		)
		self.assertEqual(status, "On Leave")

	def test_backfill_attendance_requests_creates_attendance(self):
		"""An Attendance Request that never went through the normal submit() flow
		(e.g. bulk-imported) should get its Attendance backfilled."""
		from employee_custom_attendance.attendance.daily_job import backfill_attendance_requests

		emp = make_flex_employee()
		date = getdate(today())
		make_approved_attendance_request(emp.name, date, date)

		backfill_attendance_requests()

		status = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": date}, "status"
		)
		self.assertEqual(status, "Present")

	def test_missing_fingerprint_marks_half_day_after_3(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date

		emp = make_flex_employee()
		company = frappe.db.get_value("Employee", emp.name, "company")
		# Simulate 3 prior no-checkin days this month
		month_start = get_first_day(today())
		for i in range(3):
			d = add_days(month_start, i)
			if getdate(d) < getdate(today()):
				att = frappe.get_doc(
					{
						"doctype": "Attendance",
						"employee": emp.name,
						"attendance_date": d,
						"status": "Absent",
						"company": company,
						"custom_missing_fingerprint": 1,
					}
				)
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
