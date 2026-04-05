def on_checkin_validate(doc, method):
	"""Set skip_auto_attendance for flex employees.

	HRMS validate() runs fetch_shift() before doc_events["validate"] fires,
	so doc.shift is already populated when we are called.
	"""
	if doc.shift == "Flexible Hours":
		doc.skip_auto_attendance = 1
