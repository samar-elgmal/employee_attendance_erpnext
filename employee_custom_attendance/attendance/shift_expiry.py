from datetime import datetime, timedelta

import frappe
from frappe.utils import getdate, now_datetime
from hrms.hr.doctype.shift_assignment.shift_assignment import get_shift_type

# HRMS runs this same check daily but only compares Shift Assignment.end_date
# against today's calendar date, ignoring the shift's checkout grace period
# (Shift Type.allow_check_out_after_shift_end_time). That races against
# Employee Checkin.fetch_shift(), which resolves a checkin's shift by querying
# Shift Assignments with status="Active" only. Whichever runs first each day
# right after midnight wins: if HRMS's job deactivates yesterday's assignment
# before a post-midnight checkout (still inside its grace window) is saved,
# that checkin permanently loses its shift/attendance link.
#
# This replacement only deactivates an assignment once its shift, including
# the grace period, has actually finished. HRMS's own scheduled job is
# disabled in install.py so only this version runs.
HRMS_EXPIRY_JOB_METHOD = (
	"hrms.hr.doctype.shift_assignment.shift_assignment.mark_expired_shift_assignments_as_inactive"
)


def _actual_end_datetime(shift_type_name: str, end_date) -> datetime:
	"""Datetime the shift on `end_date` truly finishes, including its checkout grace period."""
	shift_type = get_shift_type(shift_type_name)
	start_time = shift_type.start_time
	end_time = shift_type.end_time
	grace_minutes = shift_type.allow_check_out_after_shift_end_time or 0

	end_datetime = datetime.combine(end_date, datetime.min.time()) + end_time
	if end_time <= start_time:
		# shift crosses midnight (e.g. 22:00 - 06:00): end time lands on the day after end_date
		end_datetime += timedelta(days=1)

	return end_datetime + timedelta(minutes=grace_minutes)


def mark_expired_shift_assignments_as_inactive(now=None):
	"""Entry point called by Frappe scheduler (daily) in place of HRMS's stock job."""
	now = now or now_datetime()
	today = getdate(now)

	assignment = frappe.qb.DocType("Shift Assignment")
	candidates = (
		frappe.qb.from_(assignment)
		.select(assignment.name, assignment.shift_type, assignment.end_date)
		.where(
			(assignment.docstatus == 1)
			& (assignment.status == "Active")
			& (assignment.end_date.isnotnull())
			& (assignment.end_date < today)
		)
	).run(as_dict=True)

	for row in candidates:
		if now < _actual_end_datetime(row.shift_type, row.end_date):
			continue  # still inside the checkout grace period, leave Active
		frappe.db.set_value("Shift Assignment", row.name, "status", "Inactive")
