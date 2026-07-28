# Architecture — employee_custom_attendance

## Architecture Diagram

```
Employee Checkin (punch in/out)
       │
       ▼
Employee Checkin.validate  ──────────────► skip_auto_attendance = 1 (if shift = Flexible Hours)
       │
       │  (raw checkins accumulate through the day)
       ▼
process_flex_attendance  ◄── Daily Scheduler (scheduler_events.daily)
       │
       ├─► Already has Attendance today?  ──yes──► skip (idempotent)
       │
       ├─► Approved Leave covers date?    ──yes──► Leave Application.update_attendance() → done
       │
       ├─► Holiday?                       ──yes, no checkins──► skip silently, no Attendance
       │
       ├─► Pair Checkins (drop odd trailing entry, handle overnight window)
       │
       ├─► No usable checkins? ──► _handle_missing_fingerprint
       │                             └─ ≥3 missing this permission period → Half Day, else Absent
       │
       ├─► Calculate Hours (calculate_working_hours, capped at daily_max_hours)
       │
       ├─► Create Attendance (Present / Absent) + link checkins
       │
       ├─► Absent & shortfall? ──► Create Employee Penalty (Draft, for HR review)
       │
       └─► Send Notifications (all active employees, not just Flex)
             ├─ notify_lateness_cap   (>5h late this month)
             └─ notify_article_69    (20 non-consec / 12mo, or 10-day streak)

Separately, each day:
Shift Assignment lifecycle:
  Employee.on_update (custom_attendance_system changed) → create/end open Shift Assignment
  mark_expired_shift_assignments_as_inactive (daily) → deactivates assignments only after
    shift end + checkout grace period (replaces HRMS's stock job, which races checkouts at midnight)
```

## Overview

This app layers a parallel **"Flexible Hours" attendance system** on top of HRMS for
employees who don't work fixed shift hours, while a few of its pieces (shift-expiry
handling, HR compliance notifications) apply to **all** employees regardless of
attendance system. It does not replace HRMS's standard attendance flow — Standard
employees are untouched by the flex pipeline and continue through HRMS's normal
Shift Type auto-attendance.

Three concerns, one app:

1. **Flexible Hours attendance** — turn raw Employee Checkins into daily Attendance
   records and shortfall Penalties for employees who don't have fixed shift hours.
2. **Shift-expiry correctness** — a grace-period-aware replacement for an HRMS stock
   job that has a midnight race condition affecting all shift-based employees.
3. **HR compliance alerting** — Egyptian Labour Law lateness-cap and Article 69
   absence-threshold email notifications, for all active employees.

## Design Decisions

Why the system is built this way, not just what the code does:

1. **Daily scheduler instead of processing on each Employee Checkin.** A day isn't
   "complete" until check-out, and pairing logic (odd-checkin drop, overnight-window
   handling) needs the full day's checkins at once. Batching once, after the day is
   over, avoids re-evaluating on every punch and mirrors how HRMS itself batches
   standard-shift auto-attendance.

2. **Flexible Hours employees get a dedicated, auto-managed Shift Assignment**, not
   just the `custom_attendance_system` flag. HRMS's `Employee Checkin.fetch_shift()`
   only resolves a shift by querying active Shift Assignments; the custom field alone
   is just an intent marker — without a real assignment, checkins never wire into
   HRMS's shift machinery at all.

3. **`skip_auto_attendance` is explicitly set on flex checkins**, and it's not just a
   flag for HRMS — it's the literal filter `_get_checkins` uses to find "checkins
   this pipeline should process" versus checkins HRMS auto-processes normally.
   Setting it is how the two systems stay non-overlapping.

4. **HRMS's stock shift-expiry job is disabled and replaced.** HRMS deactivates a
   Shift Assignment purely by comparing `end_date` to today's calendar date, ignoring
   the shift's checkout grace period. That races against
   `Employee Checkin.fetch_shift()`, which only resolves shifts via Shift Assignments
   with `status="Active"`. If HRMS's job deactivates yesterday's assignment before a
   post-midnight checkout (still inside its grace window) is saved, that checkin
   permanently loses its shift/attendance link. `shift_expiry.py` only deactivates an
   assignment once its shift, *including the grace period*, has actually finished.

5. **Shortfall penalties are created as Draft, never auto-submitted.** A financial
   deduction needs a human (HR) to review before it becomes binding — the daily job
   only proposes it.

6. **Missing-fingerprint counting uses the payroll permission period (e.g. 21→20),
   not the calendar month.** It aligns disciplinary counting with the company's
   actual pay-cycle boundaries, sharing configuration (`Permission Settings`) with the
   `employee_permission` app rather than an arbitrary calendar month.

7. **Leave/attendance-request backfills reuse `update_attendance()` /
   `create_attendance_records()` instead of reimplementing that logic.** This keeps
   behavior identical to the normal `on_submit` path and self-heals records that were
   bulk-imported without ever triggering those hooks.

8. **`reprocess_all` deletes everything in the date range before reprocessing
   anything (two-phase).** A single-pass reprocess risks double-counting checkins
   that straddle midnight, since an overnight checkin could otherwise be claimed by
   two different day-processing runs before both are cleanly unlinked.

9. **HR compliance notifications (lateness cap, Article 69) ride on the flex-only
   daily job even though they apply to every employee.** This avoids standing up a
   second scheduled job — the existing one already runs at the right cadence.

## Daily Scheduler Order

Two jobs are registered under `scheduler_events.daily` in `hooks.py`:

1. `process_flex_attendance`
2. `mark_expired_shift_assignments_as_inactive`

Frappe does **not** preserve this list order at runtime: `enqueue_events()` in
`frappe/utils/scheduler.py` shuffles all due Scheduled Job Types (`random.shuffle`)
and enqueues each as an independent background job. These two can run in either
order, or concurrently across workers.

This is safe here because neither job depends on the other's output within the same
day:

- `process_flex_attendance` only reads/writes Employee Checkin, Attendance, Leave
  Application, Holiday.
- `mark_expired_shift_assignments_as_inactive` only writes `Shift Assignment.status`.

There is no ordering dependency to protect.

Within `process_flex_attendance` itself, execution **is** sequential (single job):

1. Every active Flexible Hours employee → `process_employee_for_date` (creates
   Attendance, and a Draft shortfall Penalty if applicable).
2. Every active employee (Standard + Flexible) → `notify_lateness_cap`, then
   `notify_article_69`.

## Assumptions

The code relies on the following, none of which are enforced by validation —
violating them fails silently or produces wrong data rather than raising an error:

1. **Exactly one Shift Type is named "Flexible Hours".** The name is a hardcoded
   string literal in `checkin.py`, `daily_job.py`, `employee_hooks.py`, and seeded by
   `install.py`. Renaming or duplicating it breaks flex detection everywhere with no
   error.
2. **At most one open-ended (`end_date` unset) Flexible Hours Shift Assignment exists
   per employee at a time.** `_ensure_flex_assignment` only checks existence, not
   uniqueness — manually created duplicates are not detected.
3. **Checkins for a shift strictly alternate IN/OUT.** `calculate_working_hours` is
   called with `"Alternating entries as IN and OUT"` — a double punch of the same
   type (e.g. two INs in a row from a flaky device) desyncs pairing for the rest of
   the day, not just that pair.
4. **A flex shift never runs past noon the day after it starts.** The checkin window
   is hardcoded to `date 00:00` → `(date+1) 12:00`. A shift that legitimately checks
   out after noon the next day is silently excluded.
5. **Once an Attendance record exists for employee+date, that day is "settled".**
   `process_employee_for_date` skips entirely if any Attendance already exists,
   regardless of who/what created it or its status.
6. **`Employee.holiday_list` is authoritative and, if unset, the employee is assumed
   to never be on holiday** (`_is_holiday` returns `False` silently).
7. **`Flexible Hours Settings` and `Permission Settings` are single, global
   Singles** — one configuration applies to every employee and company on the site;
   there's no per-company or per-branch override.
8. **`penalty_type_for_shortfall`, if configured, points to a Penalty Type whose
   rules make sense for a per-day shortfall** (vs. `employee_penalty`'s original
   occurrence-count model) — nothing validates this at either end.
9. **The daily scheduler runs every day without extended gaps.** There's no automatic
   catch-up if the scheduler is paused/disabled for a period — only the manual
   `reprocess_all` script recovers missed days.
10. **`skip_auto_attendance=1` exclusively marks "belongs to flex processing".**
    `_get_checkins` filters on it, so any other code path that sets or clears this
    flag on Employee Checkin would silently steal or hide checkins from this
    pipeline.

## Failure Scenarios

| Scenario | Behavior | Where |
|---|---|---|
| Missing OUT checkin (odd checkin count) | Trailing unpaired checkin dropped; pairs on the rest. If nothing pairable remains, falls through to missing-fingerprint handling | `process_employee_for_date` |
| No checkins at all | Holiday → skip silently, no Attendance created. Not a holiday → missing-fingerprint counter runs (≥3 this permission period → Half Day, else Absent) | `_mark_absent_or_skip_holiday`, `_handle_missing_fingerprint` |
| Overnight shift (e.g. 20:00 IN → 02:00 OUT next day) | Checkin window extends to noon next day so the OUT is captured under the shift's start date; once linked, the `attendance: not set` filter excludes it from bleeding into next day's window too | `_get_checkins` |
| Approved Leave covers the date | Custom processing skipped entirely; delegates to `Leave Application.update_attendance()` for consistent leave_type/half-day handling | `process_employee_for_date` |
| Holiday, no checkins | Skipped silently — no Attendance record created at all | `_mark_absent_or_skip_holiday` |
| Holiday, but employee has checkins anyway (worked overtime) | Holiday check is never reached — normal hours calculation and Attendance creation proceed as usual | `process_employee_for_date` |
| Duplicate/re-run of the scheduler for the same date | No-op — function returns immediately if an Attendance already exists for employee+date, regardless of its status | `process_employee_for_date` (top guard) |
| Shortfall penalty already exists for employee+date+type | Not recreated — existence check short-circuits before `insert()` | `_create_shortfall_penalty` |
| Hours below `daily_min_hours` but not zero (partial shortfall) | Status = Absent (binary Present/Absent on hours; Half Day only comes from the missing-fingerprint counter, not partial hours) | `process_employee_for_date` |
| Hours exceed `daily_max_hours` | Capped at `daily_max_hours` before being stored — never inflates recorded working hours | `process_employee_for_date` |
| One employee's processing throws an exception | Caught and logged per-employee (`frappe.log_error`); loop continues to the next employee, rest of the run is unaffected | `process_flex_attendance` |
| One employee's notification check throws an exception | Same pattern — caught, logged, loop continues | `process_flex_attendance` |
| Checkin's shift never resolves (shift-expiry race, pre-fix) | Not self-healing — requires the one-off `backfill_shift_expiry.scope()`/`.run()` | `backfill_shift_expiry.py` |
| Leave Application / Attendance Request bulk-imported without triggering `on_submit` | No Attendance exists until manually recovered | `backfill_on_leave_attendance`, `backfill_attendance_requests` |

## Doctypes & Custom Fields

- **`Flexible Hours Settings`** (Single) — `weekly_target_hours`, `daily_min_hours`,
  `daily_max_hours`, `penalty_type_for_shortfall` (Link to Penalty Type),
  `job_run_time` (Time — **defined but not read by any code**; changing it has no
  effect on when the scheduler actually runs).
- **`Employee.custom_attendance_system`** (Select: Standard / Flexible Hours,
  default Standard) — drives which pipeline an employee is processed by.
- **`Employee.custom_lateness_cap_notified`** (Data, hidden) — stores the month
  (`YYYY-MM-DD` of month start) the lateness-cap email was last sent, so it fires at
  most once per month per employee.
- **`Attendance.custom_missing_fingerprint`** (Check, hidden) — marks an
  Absent/Half Day Attendance that was created because no checkins were found, as
  opposed to checkins existing but falling short of `daily_min_hours`.

## Hook Wiring

From `hooks.py`:

```python
doc_events = {
    "Employee Checkin": {"validate": "...checkin.on_checkin_validate"},
    "Employee": {"on_update": "...employee_hooks.on_employee_update"},
}

scheduler_events = {
    "daily": [
        "...daily_job.process_flex_attendance",
        "...shift_expiry.mark_expired_shift_assignments_as_inactive",
    ]
}
```

`install.py` seeds a `Shift Type` named "Flexible Hours" on `after_install`
(00:00–23:59, `enable_auto_attendance=0`, working hours based on "Every Valid
Check-in and Check-out"), and force-stops HRMS's stock
`mark_expired_shift_assignments_as_inactive` Scheduled Job Type on both
`after_install` and `after_migrate` — the latter to cover sites where the job record
didn't exist yet when hooks were first synced (`frappe.get_hooks` sync only touches
frequency/cron format on an *existing* record).

## Flex Attendance Daily Pipeline (`daily_job.py`)

`process_flex_attendance(process_date=None)` is the scheduler entry point. It
defaults to processing *yesterday* (`today() - 1 day`), so a day's attendance is
finalized the day after it happens, once all checkins for it are in.

For each active employee with `custom_attendance_system = "Flexible Hours"`,
`process_employee_for_date` runs the full decision tree shown in the diagram above
and detailed in "Failure Scenarios". Key internals:

- **`_get_checkins`** — window is `date 00:00` → `(date+1) 12:00` to capture
  overnight shifts, filtered to `skip_auto_attendance=1` and `attendance: not set`
  so already-claimed checkins aren't double-counted on a later run.
- **`_get_permission_period`** — reads `period_start_day`/`period_end_day` from the
  shared `Permission Settings` doctype to compute the payroll period (e.g. 21→20)
  used for missing-fingerprint counting; falls back to the calendar month when
  `period_start_day <= 1`.
- **`_handle_missing_fingerprint`** — counts prior `custom_missing_fingerprint`
  Attendance records within the current permission period; 3 or more → today is
  Half Day, otherwise Absent.
- **`_create_shortfall_penalty`** — creates a Draft `Employee Penalty` (never
  auto-submitted) with a mandatory `reason` field (see `CHANGES.md` for the bug this
  fixed) when an Absent day has `penalty_type_for_shortfall` configured.

## Shift Lifecycle Management (`employee_hooks.py`, `shift_expiry.py`)

- **`on_employee_update`** fires on every `Employee.on_update`. If
  `custom_attendance_system` is "Flexible Hours", ensures one open-ended Flexible
  Hours Shift Assignment exists (creates one starting today if none does). Otherwise,
  end-dates any open Flexible Hours Shift Assignment as of today. This keeps HRMS's
  shift-resolution machinery in sync with the custom field automatically — no manual
  Shift Assignment management needed by HR.
- **`mark_expired_shift_assignments_as_inactive`** replaces HRMS's stock job (see
  "Design Decisions" #4). It only flips a Shift Assignment to Inactive once
  `now >= actual_end_datetime`, where `actual_end_datetime` accounts for the shift's
  end time, midnight rollover, and `allow_check_out_after_shift_end_time` grace
  minutes.
- **`backfill_shift_expiry.py`** is a one-off, manually-run repair script (not
  wired to any hook/scheduler) for Employee Checkins that already lost their
  shift/attendance link to the race condition *before* this fix existed.
  `scope()` reports affected checkins read-only; `run()` temporarily reactivates the
  relevant Shift Assignments, re-saves the checkins so HRMS resolves their shift,
  routes Flexible Hours checkins through `process_employee_for_date`, routes other
  shift types through `ShiftType.process_auto_attendance()`, then re-settles
  assignment statuses.

## Notifications (`notifications.py`)

Both run for **every active employee**, regardless of `custom_attendance_system`,
called from inside `process_flex_attendance`'s daily run:

- **`notify_lateness_cap`** — sums the month's lateness. For Flexible Hours
  employees, that's `Σ (daily_min_hours - working_hours)` across Absent/Half Day
  days below the minimum; for Standard employees, it's late-entry minutes computed
  against `Shift Type.start_time`. Above 5 hours for the month → one email to HR
  Manager/HR User role holders, gated by `custom_lateness_cap_notified` so it fires
  at most once per employee per month.
- **`notify_article_69`** — Egyptian Labour Law Article 69 (absence as implied
  resignation): 20 non-consecutive Absent days in a rolling 12 months, or a 10-day
  consecutive Absent streak. No per-employee de-duplication — this can email HR again
  on a later run if the condition is still true (unlike the lateness-cap check).

Recipients are resolved by `_get_hr_recipients`: every user holding the HR Manager
or HR User role, deduplicated.

## Backfill & Maintenance Utilities

All are `bench execute`-only — none are wired to a hook or scheduled job:

- **`reprocess_all(from_date, to_date)`** — two-phase reset-then-reprocess across a
  date range for all active flex employees (see Design Decision #8).
- **`reset_and_reprocess(employee, date)`** — same reset-then-reprocess for one
  employee/date.
- **`backfill_on_leave_attendance()`** — re-runs `Leave Application.update_attendance()`
  for every approved, submitted Leave Application, for all employees.
- **`backfill_attendance_requests()`** — re-runs
  `Attendance Request.create_attendance_records()` for every submitted Attendance
  Request, for all employees.
- **`backfill_shift_expiry.scope()` / `.run()`** — see "Shift Lifecycle Management"
  above.

All are explicitly documented as idempotent — safe to re-run.

## Reporting & Workspace

- **`Flex Weekly Hours Summary`** (script report) — buckets submitted Flexible Hours
  Attendance into Monday-start weeks per employee, comparing total hours against
  `weekly_target_hours` and counting days below `daily_min_hours`.
- **`Flexible Attendance`** workspace (under the HR parent page) — shortcuts to
  Attendance, Employee Checkin, Flexible Hours Settings, and the weekly report above.

## Integration Points

- **`employee_penalty`** — shortfall penalties are plain `Employee Penalty`
  documents; this app only creates them as Draft. The `reason` field is mandatory on
  that doctype (see `CHANGES.md`, 2026-06-24 entry #2, for the bug this caused).
- **`employee_permission`** — reads the shared `Permission Settings` Single
  (`period_start_day`/`period_end_day`) to align missing-fingerprint counting with
  the payroll permission period rather than the calendar month.
- **HRMS** — depends on `Employee Checkin.fetch_shift()`,
  `calculate_working_hours()`, `update_attendance_in_checkins()`,
  `Leave Application.update_attendance()`, `Attendance Request.create_attendance_records()`,
  `Shift Type.process_auto_attendance()`, and `get_shift_type()` from `hrms.hr.doctype.*`.
