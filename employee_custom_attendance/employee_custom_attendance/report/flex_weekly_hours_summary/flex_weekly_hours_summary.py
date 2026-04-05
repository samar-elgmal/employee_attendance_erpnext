import frappe
from frappe.utils import getdate, add_days, flt
from collections import defaultdict


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{"label": "Employee", "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 120},
		{"label": "Employee Name", "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
		{"label": "Department", "fieldname": "department", "fieldtype": "Data", "width": 140},
		{"label": "Week Start", "fieldname": "week_start", "fieldtype": "Date", "width": 110},
		{"label": "Total Hours", "fieldname": "total_hours", "fieldtype": "Float", "width": 100},
		{"label": "Target Hours", "fieldname": "target_hours", "fieldtype": "Float", "width": 100},
		{"label": "Shortfall Hours", "fieldname": "shortfall_hours", "fieldtype": "Float", "width": 110},
		{"label": "Days Below Min", "fieldname": "days_below_min", "fieldtype": "Int", "width": 120},
	]


def get_data(filters):
	settings = frappe.get_single("Flexible Hours Settings")
	target = flt(settings.weekly_target_hours)
	daily_min = flt(settings.daily_min_hours)

	from_date = getdate(filters.get("from_date") or frappe.utils.add_days(frappe.utils.today(), -7))
	to_date = getdate(filters.get("to_date") or frappe.utils.today())

	employee_filters = {"custom_attendance_system": "Flexible Hours", "status": "Active"}
	if filters.get("employee"):
		employee_filters["name"] = filters["employee"]
	if filters.get("department"):
		employee_filters["department"] = filters["department"]

	employees = frappe.get_all(
		"Employee",
		filters=employee_filters,
		fields=["name", "employee_name", "department"],
	)

	if not employees:
		return []

	attendance = frappe.get_all(
		"Attendance",
		filters={
			"employee": ["in", [e.name for e in employees]],
			"attendance_date": ["between", [from_date, to_date]],
			"shift": "Flexible Hours",
			"docstatus": 1,
		},
		fields=["employee", "attendance_date", "working_hours"],
	)

	buckets = defaultdict(lambda: {"total_hours": 0.0, "days_below_min": 0})
	for row in attendance:
		week_start = _get_week_start(getdate(row.attendance_date))
		key = (row.employee, week_start)
		buckets[key]["total_hours"] += flt(row.working_hours)
		if flt(row.working_hours) < daily_min:
			buckets[key]["days_below_min"] += 1

	emp_map = {e.name: e for e in employees}
	result = []
	for (employee, week_start), vals in sorted(buckets.items(), key=lambda x: (x[0][0], x[0][1])):
		emp = emp_map.get(employee)
		shortfall = max(target - vals["total_hours"], 0)
		result.append({
			"employee": employee,
			"employee_name": emp.employee_name if emp else "",
			"department": emp.department if emp else "",
			"week_start": week_start,
			"total_hours": round(vals["total_hours"], 2),
			"target_hours": target,
			"shortfall_hours": round(shortfall, 2),
			"days_below_min": vals["days_below_min"],
		})

	return result


def _get_week_start(date):
	"""Return the Monday of the week containing date."""
	return add_days(date, -date.weekday())
