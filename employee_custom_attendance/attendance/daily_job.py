import frappe
from frappe.utils import flt, get_first_day, get_last_day, getdate, today
from hrms.hr.doctype.employee_checkin.employee_checkin import (
	calculate_working_hours,
	update_attendance_in_checkins,
)


def process_flex_attendance():
	"""Entry point called by Frappe scheduler (daily)."""
	date = getdate(today())
	flex_employees = frappe.get_all(
		"Employee",
		filters={"custom_attendance_system": "Flexible Hours", "status": "Active"},
		pluck="name",
	)
	for employee in flex_employees:
		try:
			process_employee_for_date(employee, date)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"Flex attendance error: {employee} on {date}",
			)

	# Notifications run for ALL active employees (Standard + Flexible)
	from employee_custom_attendance.attendance.notifications import notify_article_69, notify_lateness_cap

	all_employees = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name")
	for employee in all_employees:
		try:
			notify_lateness_cap(employee, date)
			notify_article_69(employee, date)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"Notification error: {employee} on {date}",
			)


def process_employee_for_date(employee, date):
	"""Process one flex employee for a given date. Idempotent."""
	# Skip if attendance already marked
	if frappe.db.exists("Attendance", {"employee": employee, "attendance_date": date}):
		return

	settings = frappe.get_single("Flexible Hours Settings")

	# Check for approved leave
	if _has_approved_leave(employee, date):
		return

	logs = _get_checkins(employee, date)

	if not logs:
		_handle_missing_fingerprint(employee, date, settings)
		return

	total_hours, in_time, out_time = calculate_working_hours(
		logs,
		check_in_out_type="Alternating entries as IN and OUT during the same shift",
		working_hours_calc_type="Every Valid Check-in and Check-out",
	)
	effective_hours = min(flt(total_hours), flt(settings.daily_max_hours))
	status = "Present" if effective_hours >= flt(settings.daily_min_hours) else "Absent"

	attendance = _create_attendance(employee, date, status, effective_hours, in_time, out_time)
	update_attendance_in_checkins([log.name for log in logs], attendance.name)

	if status == "Absent" and settings.penalty_type_for_shortfall:
		_create_shortfall_penalty(employee, date, effective_hours, settings)


def _get_checkins(employee, date):
	from frappe.utils import add_days

	day_start = frappe.utils.get_datetime(str(date) + " 00:00:00")
	day_end = frappe.utils.get_datetime(str(add_days(date, 1)) + " 00:00:00")

	return frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": employee,
			"time": ["between", [day_start, day_end]],
			"skip_auto_attendance": 1,
			"attendance": ["is", "not set"],
		},
		fields=["name", "employee", "time", "log_type"],
		order_by="time asc",
	)


def _has_approved_leave(employee, date):
	return frappe.db.exists(
		"Leave Application",
		{
			"employee": employee,
			"from_date": ["<=", date],
			"to_date": [">=", date],
			"status": "Approved",
			"docstatus": 1,
		},
	)


def _create_attendance(employee, date, status, working_hours, in_time, out_time):
	company = frappe.db.get_value("Employee", employee, "company")
	att = frappe.get_doc(
		{
			"doctype": "Attendance",
			"employee": employee,
			"attendance_date": date,
			"status": status,
			"working_hours": working_hours,
			"in_time": in_time,
			"out_time": out_time,
			"shift": "Flexible Hours",
			"company": company,
		}
	)
	att.insert(ignore_permissions=True)
	att.submit()
	return att


def _handle_missing_fingerprint(employee, date, settings):
	"""Count missing-fingerprint days this month. >=3 prior → Half Day, else Absent."""
	month_start = get_first_day(date)
	month_end = get_last_day(date)

	missing_count = frappe.db.count(
		"Attendance",
		{
			"employee": employee,
			"attendance_date": ["between", [month_start, month_end]],
			"custom_missing_fingerprint": 1,
			"docstatus": 1,
		},
	)

	status = "Half Day" if missing_count >= 3 else "Absent"
	company = frappe.db.get_value("Employee", employee, "company")

	att = frappe.get_doc(
		{
			"doctype": "Attendance",
			"employee": employee,
			"attendance_date": date,
			"status": status,
			"working_hours": 0,
			"shift": "Flexible Hours",
			"company": company,
			"custom_missing_fingerprint": 1,
		}
	)
	att.insert(ignore_permissions=True)
	att.submit()


def _create_shortfall_penalty(employee, date, effective_hours, settings):
	"""Create a draft Employee Penalty for a daily hours shortfall."""
	if frappe.db.exists(
		"Employee Penalty",
		{
			"employee": employee,
			"penalty_date": date,
			"penalty_type": settings.penalty_type_for_shortfall,
		},
	):
		return

	penalty = frappe.get_doc(
		{
			"doctype": "Employee Penalty",
			"employee": employee,
			"penalty_date": date,
			"penalty_month": get_first_day(date),
			"penalty_type": settings.penalty_type_for_shortfall,
			"remarks": (
				f"Flexible hours shortfall: {effective_hours:.1f}h / {settings.daily_min_hours}h required"
			),
		}
	)
	penalty.insert(ignore_permissions=True)
	# Leave as docstatus=0 (Draft) for HR to review and submit
