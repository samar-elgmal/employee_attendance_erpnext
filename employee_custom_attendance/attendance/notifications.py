import frappe
from frappe.utils import flt, formatdate, get_first_day, get_last_day


def _get_hr_recipients():
	"""Return email addresses of all HR Manager and HR User role holders."""
	users = frappe.get_all(
		"Has Role",
		filters={"role": ["in", ["HR Manager", "HR User"]], "parenttype": "User"},
		pluck="parent",
	)
	emails = []
	for user in set(users):
		email = frappe.db.get_value("User", user, "email")
		if email:
			emails.append(email)
	return list(set(emails))


def notify_lateness_cap(employee, date):
	"""Send one-time email to HR when employee's total lateness exceeds 5h this month."""
	month_start = get_first_day(date)
	month_end = get_last_day(date)

	# Idempotency: check if already notified this month
	notified_month = frappe.db.get_value("Employee", employee, "custom_lateness_cap_notified")
	current_month = str(month_start)
	if notified_month == current_month:
		return

	total_lateness_hours = _get_total_lateness_hours(employee, month_start, month_end)
	if total_lateness_hours <= 5:
		return

	employee_doc = frappe.get_doc("Employee", employee)
	recipients = _get_hr_recipients()
	if not recipients:
		return

	month_label = formatdate(month_start, "MMMM yyyy")
	frappe.sendmail(
		recipients=recipients,
		subject=f"[HR Alert] Lateness Cap Reached — {employee_doc.employee_name} — {month_label}",
		message=(
			f"<p>Employee <strong>{employee_doc.employee_name}</strong> ({employee})"
			f" has exceeded 5 hours of lateness in {month_label}.</p>"
			f"<p>Current total lateness: <strong>{total_lateness_hours:.1f} hours</strong></p>"
			f"<p>Per policy, deductions will be doubled for the remainder of the month.</p>"
			f"<p>Action required: Review and ensure salary deduction is applied correctly.</p>"
		),
	)

	# Mark as notified
	frappe.db.set_value("Employee", employee, "custom_lateness_cap_notified", current_month)


def _get_total_lateness_hours(employee, month_start, month_end):
	"""Calculate total lateness hours for the month.

	For Flexible Hours employees: sum shortfall (daily_min - working_hours)
	for days where working_hours < daily_min.
	For Standard employees: sum late minutes from late_entry days.
	"""
	attendance_system = frappe.db.get_value("Employee", employee, "custom_attendance_system")

	if attendance_system == "Flexible Hours":
		settings = frappe.get_single("Flexible Hours Settings")
		daily_min = flt(settings.daily_min_hours)

		records = frappe.get_all(
			"Attendance",
			filters={
				"employee": employee,
				"attendance_date": ["between", [month_start, month_end]],
				"docstatus": 1,
				"working_hours": ["<", daily_min],
				"status": ["in", ["Absent", "Half Day"]],
			},
			fields=["working_hours"],
		)
		return sum(daily_min - flt(r.working_hours) for r in records)

	else:
		# Standard: sum late minutes from late_entry attendance
		records = frappe.db.sql(
			"""
			SELECT a.in_time, st.start_time
			FROM `tabAttendance` a
			JOIN `tabShift Type` st ON st.name = a.shift
			WHERE a.employee = %s
			  AND a.attendance_date BETWEEN %s AND %s
			  AND a.late_entry = 1
			  AND a.docstatus = 1
			  AND a.in_time IS NOT NULL
			""",
			(employee, month_start, month_end),
			as_dict=True,
		)
		total_minutes = 0
		for r in records:
			if r.in_time and r.start_time:
				from frappe.utils import get_datetime

				shift_start = get_datetime(str(month_start) + " " + str(r.start_time))
				late_minutes = (get_datetime(r.in_time) - shift_start).total_seconds() / 60
				if late_minutes > 0:
					total_minutes += late_minutes
		return total_minutes / 60


def notify_article_69(employee, date):
	"""Send email to HR if employee hits Article 69 absence threshold."""
	from frappe.utils import add_months

	employee_doc = frappe.get_doc("Employee", employee)
	twelve_months_ago = add_months(date, -12)

	# Case A: 20 non-consecutive absent days in rolling 12 months
	absent_count = frappe.db.count(
		"Attendance",
		{
			"employee": employee,
			"status": "Absent",
			"attendance_date": ["between", [twelve_months_ago, date]],
			"docstatus": 1,
		},
	)

	# Case B: consecutive absence streak
	streak = _get_current_absence_streak(employee, date)

	reason = None
	if absent_count >= 20:
		reason = f"{absent_count} non-consecutive days absent in the last 12 months"
	elif streak >= 10:
		reason = f"{streak} consecutive days absent"

	if not reason:
		return

	recipients = _get_hr_recipients()
	if not recipients:
		return

	frappe.sendmail(
		recipients=recipients,
		subject=f"[HR Alert] Absence Threshold Reached — {employee_doc.employee_name}",
		message=(
			f"<p>Employee <strong>{employee_doc.employee_name}</strong> ({employee})"
			f" has reached an absence threshold that may constitute voluntary resignation"
			f" under Article 69 of Egyptian Labour Law.</p>"
			f"<p>Reason: <strong>{reason}</strong></p>"
			f"<p>Last checked date: {formatdate(date)}</p>"
			f"<p>Action required: Investigate immediately and follow HR procedures.</p>"
		),
	)


def _get_current_absence_streak(employee, date):
	"""Return the number of consecutive absent days ending on or before date."""
	from frappe.utils import add_days

	streak = 0
	check_date = date
	for _ in range(30):  # check at most 30 days back
		status = frappe.db.get_value(
			"Attendance",
			{"employee": employee, "attendance_date": check_date, "docstatus": 1},
			"status",
		)
		if status == "Absent":
			streak += 1
			check_date = add_days(check_date, -1)
		else:
			break
	return streak
