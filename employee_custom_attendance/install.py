import frappe
from frappe import _


def after_install():
	_seed_flexible_hours_shift_type()
	frappe.db.commit()


def _seed_flexible_hours_shift_type():
	if frappe.db.exists("Shift Type", "Flexible Hours"):
		return

	doc = frappe.new_doc("Shift Type")
	doc.name = "Flexible Hours"
	doc.start_time = "00:00:00"
	doc.end_time = "23:59:00"
	doc.begin_check_in_before_shift_start_time = 0
	doc.allow_check_out_after_shift_end_time = 0
	doc.enable_auto_attendance = 0
	doc.working_hours_calculation_based_on = "Every Valid Check-in and Check-out"
	doc.determine_check_in_and_check_out = "Alternating entries as IN and OUT during the same shift"
	doc.working_hours_threshold_for_absent = 7
	doc.insert(ignore_permissions=True)
	frappe.msgprint(_("Shift Type 'Flexible Hours' created."), indicator="green")
