"""One-off backfill for Employee Checkins that lost their shift/attendance link
to the midnight race between HRMS's stock `mark_expired_shift_assignments_as_inactive`
job and Employee Checkin.fetch_shift() - see shift_expiry.py for the root cause.

Usage (read-only survey first):
    bench --site <site> execute \
        employee_custom_attendance.attendance.backfill_shift_expiry.scope

Then, once reviewed:
    bench --site <site> execute \
        employee_custom_attendance.attendance.backfill_shift_expiry.run
"""

from datetime import datetime, timedelta

import frappe
from frappe.utils import add_days, get_datetime

from hrms.hr.doctype.shift_assignment.shift_assignment import get_shift_type
from hrms.hr.doctype.shift_type.shift_type import ShiftType

from employee_custom_attendance.attendance.shift_expiry import (
	_actual_end_datetime,
	mark_expired_shift_assignments_as_inactive,
)


def _actual_start_datetime(shift_type_name: str, start_date) -> datetime:
	shift_type = get_shift_type(shift_type_name)
	start_dt = datetime.combine(start_date, datetime.min.time()) + shift_type.start_time
	return start_dt - timedelta(minutes=shift_type.begin_check_in_before_shift_start_time or 0)


def _assignment_covering(employee, date):
	"""Most relevant docstatus=1 Shift Assignment (any status) covering `date`."""
	rows = frappe.db.sql(
		"""
		select name, shift_type, start_date, end_date
		from `tabShift Assignment`
		where employee=%s and docstatus=1
		and start_date <= %s
		and (end_date is null or end_date >= %s)
		order by modified desc limit 1
		""",
		(employee, date, date),
		as_dict=True,
	)
	return rows[0] if rows else None


def _find_broken_checkins():
	"""Employee Checkins with no shift resolved, for which a Shift Assignment -
	covering either the checkin's own day (e.g. delayed device sync landed after
	that day's assignment had already expired) or the previous day (the midnight
	grace-period race) - genuinely covers the checkin once each assignment's own
	per-day window (including its shift's grace period) is taken into account.

	Returns list of dicts: {checkin, employee, time, shift_assignment, shift_type}
	"""
	candidates = frappe.db.sql(
		"""
		select name, employee, time
		from `tabEmployee Checkin`
		where (shift is null or shift = '') and offshift = 1 and (attendance is null or attendance = '')
		""",
		as_dict=True,
	)

	broken = []
	for row in candidates:
		ci_time = get_datetime(row.time)

		for day in (ci_time.date(), add_days(ci_time.date(), -1)):
			assignment = _assignment_covering(row.employee, day)
			if not assignment:
				continue

			actual_start = _actual_start_datetime(assignment.shift_type, max(assignment.start_date, day))
			actual_end = _actual_end_datetime(assignment.shift_type, min(assignment.end_date or day, day))

			if actual_start <= ci_time <= actual_end:
				broken.append(
					{
						"checkin": row.name,
						"employee": row.employee,
						"time": row.time,
						"shift_assignment": assignment.name,
						"shift_type": assignment.shift_type,
					}
				)
				break
	return broken


def scope():
	"""Read-only: report how many checkins/employees/shift types are affected."""
	broken = _find_broken_checkins()
	employees = {b["employee"] for b in broken}
	shift_types = {b["shift_type"] for b in broken}

	print(f"Broken checkins: {len(broken)}")
	print(f"Employees affected: {len(employees)}")
	print(f"Shift types involved: {sorted(shift_types)}")
	for b in broken:
		print(b)
	return broken


def run(dry_run: bool = False):
	"""Repair the broken checkins found by scope() and let HRMS mark attendance
	for them the normal way, then re-settle Shift Assignment statuses.
	"""
	broken = _find_broken_checkins()
	print(f"Repairing {len(broken)} checkins across {len({b['employee'] for b in broken})} employees...")
	if dry_run:
		print("Dry run - no changes made.")
		return broken

	touched_assignments = {b["shift_assignment"] for b in broken}
	touched_shift_types = {b["shift_type"] for b in broken}

	# 1. Temporarily reactivate the assignments involved so HRMS's own shift
	#    resolution (fetch_shift -> get_actual_start_end_datetime_of_shift) finds them.
	for name in touched_assignments:
		frappe.db.set_value("Shift Assignment", name, "status", "Active")
	frappe.db.commit()  # nosemgrep

	fixed, failed = [], []
	for b in broken:
		try:
			doc = frappe.get_doc("Employee Checkin", b["checkin"])
			doc.save(ignore_permissions=True)
			if doc.shift:
				fixed.append(b["checkin"])
			else:
				failed.append(b["checkin"])
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"backfill_shift_expiry: {b['checkin']}")
			failed.append(b["checkin"])
	frappe.db.commit()  # nosemgrep

	print(f"Checkins fixed: {len(fixed)}, still unresolved: {len(failed)}")
	if failed:
		print("Unresolved checkins (needs manual review):", failed)

	# 2. Let HRMS mark attendance for these checkins the normal way.
	for shift_type_name in touched_shift_types - {"Flexible Hours"}:
		shift_type = frappe.get_cached_doc("Shift Type", shift_type_name)
		if isinstance(shift_type, ShiftType) and not shift_type.has_incorrect_shift_config():
			shift_type.process_auto_attendance()
	frappe.db.commit()  # nosemgrep

	# 2b. Flexible Hours checkins don't go through Shift Type auto-attendance
	# (enable_auto_attendance=0 by design) - route them through the flex daily
	# job instead, now that fetch_shift() has set skip_auto_attendance=1 on them.
	if "Flexible Hours" in touched_shift_types:
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date

		flex_targets = {
			(b["employee"], get_datetime(b["time"]).date())
			for b in broken
			if b["checkin"] in fixed and b["shift_type"] == "Flexible Hours"
		}
		for employee, date in flex_targets:
			try:
				process_employee_for_date(employee, date)
			except Exception:
				frappe.log_error(frappe.get_traceback(), f"backfill_shift_expiry flex: {employee} {date}")
		frappe.db.commit()  # nosemgrep

	# 3. Re-settle assignment statuses against real time (self-correcting, safe to
	#    call regardless of whether an assignment was reactivated above).
	mark_expired_shift_assignments_as_inactive()
	frappe.db.commit()  # nosemgrep

	return {"fixed": fixed, "failed": failed}
