import frappe
from frappe.utils import today


def on_employee_update(doc, method):
	"""Auto-manage Shift Assignment when custom_attendance_system changes."""
	if doc.custom_attendance_system == "Flexible Hours":
		_ensure_flex_assignment(doc.name)
	else:
		_end_flex_assignment(doc.name)


def _ensure_flex_assignment(employee):
	"""Create an open-ended Flexible Hours Shift Assignment if none exists."""
	exists = frappe.db.exists(
		"Shift Assignment",
		{
			"employee": employee,
			"shift_type": "Flexible Hours",
			"docstatus": 1,
			"end_date": ["is", "not set"],
		},
	)
	if exists:
		return

	assignment = frappe.get_doc(
		{
			"doctype": "Shift Assignment",
			"employee": employee,
			"shift_type": "Flexible Hours",
			"start_date": today(),
			"status": "Active",
		}
	)
	assignment.insert(ignore_permissions=True)
	assignment.submit()


def _end_flex_assignment(employee):
	"""End-date any open Flexible Hours Shift Assignment."""
	open_assignments = frappe.get_all(
		"Shift Assignment",
		filters={
			"employee": employee,
			"shift_type": "Flexible Hours",
			"docstatus": 1,
			"end_date": ["is", "not set"],
		},
		pluck="name",
	)
	for name in open_assignments:
		frappe.db.set_value("Shift Assignment", name, "end_date", today())
