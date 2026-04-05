from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, get_first_day, getdate, today


def make_employee_with_attendance(attendance_system, late_hours):
	"""Create employee and attendance records simulating total late hours."""
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
	emp = frappe.get_doc({
		"doctype": "Employee",
		"first_name": "Notify",
		"last_name": "Test",
		"company": company,
		"date_of_joining": "2025-01-01",
		"date_of_birth": "1990-01-01",
		"gender": "Male",
		"custom_attendance_system": attendance_system,
	})
	emp.insert(ignore_permissions=True)

	if attendance_system == "Flexible Hours":
		# Create 2 records at start of month with shortfall = late_hours/2 each.
		# Using a fixed count (2) makes the total independent of the current day-of-month.
		per_record_shortfall = late_hours / 2
		working = max(0.0, 7.0 - per_record_shortfall)
		month_start = get_first_day(today())
		for i in range(2):
			d = add_days(month_start, i)
			att = frappe.get_doc({
				"doctype": "Attendance",
				"employee": emp.name,
				"attendance_date": d,
				"status": "Absent",
				"working_hours": working,
				"shift": "Flexible Hours",
				"company": company,
			})
			att.insert(ignore_permissions=True)
			att.submit()

	return emp


class TestLatenessCapNotification(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	@patch("employee_custom_attendance.attendance.notifications._get_hr_recipients", return_value=["hr@test.com"])
	@patch("frappe.sendmail")
	def test_alert_sent_when_over_5h_threshold(self, mock_sendmail, _mock_recipients):
		from employee_custom_attendance.attendance.notifications import notify_lateness_cap
		emp = make_employee_with_attendance("Flexible Hours", late_hours=6)  # 6h shortfall
		notify_lateness_cap(emp.name, today())
		self.assertTrue(mock_sendmail.called)
		call_kwargs = mock_sendmail.call_args[1]
		self.assertIn("Lateness Cap Reached", call_kwargs["subject"])

	@patch("employee_custom_attendance.attendance.notifications._get_hr_recipients", return_value=["hr@test.com"])
	@patch("frappe.sendmail")
	def test_alert_not_sent_below_threshold(self, mock_sendmail, _mock_recipients):
		from employee_custom_attendance.attendance.notifications import notify_lateness_cap
		emp = make_employee_with_attendance("Flexible Hours", late_hours=2)  # only 2h
		notify_lateness_cap(emp.name, today())
		self.assertFalse(mock_sendmail.called)

	@patch("employee_custom_attendance.attendance.notifications._get_hr_recipients", return_value=["hr@test.com"])
	@patch("frappe.sendmail")
	def test_alert_sent_only_once_per_month(self, mock_sendmail, _mock_recipients):
		from employee_custom_attendance.attendance.notifications import notify_lateness_cap
		emp = make_employee_with_attendance("Flexible Hours", late_hours=6)
		notify_lateness_cap(emp.name, today())
		notify_lateness_cap(emp.name, today())  # second call same month
		self.assertEqual(mock_sendmail.call_count, 1)
