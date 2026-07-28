import frappe
from frappe import _

from employee_custom_attendance.attendance.shift_expiry import HRMS_EXPIRY_JOB_METHOD


def after_install():
	_seed_flexible_hours_shift_type()
	_disable_hrms_shift_expiry_job()
	frappe.db.commit()


def after_migrate():
	_disable_hrms_shift_expiry_job()


def _disable_hrms_shift_expiry_job():
	"""Stop HRMS's stock daily job in favour of our grace-period-aware replacement
	(employee_custom_attendance.attendance.shift_expiry). See that module for why.

	frappe.get_hooks sync on every migrate only touches frequency/cron_format on an
	existing Scheduled Job Type record, so this stays stopped across migrations -
	we still call it every migrate to cover a fresh site where the record didn't
	exist yet at the time hooks were first synced.
	"""
	if frappe.db.exists("Scheduled Job Type", {"method": HRMS_EXPIRY_JOB_METHOD}):
		frappe.db.set_value(
			"Scheduled Job Type", {"method": HRMS_EXPIRY_JOB_METHOD}, "stopped", 1
		)


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
