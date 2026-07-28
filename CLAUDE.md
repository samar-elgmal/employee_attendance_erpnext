# CLAUDE.md — employee_custom_attendance

Operational guide for working in this app. For how and why the system is built the
way it is, see [`ARCHITECTURE.md`](ARCHITECTURE.md). For dated fix history, see
[`CHANGES.md`](CHANGES.md).

This app implements a parallel "Flexible Hours" attendance pipeline (Employee
Checkin → Attendance → shortfall Penalty), a grace-period-aware fix for an HRMS
shift-expiry race condition, and Egyptian Labour Law HR compliance email alerts.

## Environment

All `bench` commands run from `/home/frappe/frappe-bench/`, not from `apps/`.

```bash
cd /home/frappe/frappe-bench

# Run all tests for this app
bench --site [site-name] run-tests --app employee_custom_attendance

# Run a single test module
bench --site [site-name] run-tests --app employee_custom_attendance \
  --module employee_custom_attendance.attendance.test_daily_job
```

## Linting

```bash
cd apps/employee_custom_attendance
pre-commit run --all-files
ruff check . --fix
```

## Bench-Execute Utilities

These are manual recovery/maintenance scripts — none are wired to a hook or the
scheduler. All are idempotent, safe to re-run.

```bash
# Reprocess a date range for all active Flexible Hours employees (deletes &
# rebuilds Attendance in that range — two-phase to avoid overnight double-counting)
bench --site [site] execute employee_custom_attendance.attendance.daily_job.reprocess_all \
  --args "['2026-06-16', '2026-06-18']"

# Reset and reprocess one employee/date
bench --site [site] execute employee_custom_attendance.attendance.daily_job.reset_and_reprocess \
  --args "['HR-EMP-00079', '2026-06-16']"

# Backfill Attendance for bulk-imported Leave Applications that skipped on_submit
bench --site [site] execute employee_custom_attendance.attendance.daily_job.backfill_on_leave_attendance

# Backfill Attendance for bulk-imported Attendance Requests that skipped on_submit
bench --site [site] execute employee_custom_attendance.attendance.daily_job.backfill_attendance_requests

# Survey Employee Checkins broken by the shift-expiry race (read-only)
bench --site [site] execute employee_custom_attendance.attendance.backfill_shift_expiry.scope

# Repair them (reactivates assignments, re-resolves shifts, reprocesses attendance)
bench --site [site] execute employee_custom_attendance.attendance.backfill_shift_expiry.run
```

## Gotchas

- **HRMS's stock `mark_expired_shift_assignments_as_inactive` job is force-disabled**
  by `install.py` on every `after_install`/`after_migrate`, in favor of this app's
  grace-period-aware replacement in `shift_expiry.py`. Don't re-enable it — it has a
  midnight race condition that permanently strands post-midnight checkouts. See
  `ARCHITECTURE.md` → Design Decisions #4.
- **`Flexible Hours Settings.job_run_time` is dead config.** It's a Time field with
  no code reading it anywhere in the app. The scheduler always runs on Frappe's
  normal daily cadence regardless of this value — don't expect changing it to do
  anything.
- **`Employee Penalty.reason` is mandatory.** `_create_shortfall_penalty` sets it
  explicitly; if you add another call site that creates an `Employee Penalty`, don't
  drop that field — a prior bug (fixed 2026-06-24) had `penalty.insert()` silently
  fail with `MandatoryError` inside a swallowed exception handler, so Attendance was
  created but penalties were not.
- **Missing-fingerprint counting uses the payroll permission period (e.g. 21→20),
  not the calendar month** — driven by `Permission Settings.period_start_day` /
  `period_end_day` (shared with `employee_permission`). Falls back to calendar month
  only when `period_start_day <= 1`.
- **The Shift Type must be named exactly `"Flexible Hours"`.** It's a hardcoded
  string literal across `checkin.py`, `daily_job.py`, and `employee_hooks.py`. Don't
  rename it in the UI without updating all three.
- **The daily job processes *yesterday* by default**, not today —
  `process_flex_attendance(process_date=None)` defaults to `today() - 1 day` so a
  full day's checkins are in before it's finalized.
- **Everything in the daily pipeline is idempotent by design** (re-running skips
  employees who already have an Attendance for that date, skips penalties that
  already exist) — safe to trigger the scheduler methods manually if a run is
  suspected to have failed partway.
