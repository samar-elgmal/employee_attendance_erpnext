# Changes — employee_custom_attendance

## 2026-07-27

### Added ARCHITECTURE.md and CLAUDE.md

The app's `hooks.py`/README gave no indication of its actual scope — it had grown
into a full Flexible Hours attendance pipeline, a fix for an HRMS shift-expiry race
condition, and HR compliance email alerts, none of which were documented anywhere.

Added `ARCHITECTURE.md` (data-flow diagram, design decisions, daily scheduler
ordering, assumptions, failure-scenario table, module-by-module reference) and
`CLAUDE.md` (bench commands, lint commands, gotchas) at the app root.

---

## 2026-06-24

### 1. Permission-period-aware missing-fingerprint count

**File:** `employee_custom_attendance/attendance/daily_job.py`

Added helper `_get_permission_period(date)` that reads `period_start_day` and `period_end_day` from the `Permission Settings` doctype (shared with the `employee_permission` app).

With the company configured as **21 → 20** (e.g. Jun 21 – Jul 20), the missing-fingerprint day count in `_handle_missing_fingerprint` now spans the correct permission period instead of the calendar month.

| Date | Period before fix | Period after fix |
|------|------------------|-----------------|
| Jun 5 | Jun 1 – Jun 30 | May 21 – Jun 20 |
| Jun 25 | Jun 1 – Jun 30 | Jun 21 – Jul 20 |

Falls back to the calendar month when `period_start_day <= 1`.

---

### 2. Fixed: shortfall penalties silently not created

**File:** `employee_custom_attendance/attendance/daily_job.py`

`_create_shortfall_penalty` was missing the mandatory `reason` field on `Employee Penalty`, causing every `penalty.insert()` call to raise `MandatoryError`. The exception was swallowed by `reprocess_all`'s error handler, so attendance records were created correctly but no penalties were ever saved.

**Fix:** added `"reason": f"Flexible hours shortfall on {date}"` to the document dict.

To backfill missing penalties for Jun 22–24, rerun:

```bash
bench --site milaerp.milaserv.com execute \
  employee_custom_attendance.attendance.daily_job.reprocess_all \
  --args "['2026-06-22', '2026-06-24']"
```
