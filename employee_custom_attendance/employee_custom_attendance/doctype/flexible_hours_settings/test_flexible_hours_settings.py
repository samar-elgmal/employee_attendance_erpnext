# test_flexible_hours_settings.py
import frappe
from frappe.tests.utils import FrappeTestCase


class TestFlexibleHoursSettings(FrappeTestCase):
	def test_defaults_are_correct(self):
		settings = frappe.get_single("Flexible Hours Settings")
		self.assertEqual(settings.weekly_target_hours, 54)
		self.assertEqual(settings.daily_min_hours, 7)
		self.assertEqual(settings.daily_max_hours, 11)

	def test_can_save_settings(self):
		settings = frappe.get_single("Flexible Hours Settings")
		try:
			settings.weekly_target_hours = 48
			settings.save()
			reloaded = frappe.get_single("Flexible Hours Settings")
			self.assertEqual(reloaded.weekly_target_hours, 48)
		finally:
			settings.weekly_target_hours = 54
			settings.save()
