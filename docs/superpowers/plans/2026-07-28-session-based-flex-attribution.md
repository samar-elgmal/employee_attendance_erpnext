# Session-Based Flex Attendance Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `process_employee_for_date` so a Flexible Hours employee's working
hours are always attributed to the checkin's IN date — never silently merged
into a neighboring day because of checkin-count parity in a fixed fetch window.

**Architecture:** Replace window-truncate-then-position-pair with
dedupe → pair-into-sessions → filter-by-IN-date. A new `Session` namedtuple
becomes the unit the rest of the pipeline operates on. Two new
`Flexible Hours Settings` fields (`checkin_window_grace_hours`,
`max_session_hours`) replace hardcoded constants. A bounded single-punch
resync in `_build_sessions` keeps one stray checkin from discarding the rest
of a day's real sessions. No change to the scheduler entrypoint,
`reprocess_all`, or `reset_and_reprocess` signatures.

**Tech Stack:** Python 3, Frappe/HRMS framework, `FrappeTestCase`, `ruff`.

**Design doc:** `docs/superpowers/specs/2026-07-28-session-based-flex-attribution-design.md`

**Site for test commands:** `milaerp.milaserv.com` — all `bench` commands run
from `/home/frappe/frappe-bench`, not from `apps/`.

---

### Task 1: Add `checkin_window_grace_hours` and `max_session_hours` settings fields

**Files:**
- Modify: `employee_custom_attendance/employee_custom_attendance/doctype/flexible_hours_settings/flexible_hours_settings.json`
- Test: `employee_custom_attendance/employee_custom_attendance/doctype/flexible_hours_settings/test_flexible_hours_settings.py:7-11`

- [ ] **Step 1: Extend the existing defaults test to cover the two new fields**

Edit `test_flexible_hours_settings.py`, replacing the body of
`test_defaults_are_correct`:

```python
	def test_defaults_are_correct(self):
		settings = frappe.get_single("Flexible Hours Settings")
		self.assertEqual(settings.weekly_target_hours, 54)
		self.assertEqual(settings.daily_min_hours, 7)
		self.assertEqual(settings.daily_max_hours, 11)
		self.assertEqual(settings.checkin_window_grace_hours, 12)
		self.assertEqual(settings.max_session_hours, 20)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com run-tests --app employee_custom_attendance \
  --module employee_custom_attendance.employee_custom_attendance.doctype.flexible_hours_settings.test_flexible_hours_settings
```
Expected: FAIL — `AttributeError` (or `AssertionError` comparing `None` to
`12`) on `settings.checkin_window_grace_hours`.

- [ ] **Step 3: Add the two fields to the doctype JSON**

In `flexible_hours_settings.json`, update `field_order` (append the two new
fieldnames):

```json
  "field_order": [
    "weekly_target_hours",
    "daily_min_hours",
    "daily_max_hours",
    "penalty_type_for_shortfall",
    "job_run_time",
    "checkin_window_grace_hours",
    "max_session_hours"
  ],
```

Add the two field definitions to the `fields` array, after the
`job_run_time` field definition:

```json
    {
      "default": "12",
      "description": "How many hours past midnight of the day after a shift starts to keep fetching checkins for it (captures overnight check-outs). Correctness doesn't depend on this being tight — widen it only if a real shift legitimately runs later than this.",
      "fieldname": "checkin_window_grace_hours",
      "fieldtype": "Int",
      "label": "Checkin Window Grace Hours"
    },
    {
      "default": "20",
      "description": "Upper bound on a single IN/OUT pair's duration before it's treated as a pairing anomaly rather than a real session.",
      "fieldname": "max_session_hours",
      "fieldtype": "Float",
      "label": "Maximum Session Hours"
    }
```

- [ ] **Step 4: Apply the schema change**

Run:
```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com migrate
```
Expected: completes without error.

- [ ] **Step 5: Run test to verify it passes**

Run the same command as Step 2.
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
git add employee_custom_attendance/employee_custom_attendance/doctype/flexible_hours_settings/flexible_hours_settings.json \
  employee_custom_attendance/employee_custom_attendance/doctype/flexible_hours_settings/test_flexible_hours_settings.py
git commit -m "feat: add checkin_window_grace_hours and max_session_hours settings"
```

---

### Task 2: Add `_dedupe_checkins` (type-agnostic near-duplicate collapse)

**Files:**
- Modify: `employee_custom_attendance/attendance/daily_job.py:1-8` (imports), and a
  new block inserted directly above `_get_checkins` (currently `daily_job.py:97`)
- Test: `employee_custom_attendance/attendance/test_daily_job.py`

- [ ] **Step 1: Write the failing tests**

Add to the top of `test_daily_job.py`, after the existing imports (line 5),
add `timedelta` to the datetime import and a small log-row helper:

```python
from datetime import datetime, timedelta
```
(replaces the existing `from datetime import datetime` on line 1)

Then, after the `make_checkin` function (after line 108, before
`class TestDailyJobAttendance`), add:

```python
def _log(name, dt, log_type="IN"):
	"""A bare Employee Checkin-shaped row for testing pure pairing/dedup logic
	without touching the database."""
	return frappe._dict(name=name, time=dt, log_type=log_type)


class TestDedupeCheckins(FrappeTestCase):
	def test_collapses_duplicate_within_threshold(self):
		from employee_custom_attendance.attendance.daily_job import _dedupe_checkins

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [_log("a", base), _log("b", base + timedelta(seconds=30))]

		result = _dedupe_checkins(logs)

		self.assertEqual([entry.name for entry in result], ["a"])

	def test_keeps_checkins_beyond_threshold(self):
		from employee_custom_attendance.attendance.daily_job import _dedupe_checkins

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [_log("a", base), _log("b", base + timedelta(minutes=5))]

		result = _dedupe_checkins(logs)

		self.assertEqual([entry.name for entry in result], ["a", "b"])

	def test_collapses_regardless_of_log_type(self):
		from employee_custom_attendance.attendance.daily_job import _dedupe_checkins

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [_log("a", base, "IN"), _log("b", base + timedelta(seconds=30), "OUT")]

		result = _dedupe_checkins(logs)

		self.assertEqual([entry.name for entry in result], ["a"])

	def test_chains_against_last_kept_not_last_raw(self):
		from employee_custom_attendance.attendance.daily_job import _dedupe_checkins

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [
			_log("a", base),
			_log("b", base + timedelta(seconds=20)),
			_log("c", base + timedelta(seconds=40)),
		]

		result = _dedupe_checkins(logs)

		# "c" is 40s after "a" (the last KEPT entry, since "b" was dropped) —
		# still within the 60s threshold, so it's dropped too.
		self.assertEqual([entry.name for entry in result], ["a"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com run-tests --app employee_custom_attendance \
  --module employee_custom_attendance.attendance.test_daily_job
```
Expected: FAIL — `ImportError: cannot import name '_dedupe_checkins'`.

- [ ] **Step 3: Implement `_dedupe_checkins`**

In `daily_job.py`, update the import block (lines 1-8) to add `timedelta`:

```python
import calendar
from datetime import timedelta

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, today
from hrms.hr.doctype.employee_checkin.employee_checkin import (
	calculate_working_hours,
	time_diff_in_hours,
	update_attendance_in_checkins,
)
```

Then insert this directly above `def _get_checkins(employee, date):`
(currently line 97):

```python
DEDUP_THRESHOLD_SECONDS = 60


def _dedupe_checkins(logs, threshold_seconds=DEDUP_THRESHOLD_SECONDS):
	"""Collapse consecutive checkins within `threshold_seconds` of each other
	into one (keep the first). Doesn't check log_type — it isn't trustworthy on
	this system's devices (the same reason pairing below stays positional
	instead of type-based), so a type-agnostic gap check is the only reliable
	noise signal. Compares each entry to the last KEPT one, not the last raw
	one, so a run of 3+ near-simultaneous duplicates all collapse to the first.
	"""
	kept = []
	for log in logs:
		if kept and (log.time - kept[-1].time).total_seconds() <= threshold_seconds:
			continue
		kept.append(log)
	return kept
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same command as Step 2.
Expected: PASS (4 new tests, plus all existing tests in the module still pass).

- [ ] **Step 5: Commit**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
git add employee_custom_attendance/attendance/daily_job.py employee_custom_attendance/attendance/test_daily_job.py
git commit -m "feat: add type-agnostic checkin dedup"
```

---

### Task 3: Add `Session`, `_make_session`, and `_build_sessions` with bounded resync

**Files:**
- Modify: `employee_custom_attendance/attendance/daily_job.py` (new block after
  the `_dedupe_checkins` addition from Task 2, still above `_get_checkins`)
- Test: `employee_custom_attendance/attendance/test_daily_job.py`

This is the core algorithm change: positional pairing into `Session` objects,
with duration-plausibility as the only anomaly signal (log_type isn't
trustworthy) and a bounded single-punch resync instead of a hard stop.

- [ ] **Step 1: Write the failing tests**

Add to `test_daily_job.py`, after the `TestDedupeCheckins` class:

```python
class TestBuildSessions(FrappeTestCase):
	def test_pairs_plausible_sessions(self):
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [
			_log("a", base),
			_log("b", base + timedelta(hours=4)),
			_log("c", base + timedelta(hours=5)),
			_log("d", base + timedelta(hours=9)),
		]

		sessions, anomalies = _build_sessions(logs, "TEST-EMP")

		self.assertEqual(len(sessions), 2)
		self.assertAlmostEqual(sessions[0].duration, 4.0)
		self.assertAlmostEqual(sessions[1].duration, 4.0)
		self.assertEqual(anomalies, [])

	def test_trailing_unmatched_log_is_ignored(self):
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [
			_log("a", base),
			_log("b", base + timedelta(hours=4)),
			_log("c", base + timedelta(hours=5)),  # dangling IN, no OUT
		]

		sessions, anomalies = _build_sessions(logs, "TEST-EMP")

		self.assertEqual(len(sessions), 1)
		self.assertEqual(anomalies, [])

	def test_crosses_midnight_flag(self):
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		base = datetime(2026, 7, 1, 22, 0, 0)
		logs = [_log("a", base), _log("b", base + timedelta(hours=3))]

		sessions, _anomalies = _build_sessions(logs, "TEST-EMP")

		self.assertTrue(sessions[0].crosses_midnight)

	def test_same_day_session_does_not_cross_midnight(self):
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [_log("a", base), _log("b", base + timedelta(hours=4))]

		sessions, _anomalies = _build_sessions(logs, "TEST-EMP")

		self.assertFalse(sessions[0].crosses_midnight)

	def test_resync_recovers_after_stray_punch(self):
		"""A stray punch beyond dedup's 60s threshold (4 min later) implausibly
		pairs with the real IN. Resync should drop the stray, re-pair the real
		IN against the real OUT, and recover the rest of the day untouched —
		not the naive skip-and-continue-by-2 mis-pairing."""
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [
			_log("stray", base),
			_log("real_in", base + timedelta(minutes=4)),
			_log("out1", base + timedelta(hours=9)),
			_log("in2", base + timedelta(hours=12)),
			_log("out2", base + timedelta(hours=15)),
		]

		sessions, anomalies = _build_sessions(logs, "TEST-EMP")

		self.assertEqual(len(sessions), 2)
		self.assertEqual(sessions[0].in_log.name, "real_in")
		self.assertEqual(sessions[0].out_log.name, "out1")
		self.assertEqual(sessions[1].in_log.name, "in2")
		self.assertEqual(sessions[1].out_log.name, "out2")
		self.assertEqual(len(anomalies), 1)

	def test_resync_fails_on_genuinely_broken_data(self):
		"""Both the original pair and the resync retry are implausible —
		session-building stops rather than keep guessing."""
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [
			_log("a", base),
			_log("b", base + timedelta(minutes=2)),
			_log("c", base + timedelta(minutes=4)),
			_log("d", base + timedelta(hours=9)),
		]

		sessions, anomalies = _build_sessions(logs, "TEST-EMP")

		self.assertEqual(sessions, [])
		self.assertEqual(len(anomalies), 1)

	def test_sessions_before_anomaly_are_kept(self):
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [
			_log("good_in", base),
			_log("good_out", base + timedelta(hours=4)),
			_log("a", base + timedelta(hours=5)),
			_log("b", base + timedelta(hours=5, minutes=2)),
			_log("c", base + timedelta(hours=5, minutes=4)),
		]

		sessions, anomalies = _build_sessions(logs, "TEST-EMP")

		self.assertEqual(len(sessions), 1)
		self.assertEqual(sessions[0].in_log.name, "good_in")
		self.assertEqual(len(anomalies), 1)

	def test_logs_error_on_anomaly(self):
		from employee_custom_attendance.attendance.daily_job import _build_sessions

		frappe.db.delete("Error Log")
		base = datetime(2026, 7, 1, 8, 0, 0)
		logs = [_log("a", base), _log("b", base + timedelta(minutes=2))]

		_build_sessions(logs, "TEST-EMP-999")

		self.assertTrue(
			frappe.db.exists("Error Log", {"method": ["like", "%TEST-EMP-999%"]})
		)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com run-tests --app employee_custom_attendance \
  --module employee_custom_attendance.attendance.test_daily_job
```
Expected: FAIL — `ImportError: cannot import name '_build_sessions'`.

- [ ] **Step 3: Implement `Session`, `_make_session`, `_build_sessions`**

Add `from collections import namedtuple` to the top of `daily_job.py` (with
the other stdlib imports, alongside `from datetime import timedelta`):

```python
import calendar
from collections import namedtuple
from datetime import timedelta
```

Insert this block directly after the `_dedupe_checkins` function added in
Task 2 (still above `_get_checkins`):

```python
Session = namedtuple("Session", ["in_log", "out_log", "duration", "attendance_date", "crosses_midnight"])

MIN_SESSION_HOURS = 5 / 60  # 5 minutes — anomaly signal only, not a hard rule


def _make_session(in_log, out_log, duration):
	return Session(
		in_log=in_log,
		out_log=out_log,
		duration=duration,
		attendance_date=getdate(in_log.time),
		crosses_midnight=getdate(out_log.time) != getdate(in_log.time),
	)


def _is_plausible_session(duration, max_session_hours):
	return MIN_SESSION_HOURS <= duration <= max_session_hours


def _build_sessions(logs, employee):
	"""Pair deduped, chronological checkins into Sessions. log_type isn't
	trustworthy on this system's devices, so pairing stays positional (the
	same alternating assumption HRMS's own "Alternating entries" mode uses)
	— the only anomaly signal available is whether a pair's duration is
	plausible for a single real session.

	On an implausible pair, don't just drop it and resume two positions
	later — that silently shifts every later pair by one position and can
	fabricate a plausible-looking but wrong session. Instead, assume the
	first log of the pair was a stray extra punch dedup didn't catch, and
	retry pairing the second log against the next one. If that's also
	implausible, this isn't one stray punch — stop rather than keep
	guessing; sessions already built are kept, everything from this point
	is left unclaimed for manual review / reset_and_reprocess.
	"""
	max_session_hours = flt(frappe.get_single("Flexible Hours Settings").max_session_hours)
	sessions = []
	anomalies = []
	i = 0
	while i + 1 < len(logs):
		in_log, out_log = logs[i], logs[i + 1]
		duration = time_diff_in_hours(in_log.time, out_log.time)

		if _is_plausible_session(duration, max_session_hours):
			sessions.append(_make_session(in_log, out_log, duration))
			i += 2
			continue

		anomalies.append((in_log, out_log, duration))

		if i + 2 < len(logs):
			retry_out = logs[i + 2]
			retry_duration = time_diff_in_hours(out_log.time, retry_out.time)
			if _is_plausible_session(retry_duration, max_session_hours):
				sessions.append(_make_session(out_log, retry_out, retry_duration))
				i += 3
				continue

		break

	if anomalies:
		detail = "\n".join(
			f"{a[0].time} -> {a[1].time} ({a[2]:.2f}h) outside plausible session range "
			f"({MIN_SESSION_HOURS * 60:.0f}min - {max_session_hours}h)"
			for a in anomalies
		)
		frappe.log_error(
			title=f"Flex attendance pairing anomaly: {employee}",
			message=detail,
		)

	return sessions, anomalies
```

Note: `frappe.log_error` is called with explicit `title=`/`message=` keyword
arguments rather than positionally. The positional form has an
auto-swap heuristic keyed on whether the first argument contains a newline
(see `frappe/utils/error.py`), which is what the rest of this file's
existing `frappe.log_error(frappe.get_traceback(), f"...")` call sites rely
on — that's safe there because a traceback always contains newlines. Our
`detail` string does *not* always contain a newline (a single anomaly is one
line), so the heuristic would misfire and swap `method`/`error` on the
stored Error Log. Explicit keywords sidestep that.

- [ ] **Step 4: Run tests to verify they pass**

Run the same command as Step 2.
Expected: PASS (8 new tests, plus all existing tests in the module still pass).

- [ ] **Step 5: Commit**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
git add employee_custom_attendance/attendance/daily_job.py employee_custom_attendance/attendance/test_daily_job.py
git commit -m "feat: add Session pairing with bounded anomaly resync"
```

---

### Task 4: Shared `_get_window_bounds` and widened `_get_checkins`

**Files:**
- Modify: `employee_custom_attendance/attendance/daily_job.py:97-113` (`_get_checkins`)
- Test: `employee_custom_attendance/attendance/test_daily_job.py`

- [ ] **Step 1: Write the failing test**

A test driven through `process_employee_for_date` at the *default* grace
value (12h) can't actually distinguish old from new behavior here — the
default reproduces the old noon cutoff exactly, so it would pass unchanged
either way and wouldn't be a real red step. Instead, unit-test
`_get_window_bounds` directly with a non-default setting value, which the
*old* code structurally cannot respond to at all (it doesn't read the
setting yet).

Add to `test_daily_job.py`, after the `TestBuildSessions` class:

```python
class TestGetWindowBounds(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_respects_checkin_window_grace_hours_setting(self):
		from employee_custom_attendance.attendance.daily_job import _get_window_bounds

		settings = frappe.get_single("Flexible Hours Settings")
		settings.checkin_window_grace_hours = 3
		settings.save()

		date = getdate(today())
		day_start, day_end = _get_window_bounds(date)

		self.assertEqual(day_start, datetime.combine(date, datetime.min.time()))
		self.assertEqual(
			day_end,
			datetime.combine(add_days(date, 1), datetime.min.time()) + timedelta(hours=3),
		)

	def test_default_grace_hours_matches_old_noon_cutoff(self):
		from employee_custom_attendance.attendance.daily_job import _get_window_bounds

		date = getdate(today())
		_day_start, day_end = _get_window_bounds(date)

		self.assertEqual(
			day_end,
			datetime.combine(add_days(date, 1), datetime.min.time()) + timedelta(hours=12),
		)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com run-tests --app employee_custom_attendance \
  --module employee_custom_attendance.attendance.test_daily_job
```
Expected: FAIL — `ImportError: cannot import name '_get_window_bounds'`.

- [ ] **Step 3: Add `_get_window_bounds` and rewrite `_get_checkins`**

Replace `_get_checkins` (`daily_job.py:97-113`):

```python
def _get_checkins(employee, date):
	# window is: date 00:00 -> (date+1) + checkin_window_grace_hours.
	# Pairing (see _build_sessions) happens before date-attribution, so this
	# doesn't need to be tight for correctness — it only bounds how much data
	# a single run reads. Deduped immediately so downstream pairing never
	# sees near-duplicate noise.
	day_start, day_end = _get_window_bounds(date)

	logs = frappe.get_all(
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
	return _dedupe_checkins(logs)
```

Add `_get_window_bounds` directly above it:

```python
def _get_window_bounds(date):
	"""Checkin fetch/re-arm window for `date`: midnight through
	(date+1) + checkin_window_grace_hours. Shared by _get_checkins (what to
	fetch) and _reset_attendance (what to re-arm skip_auto_attendance on) so
	the two can't drift out of sync.
	"""
	grace_hours = flt(frappe.get_single("Flexible Hours Settings").checkin_window_grace_hours)
	day_start = frappe.utils.get_datetime(str(date) + " 00:00:00")
	day_end = frappe.utils.get_datetime(str(add_days(date, 1)) + " 00:00:00") + timedelta(hours=grace_hours)
	return day_start, day_end
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same command as Step 2.
Expected: PASS — including the pre-existing
`test_holiday_not_marked_absent_by_next_day_checkin_bleed` (its checkin is at
`(date+1) 09:00`, still inside the default 12h-grace window, so behavior is
unchanged).

- [ ] **Step 5: Commit**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
git add employee_custom_attendance/attendance/daily_job.py employee_custom_attendance/attendance/test_daily_job.py
git commit -m "feat: make checkin window bound configurable via settings"
```

---

### Task 5: Wire `_get_window_bounds` into `_reset_attendance`

**Files:**
- Modify: `employee_custom_attendance/attendance/daily_job.py:341-363` (`_reset_attendance`)
- Test: `employee_custom_attendance/attendance/test_daily_job.py`

Today `_reset_attendance` independently rebuilds the day window with the old
hardcoded noon cutoff to re-arm `skip_auto_attendance`. Left alone, it would
now diverge from `_get_checkins`'s new settings-driven window.

- [ ] **Step 1: Write the failing test**

At the default `checkin_window_grace_hours` (12), old and new
`_reset_attendance` windows agree, so — same problem as Task 4 — a
default-value test wouldn't actually be red first. Configure a narrower
window so old (hardcoded noon) and new (setting-driven) genuinely disagree
on one checkin's fate.

Add to `test_daily_job.py`, inside `TestDailyJobAttendance`:

```python
	def test_reset_attendance_uses_configured_window_not_hardcoded_noon(self):
		from employee_custom_attendance.attendance.daily_job import _reset_attendance

		emp = make_flex_employee()
		settings = frappe.get_single("Flexible Hours Settings")
		settings.checkin_window_grace_hours = 1
		settings.save()

		date = getdate(today())
		next_day_base = datetime.combine(add_days(date, 1), datetime.min.time())
		# 05:00 next day: inside the OLD hardcoded noon cutoff, but outside the
		# 1-hour grace window configured above (day_end = (date+1) 01:00).
		outside_checkin = make_checkin(emp.name, next_day_base.replace(hour=5), "IN")
		frappe.db.set_value("Employee Checkin", outside_checkin.name, "skip_auto_attendance", 0)

		_reset_attendance(emp.name, date)

		skip_flag = frappe.db.get_value(
			"Employee Checkin", outside_checkin.name, "skip_auto_attendance"
		)
		self.assertEqual(skip_flag, 0)  # outside the configured window — left alone

	def test_reset_then_reprocess_recreates_same_attendance(self):
		from employee_custom_attendance.attendance.daily_job import _reset_attendance, process_employee_for_date

		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())
		make_checkin(emp.name, base.replace(hour=9), "IN")
		make_checkin(emp.name, base.replace(hour=17), "OUT")

		process_employee_for_date(emp.name, date)
		_reset_attendance(emp.name, date)
		self.assertFalse(
			frappe.db.exists("Attendance", {"employee": emp.name, "attendance_date": date})
		)

		process_employee_for_date(emp.name, date)
		status = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": date}, "status"
		)
		self.assertEqual(status, "Present")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com run-tests --app employee_custom_attendance \
  --module employee_custom_attendance.attendance.test_daily_job
```
Expected: `test_reset_attendance_uses_configured_window_not_hardcoded_noon`
FAILS — the old hardcoded-noon `_reset_attendance` window includes
`(date+1) 05:00` regardless of the setting, so it flips `skip_auto_attendance`
back to `1`, and the assertion (expecting it to stay `0`) fails.
`test_reset_then_reprocess_recreates_same_attendance` may already pass
(both windows agree on this input) — that's fine, it's regression coverage
for Step 3, not the primary red case.

- [ ] **Step 3: Update `_reset_attendance` to use `_get_window_bounds`**

Replace lines 355-357 in `_reset_attendance` (the "Also ensure
skip_auto_attendance=1..." block):

```python
	# Also ensure skip_auto_attendance=1 on any unlinked checkins in the day window.
	day_start = frappe.utils.get_datetime(str(date) + " 00:00:00")
	day_end = frappe.utils.get_datetime(str(add_days(date, 1)) + " 12:00:00")
```

with:

```python
	# Also ensure skip_auto_attendance=1 on any unlinked checkins in the day window.
	day_start, day_end = _get_window_bounds(date)
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same command as Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
git add employee_custom_attendance/attendance/daily_job.py employee_custom_attendance/attendance/test_daily_job.py
git commit -m "fix: share window-bound computation between _get_checkins and _reset_attendance"
```

---

### Task 6: Rewire `process_employee_for_date` to use Sessions (core cutover)

**Files:**
- Modify: `employee_custom_attendance/attendance/daily_job.py:45-94` (`process_employee_for_date`)
- Test: `employee_custom_attendance/attendance/test_daily_job.py`

This is the change that actually fixes the cross-day contamination bug: hours
are now attributed by session IN-date instead of window-position parity.

- [ ] **Step 1: Write the failing tests**

Add to `test_daily_job.py`, inside `TestDailyJobAttendance`:

```python
	def test_next_day_early_session_does_not_contaminate_today(self):
		"""The bug this whole change fixes: a same-day session plus a next-day
		early session that both fall inside the fetch window, with a checkin
		count that happens to be even, must NOT be merged into one total."""
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date

		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())
		next_day_base = datetime.combine(add_days(date, 1), datetime.min.time())

		make_checkin(emp.name, base.replace(hour=9), "IN")
		make_checkin(emp.name, base.replace(hour=17), "OUT")  # today: real 8h session
		make_checkin(emp.name, next_day_base.replace(hour=5), "IN")
		make_checkin(emp.name, next_day_base.replace(hour=7), "OUT")  # tomorrow: real 2h session

		process_employee_for_date(emp.name, date)

		today_hours = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": date}, "working_hours"
		)
		self.assertAlmostEqual(today_hours, 8.0, places=1)

		process_employee_for_date(emp.name, getdate(add_days(date, 1)))

		tomorrow = frappe.db.get_value(
			"Attendance",
			{"employee": emp.name, "attendance_date": getdate(add_days(date, 1))},
			["status", "working_hours"],
			as_dict=True,
		)
		self.assertEqual(tomorrow.status, "Present")
		self.assertAlmostEqual(tomorrow.working_hours, 2.0, places=1)

	def test_multiple_overnight_sessions_in_a_row(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date

		emp = make_flex_employee()
		date = getdate(today())
		next_day = add_days(date, 1)
		base = datetime.combine(date, datetime.min.time())
		next_base = datetime.combine(next_day, datetime.min.time())

		make_checkin(emp.name, base.replace(hour=22), "IN")
		make_checkin(emp.name, next_base.replace(hour=1), "OUT")  # 1/7 22:00 -> 2/7 01:00 = 3h
		make_checkin(emp.name, next_base.replace(hour=10), "IN")
		make_checkin(emp.name, next_base.replace(hour=12), "OUT")  # 2/7 10:00 -> 12:00 = 2h
		make_checkin(emp.name, next_base.replace(hour=15), "IN")
		make_checkin(emp.name, next_base.replace(hour=18), "OUT")  # 2/7 15:00 -> 18:00 = 3h

		process_employee_for_date(emp.name, date)
		day1_hours = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": date}, "working_hours"
		)
		self.assertAlmostEqual(day1_hours, 3.0, places=1)

		process_employee_for_date(emp.name, getdate(next_day))
		day2_hours = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": getdate(next_day)}, "working_hours"
		)
		self.assertAlmostEqual(day2_hours, 5.0, places=1)  # 2h + 3h

	def test_same_day_multiple_sessions_plus_trailing_overnight(self):
		from employee_custom_attendance.attendance.daily_job import process_employee_for_date

		emp = make_flex_employee()
		date = getdate(today())
		base = datetime.combine(date, datetime.min.time())
		next_base = datetime.combine(add_days(date, 1), datetime.min.time())

		make_checkin(emp.name, base.replace(hour=8), "IN")
		make_checkin(emp.name, base.replace(hour=12), "OUT")  # 4h
		make_checkin(emp.name, base.replace(hour=13), "IN")
		make_checkin(emp.name, base.replace(hour=18), "OUT")  # 5h
		make_checkin(emp.name, base.replace(hour=19), "IN")
		make_checkin(emp.name, next_base.replace(hour=1), "OUT")  # 6h, overnight

		process_employee_for_date(emp.name, date)

		working_hours = frappe.db.get_value(
			"Attendance", {"employee": emp.name, "attendance_date": date}, "working_hours"
		)
		self.assertAlmostEqual(working_hours, 15.0, places=1)  # capped? default max is 11 -> check cap
```

The last test's final assertion needs a moment of care: `daily_max_hours`
defaults to 11, and this scenario totals 15 real hours, so the *stored*
`working_hours` will be capped at 11, not 15. Fix the assertion before
running:

```python
		self.assertAlmostEqual(working_hours, 11.0, places=1)  # 15 real hours, capped at daily_max_hours
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com run-tests --app employee_custom_attendance \
  --module employee_custom_attendance.attendance.test_daily_job
```
Expected: FAIL — `test_next_day_early_session_does_not_contaminate_today` and
`test_multiple_overnight_sessions_in_a_row` fail against the current
positional-pairing implementation (contamination bug); the third may or may
not already pass depending on count parity, but must pass after Step 3
regardless.

- [ ] **Step 3: Rewrite `process_employee_for_date`**

Replace `daily_job.py:45-94`:

```python
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
	raw_logs = _get_checkins(employee, date)

	if not raw_logs:
		_mark_absent_or_skip_holiday(employee, date, settings, is_holiday)
		return

	sessions, _anomalies = _build_sessions(raw_logs, employee)  # anomalies already logged inside
	day_sessions = [s for s in sessions if s.attendance_date == date]

	if not day_sessions:
		# Every fetched checkin belonged to a session attributed to a
		# different date (most commonly an overnight-window bleed from the
		# next day), or no session could be built at all — not a real,
		# pairable shift on `date` itself.
		_mark_absent_or_skip_holiday(employee, date, settings, is_holiday)
		return

	total_hours = sum(s.duration for s in day_sessions)
	in_time = day_sessions[0].in_log.time
	out_time = day_sessions[-1].out_log.time
	effective_hours = min(flt(total_hours), flt(settings.daily_max_hours))
	status = "Present" if effective_hours >= flt(settings.daily_min_hours) else "Absent"

	attendance = _create_attendance(employee, date, status, effective_hours, in_time, out_time)
	claimed = [log.name for s in day_sessions for log in (s.in_log, s.out_log)]
	update_attendance_in_checkins(claimed, attendance.name)

	if status == "Absent" and settings.penalty_type_for_shortfall:
		_create_shortfall_penalty(employee, date, effective_hours, settings)
```

Remove the now-unused `calculate_working_hours` import from the top of the
file (`daily_job.py`'s import block, added originally in Task 2's edit):

```python
from hrms.hr.doctype.employee_checkin.employee_checkin import (
	time_diff_in_hours,
	update_attendance_in_checkins,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same command as Step 2.
Expected: PASS — all new tests, and every pre-existing test in
`test_daily_job.py` (`test_present_when_hours_meet_minimum`,
`test_absent_when_hours_below_minimum`, `test_hours_capped_at_daily_max`,
`test_multiple_intervals_summed`, `test_idempotent_second_run`,
`test_holiday_not_marked_absent_by_next_day_checkin_bleed`,
`test_approved_leave_creates_on_leave_attendance`,
`test_backfill_attendance_requests_creates_attendance`,
`test_missing_fingerprint_marks_half_day_after_3`).

- [ ] **Step 5: Commit**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
git add employee_custom_attendance/attendance/daily_job.py employee_custom_attendance/attendance/test_daily_job.py
git commit -m "fix: attribute flex hours by session IN-date instead of window position"
```

---

### Task 7: Update `ARCHITECTURE.md`

**Files:**
- Modify: `ARCHITECTURE.md`

No test — documentation only.

- [ ] **Step 1: Update Assumption #4**

Find (in the "Assumptions" section):

```
4. **A flex shift never runs past noon the day after it starts.** The checkin
   window is hardcoded to `date 00:00` → `(date+1) 12:00`. A shift that
   legitimately checks out after noon the next day is silently excluded.
```

Replace with:

```
4. **A flex shift never runs past `(date+1) + checkin_window_grace_hours`.**
   The checkin window's end bound now reads
   `Flexible Hours Settings.checkin_window_grace_hours` (default 12, i.e. the
   same effective noon cutoff as before). Unlike before, correctness no
   longer depends on this bound being tight — pairing happens before
   date-attribution (see `_build_sessions` / "Flex Attendance Daily
   Pipeline" below), so a wider window can never merge a next-day session
   into today's total by coincidence. The setting only bounds how much data
   a single run reads; widen it if a real shift legitimately needs more room.
```

- [ ] **Step 2: Update Assumption #3**

Find:

```
3. **Checkins for a shift strictly alternate IN/OUT.** `calculate_working_hours` is
   called with `"Alternating entries as IN and OUT"` — a double punch of the same
   type (e.g. two INs in a row from a flaky device) desyncs pairing for the rest of
   the day, not just that pair.
```

Replace with:

```
3. **Checkins for a shift strictly alternate IN/OUT.** `log_type` isn't
   trustworthy on this system's devices, so `_build_sessions` pairs
   positionally, the same alternating assumption HRMS's own "Alternating
   entries" mode uses. Two layers soften the failure mode:
   `_dedupe_checkins` collapses any two checkins within 60 seconds
   regardless of type (device double-scan noise), and `_build_sessions`'s
   bounded resync recovers from a single stray punch beyond that threshold
   by dropping it and re-pairing the next log. Only a genuine multi-punch
   desync (the resync retry is *also* implausible) still stops
   session-building at that point — sessions already built are kept, the
   rest of that window's checkins are left unclaimed for manual review.
```

- [ ] **Step 3: Update the "Flex Attendance Daily Pipeline" section**

Find the `_get_checkins` bullet under "Flex Attendance Daily Pipeline
(`daily_job.py`)":

```
- **`_get_checkins`** — window is `date 00:00` → `(date+1) 12:00` to capture
  overnight shifts, filtered to `skip_auto_attendance=1` and `attendance: not set`
  so already-claimed checkins aren't double-counted on a later run.
```

Replace with:

```
- **`_get_checkins`** — window is `date 00:00` → `(date+1) +
  checkin_window_grace_hours` (default 12h, i.e. noon) to capture overnight
  shifts, filtered to `skip_auto_attendance=1` and `attendance: not set` so
  already-claimed checkins aren't double-counted on a later run, then
  deduped via `_dedupe_checkins` (collapses near-duplicate device noise
  within 60 seconds, regardless of `log_type`).
- **`_build_sessions`** — pairs the deduped, chronological checkins
  positionally into `Session` objects (`in_log`, `out_log`, `duration`,
  `attendance_date`, `crosses_midnight`). A pair with an implausible
  duration (outside `MIN_SESSION_HOURS`–`max_session_hours`) triggers a
  bounded single-punch resync rather than aborting the rest of the window
  (see Assumption #3). `process_employee_for_date` then keeps only the
  sessions whose `attendance_date` matches the date being processed —
  sessions belonging to a different date (most commonly a next-day session
  that fell inside the fetch window) are left unclaimed for that date's own
  run, which is what prevents the cross-day contamination the old
  window-position-based pairing was vulnerable to.
```

- [ ] **Step 4: Update the Failure Scenarios table**

Find the row:

```
| Overnight shift (e.g. 20:00 IN → 02:00 OUT next day) | Checkin window extends to noon next day so the OUT is captured under the shift's start date; once linked, the `attendance: not set` filter excludes it from bleeding into next day's window too | `_get_checkins` |
```

Replace with:

```
| Overnight shift (e.g. 20:00 IN → 02:00 OUT next day) | Checkin window extends past midnight (`checkin_window_grace_hours`) so the OUT is captured; the session is attributed by its IN date regardless of window position, and once linked, the `attendance: not set` filter excludes it from bleeding into next day's window too | `_get_checkins`, `_build_sessions` |
| Next-day early session falls inside today's fetch window | Never merged into today's total, regardless of checkin count parity — filtered out by session IN-date after pairing, left unclaimed for its own day's run | `_build_sessions`, `process_employee_for_date` |
| Device double-scan within 60 seconds | Collapsed by `_dedupe_checkins` before pairing, regardless of `log_type` | `_dedupe_checkins` |
| Single stray punch beyond dedup's threshold | Bounded resync in `_build_sessions` drops the presumed stray and retries against the next log — recovers the rest of the window's sessions correctly in the common case; anomaly logged via `frappe.log_error` either way | `_build_sessions` |
| Pairing desync the resync can't recover (retry also implausible) | Session-building stops at that point; sessions before it are kept, everything from the anomaly onward is left unclaimed for manual review / `reset_and_reprocess` | `_build_sessions` |
```

- [ ] **Step 5: Commit**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
git add ARCHITECTURE.md
git commit -m "docs: update ARCHITECTURE.md for session-based flex attribution"
```

---

### Task 8: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full app test suite**

```bash
cd /home/frappe/frappe-bench
bench --site milaerp.milaserv.com run-tests --app employee_custom_attendance
```
Expected: all tests pass, no failures or errors anywhere in the app (not
just `test_daily_job.py` — confirms nothing else in the app imported
`calculate_working_hours` or relied on the old window constants).

- [ ] **Step 2: Run lint**

```bash
cd /home/frappe/frappe-bench/apps/employee_custom_attendance
pre-commit run --all-files
```
Expected: passes, or auto-fixes formatting with no remaining errors. If
`pre-commit` reformats files, re-stage and amend as a follow-up commit (per
this repo's normal workflow — do not bypass with `--no-verify`).

- [ ] **Step 3: Confirm no historical data cleanup was silently skipped**

This is a reminder, not a code step: per the design doc's "Rollout"
section, after this deploys, run `reprocess_all` over any historical date
range suspected of the old cross-day contamination bug:

```bash
bench --site milaerp.milaserv.com execute \
  employee_custom_attendance.attendance.daily_job.reprocess_all \
  --args "['<from_date>', '<to_date>']"
```

This is an operational step for whoever deploys this change, not part of
the automated test suite — flag it in the PR description.
