import calendar

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, today
from hrms.hr.doctype.employee_checkin.employee_checkin import (
	calculate_working_hours,
	update_attendance_in_checkins,
)


def process_flex_attendance(process_date=None):
	"""Entry point called by Frappe scheduler (daily)."""
	# date = getdate(add_days(today(), -1))
	date = getdate(process_date) if process_date else getdate(add_days(today(), -1))

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

	leave_application = _get_approved_leave(employee, date)
	if leave_application:
		# Reuse core HRMS logic so leave_type/half-day/holiday handling stays
		# consistent with what Leave Application.on_submit() does itself. This
		# also self-heals leave applications that were bulk-imported without
		# triggering that hook, so no Attendance was ever created for the day.
		frappe.get_doc("Leave Application", leave_application).update_attendance()
		return

	is_holiday = _is_holiday(employee, date)
	logs = _get_checkins(employee, date)

	if not logs:
		_mark_absent_or_skip_holiday(employee, date, settings, is_holiday)
		return

	# Drop the last checkin if odd count so we only pass complete IN/OUT pairs.
	# This prevents the next day's first checkin from being double-counted as both
	# the OUT for today and the IN for tomorrow.
	if len(logs) % 2 == 1:
		logs = logs[:-1]

	if not logs:
		# The only checkin(s) fetched were the overnight-window bleed from the next
		# day (e.g. a holiday's window catches the next workday's first checkin),
		# not a real, pairable shift on `date` itself.
		_mark_absent_or_skip_holiday(employee, date, settings, is_holiday)
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
	# Window extends to noon next day to capture overnight shifts (e.g. 20:00 IN → 02:00 OUT).
	# The `attendance: not set` filter prevents double-counting across days.
	day_start = frappe.utils.get_datetime(str(date) + " 00:00:00")
	day_end = frappe.utils.get_datetime(str(add_days(date, 1)) + " 12:00:00")

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



def _get_approved_leave(employee, date):
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


def _mark_absent_or_skip_holiday(employee, date, settings, is_holiday):
	"""No usable checkins for `date`. On a holiday, skip silently — the employee
	wasn't expected to work, so no Attendance record should be created at all.
	Otherwise fall back to the missing-fingerprint Absent/Half Day counter."""
	if is_holiday:
		return
	att = _handle_missing_fingerprint(employee, date, settings)
	if att and att.status == "Absent" and settings.penalty_type_for_shortfall:
		_create_shortfall_penalty(employee, date, 0, settings)


def _is_holiday(employee, date):
	holiday_list = frappe.db.get_value("Employee", employee, "holiday_list")
	if not holiday_list:
		return False
	return bool(frappe.db.exists("Holiday", {"parent": holiday_list, "holiday_date": date}))


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


def _get_permission_period(date):
	"""Return (period_start, period_end) for the permission period containing date.

	Reads period_start_day / period_end_day from Permission Settings.
	Example with start_day=21, end_day=20:
	  Jun 5  → May 21 – Jun 20
	  Jun 25 → Jun 21 – Jul 20
	Falls back to calendar month when start_day <= 1.
	"""
	settings = frappe.get_single("Permission Settings")
	start_day = int(settings.period_start_day or 1)

	if start_day <= 1:
		return get_first_day(date), get_last_day(date)

	end_day = int(settings.period_end_day or 20)
	d = getdate(date)

	if d.day >= start_day:
		# e.g. Jun 25 → period Jun 21 – Jul 20
		period_start = d.replace(day=start_day)
		next_month = getdate(add_months(str(d), 1))
		max_day = calendar.monthrange(next_month.year, next_month.month)[1]
		period_end = next_month.replace(day=min(end_day, max_day))
	else:
		# e.g. Jun 5 → period May 21 – Jun 20
		prev_month = getdate(add_months(str(d), -1))
		period_start = prev_month.replace(day=start_day)
		max_day = calendar.monthrange(d.year, d.month)[1]
		period_end = d.replace(day=min(end_day, max_day))

	return period_start, period_end


def _handle_missing_fingerprint(employee, date, settings):
	"""Count missing-fingerprint days this permission period. >=3 prior → Half Day, else Absent."""
	month_start, _ = _get_permission_period(date)

	missing_count = frappe.db.count(
		"Attendance",
		{
			"employee": employee,
			"attendance_date": ["between", [month_start, add_days(date, -1)]],
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
	return att


def backfill_on_leave_attendance():
	"""
	Create/update 'On Leave' (or 'Half Day') Attendance for every day covered by an
	approved, submitted Leave Application, for ALL employees (not just Flexible
	Hours). Reuses Leave Application.update_attendance() so behaviour (holiday
	exclusion, half-day, leave_type/leave_application linking) matches what
	on_submit() does normally. Needed because some Leave Applications were
	bulk-imported without triggering that hook, leaving no Attendance behind.

	Idempotent — safe to run repeatedly.

	Usage via bench:
	  bench --site <site> execute \
	    employee_custom_attendance.attendance.daily_job.backfill_on_leave_attendance
	"""
	leave_applications = frappe.get_all(
		"Leave Application",
		filters={"status": "Approved", "docstatus": 1},
		pluck="name",
	)
	for name in leave_applications:
		try:
			frappe.get_doc("Leave Application", name).update_attendance()
			frappe.db.commit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"backfill_on_leave_attendance error: {name}")


def backfill_attendance_requests():
	"""
	Create/update Attendance for every day covered by a submitted Attendance
	Request, for ALL employees. Reuses Attendance Request.create_attendance_records()
	so behaviour (holiday/leave skipping, half-day, Work From Home status,
	attendance_request linking) matches what on_submit() does normally. Needed
	because some Attendance Requests were bulk-imported without triggering that
	hook, leaving no Attendance behind.

	Idempotent — safe to run repeatedly.

	Usage via bench:
	  bench --site <site> execute \
	    employee_custom_attendance.attendance.daily_job.backfill_attendance_requests
	"""
	attendance_requests = frappe.get_all(
		"Attendance Request",
		filters={"docstatus": 1},
		pluck="name",
	)
	for name in attendance_requests:
		try:
			frappe.get_doc("Attendance Request", name).create_attendance_records()
			frappe.db.commit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"backfill_attendance_requests error: {name}")


def reprocess_all(from_date, to_date):
	"""
	Reset and reprocess all flex employees for every date in the given range.

	Two-phase to prevent double-counting across overnight shifts:
	  Phase 1 — delete all attendances and unlink all checkins in the range first.
	  Phase 2 — process each date in order with clean, unlinked checkins.

	Usage via bench:
	  bench --site <site> execute employee_custom_attendance.attendance.daily_job.reprocess_all \
	    --args "['2026-06-16', '2026-06-18']"
	"""
	from_date = getdate(from_date)
	to_date = getdate(to_date)

	flex_employees = frappe.get_all(
		"Employee",
		filters={"custom_attendance_system": "Flexible Hours", "status": "Active"},
		pluck="name",
	)

	# Phase 1: delete attendances and free all checkins across the full range first.
	date = from_date
	while date <= to_date:
		for employee in flex_employees:
			try:
				_reset_attendance(employee, date)
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					f"reprocess_all reset error: {employee} on {date}",
				)
		date = getdate(add_days(date, 1))

	# Phase 2: process each date in order with all checkins now unlinked.
	date = from_date
	while date <= to_date:
		for employee in flex_employees:
			try:
				process_employee_for_date(employee, date)
				frappe.db.commit()
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					f"reprocess_all error: {employee} on {date}",
				)
		date = getdate(add_days(date, 1))


def _reset_attendance(employee, date):
	"""Delete attendance and unlink checkins linked to it. Does not reprocess."""
	att_name = frappe.db.get_value(
		"Attendance", {"employee": employee, "attendance_date": date}
	)
	if att_name:
		att_doc = frappe.get_doc("Attendance", att_name)
		if att_doc.docstatus == 1:
			att_doc.cancel()
		att_doc.delete(ignore_permissions=True)
		# Unlink only checkins that belonged to this attendance record.
		frappe.db.set_value("Employee Checkin", {"attendance": att_name}, {"attendance": None})
		frappe.db.commit()

	# Also ensure skip_auto_attendance=1 on any unlinked checkins in the day window.
	day_start = frappe.utils.get_datetime(str(date) + " 00:00:00")
	day_end = frappe.utils.get_datetime(str(add_days(date, 1)) + " 12:00:00")
	frappe.db.set_value(
		"Employee Checkin",
		{"employee": employee, "time": ["between", [day_start, day_end]], "skip_auto_attendance": 0},
		{"skip_auto_attendance": 1},
	)
	frappe.db.commit()


def reset_and_reprocess(employee, date):
	"""
	Cancel & delete existing Attendance for employee+date, clear attendance links
	on checkins, ensure skip_auto_attendance=1, then reprocess via process_employee_for_date.

	Usage via bench:
	  bench --site <site> execute employee_custom_attendance.attendance.daily_job.reset_and_reprocess \
	    --args "['HR-EMP-00079', '2026-06-16']"
	"""
	date = getdate(date)
	_reset_attendance(employee, date)
	process_employee_for_date(employee, date)
	frappe.db.commit()
	print(f"Done: {employee} on {date}")


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
			"penalty_month": date,
			"penalty_type": settings.penalty_type_for_shortfall,
			"reason": f"Flexible hours shortfall on {date}",
			"remarks": (
				f"Flexible hours shortfall: {effective_hours:.1f}h / {settings.daily_min_hours}h required"
			),
		}
	)
	penalty.insert(ignore_permissions=True)
	# Leave as docstatus=0 (Draft) for HR to review and submit
