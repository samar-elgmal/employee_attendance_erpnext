# Session-Based Flex Attendance Attribution — Design

## Problem

`process_employee_for_date` (`daily_job.py`) attributes a Flexible Hours
employee's working hours to a date by fetching all Employee Checkins in the
window `date 00:00 → (date+1) 12:00` and pairing them **positionally**
(1st+2nd, 3rd+4th, ...), then truncating a trailing odd entry. This is not
session-aware: it doesn't know where one shift ends and the next begins, only
where the window happens to end.

Concretely, this silently misattributes hours whenever a checkin count
happens to work out even. Example:

```
1/7 09:00 IN
1/7 17:00 OUT      (real 1/7 session, 8h)

2/7 05:00 IN
2/7 07:00 OUT       (real 2/7 session, 2h — unrelated to 1/7's shift,
                      just an early second job that day)
```

Both fall inside 1/7's window (window end is noon 2/7). Since the total count
is even (4), no trailing entry is dropped, so today's naive pairing computes
`(09:00,17:00)=8h` **and** `(05:00,07:00)=2h`, attributing all 10h to 1/7.
The 2/7 checkins get linked to 1/7's Attendance
(`update_attendance_in_checkins`), so when 2/7 is processed the next day, its
own genuine session is already claimed — 2/7 shows Absent even though the
employee worked.

The fix: pair checkins into sessions first, *then* decide which date each
session belongs to (its IN date) — never truncate by window position before
pairing is complete.

## Goals

- A session's hours are always attributed to its **IN date**, regardless of
  checkin count parity in any day's fetch window.
- Support multiple sessions per day and overnight (and multiple-overnight)
  sessions without special-casing.
- Never double-claim or lose a checkin across two days' runs.
- Tolerate device noise (duplicate punches) without desyncing the rest of a
  window's pairing.
- Detect (not silently miscompute) a pairing desync that noise-dedup didn't
  catch, and contain its blast radius to the point it occurs.
- Keep the existing per-`(employee, date)` entrypoint, scheduler contract,
  and `reprocess_all` / `reset_and_reprocess` signatures unchanged — this is
  a targeted algorithm fix, not a pipeline rewrite.

## Non-goals

- No global, cross-day session-building pipeline (`fetch → pair → group by
  date → create all attendance` in one pass). Rejected in favor of keeping
  the existing daily-scheduler-per-date architecture (see
  `ARCHITECTURE.md` Design Decision #1) — smaller diff, no change to how
  the scheduler, `reprocess_all`, or `reset_and_reprocess` are invoked.
- No validation against `Employee Checkin.log_type`. It is not trustworthy
  on this system's devices (this is *why* the existing code already uses
  HRMS's "Alternating entries" pairing mode instead of "Strictly based on
  Log Type") — no logic here may assume it's accurate.
- No new doctype for anomaly tracking. Reuses `frappe.log_error`, consistent
  with existing per-employee error handling in `process_flex_attendance`.
- No break-time/overtime rules. Out of scope; the `Session` object is
  introduced partly because it makes such rules easy to add *later*, not
  because they're needed now.

## Design

### Session

Replace the flat "list of checkin logs" that flows through the pipeline with
an explicit unit:

```python
class Session(NamedTuple):
    in_log: dict       # Employee Checkin row (name, time)
    out_log: dict
    duration: float     # hours
    attendance_date: date   # in_log.time's date — the attribution key
```

Everything downstream of pairing (totals, `in_time`/`out_time`, which
checkins get claimed) operates on `Session` objects, not raw logs.

### Window (`_get_checkins`)

```python
day_start = date 00:00
grace_hours = Flexible Hours Settings.checkin_window_grace_hours  # default 12
day_end = (date + 1 day) + grace_hours
```

New Single field: `Flexible Hours Settings.checkin_window_grace_hours` (Int,
default `12`). At the default, `day_end` is `(date+1) 12:00` — identical to
today's hardcoded noon cutoff, so behavior doesn't get wider by default; it's
just no longer a magic constant. Ops can raise it if a real shift legitimately
needs a longer window, without a code change.

Correctness no longer depends on this bound being tight — pairing happens
before date-filtering (see below), so a same-day-but-later next-day session
inside the window can never be merged into today's total by coincidence. The
setting exists purely to bound how much data a single run reads.

Same filters as today: `employee`, `time between [day_start, day_end]`,
`skip_auto_attendance=1`, `attendance: not set`, ordered by `time asc`.

### Dedup (`_dedupe_checkins`)

```python
def _dedupe_checkins(logs, threshold_minutes=2):
    kept = []
    for log in logs:
        if kept and (log.time - kept[-1].time) <= timedelta(minutes=threshold_minutes):
            continue
        kept.append(log)
    return kept
```

Collapses **any** two consecutive checkins within 2 minutes into one (keep
the first), regardless of `log_type` — since `log_type` can't be trusted to
tell a true duplicate scan from a real fast IN/OUT, a type-agnostic gap
check is the only reliable signal available. Runs once, immediately after
fetch, before pairing.

### Build Sessions (`_build_sessions`)

```python
MIN_SESSION_HOURS = 10 / 60   # 10 minutes
MAX_SESSION_HOURS = 20

def _build_sessions(logs, employee):
    sessions = []
    i = 0
    while i + 1 < len(logs):
        in_log, out_log = logs[i], logs[i + 1]
        duration = time_diff_in_hours(in_log.time, out_log.time)
        if duration < MIN_SESSION_HOURS or duration > MAX_SESSION_HOURS:
            frappe.log_error(
                f"{in_log.time} -> {out_log.time} ({duration:.2f}h) is outside "
                f"a plausible single-session range — likely a desynced double-punch "
                f"dedup didn't catch. Stopping session-build for this window; "
                f"remaining checkins for {employee} left unclaimed for manual review.",
                "Flex attendance pairing anomaly",
            )
            break
        sessions.append(Session(in_log, out_log, duration, getdate(in_log.time)))
        i += 2
    return sessions
```

Walks the deduped, chronological list positionally (same alternating
assumption as today — `log_type` still isn't used). The only new signal is
duration plausibility: a pair whose gap is too short (a same-type near-punch
dedup didn't catch, e.g. two INs 6 minutes apart) or absurdly long (>20h,
almost certainly a desync elsewhere) stops session-building **at that
point** — sessions already built earlier in the window are kept; the
anomalous pair and everything after it in this window are left unclaimed
(not linked to any Attendance), available for manual review and reprocessing
via `reset_and_reprocess` once the underlying data issue is understood.

A trailing unmatched single log (odd count) is simply never reached by the
`i + 1 < len(logs)` loop bound — same effective behavior as today's
drop-the-trailing-entry, just falling out naturally instead of via a
separate truncation step.

### Filter by IN date, calculate, claim (`process_employee_for_date`)

```python
raw_logs = _get_checkins(employee, date)          # fetch + dedupe
if not raw_logs:
    _mark_absent_or_skip_holiday(...)
    return

sessions = _build_sessions(raw_logs, employee)
day_sessions = [s for s in sessions if s.attendance_date == date]

if not day_sessions:
    _mark_absent_or_skip_holiday(...)
    return

total_hours = sum(s.duration for s in day_sessions)
in_time = day_sessions[0].in_log.time
out_time = day_sessions[-1].out_log.time
effective_hours = min(total_hours, settings.daily_max_hours)
status = "Present" if effective_hours >= settings.daily_min_hours else "Absent"

attendance = _create_attendance(employee, date, status, effective_hours, in_time, out_time)
claimed = [log.name for s in day_sessions for log in (s.in_log, s.out_log)]
update_attendance_in_checkins(claimed, attendance.name)
```

Only checkins belonging to `day_sessions` (IN date == `date`) get linked to
this Attendance. Sessions whose IN date differs (a next-day session that
happened to fall inside this window) are left untouched — unclaimed,
`attendance` still unset — so that date's own future run fetches and pairs
them fresh, correctly.

This drops the call to HRMS's `calculate_working_hours` — sessions already
carry precomputed durations, so summing them directly is simpler than
re-flattening into a log list and calling back into a helper built for raw
lists. `time_diff_in_hours` (used inside `_build_sessions`) is imported
directly from `hrms.hr.doctype.employee_checkin.employee_checkin`, same
module the app already depends on.

### `_reset_attendance` window consistency

`_reset_attendance` independently rebuilds `day_start`/`day_end` to re-arm
`skip_auto_attendance` before a reprocess. Extract the window-bound
computation (`_get_checkins`'s `day_start`/`day_end` logic, including the new
`checkin_window_grace_hours` read) into one shared helper so both call sites
stay in sync — today they're duplicated literals and would silently drift if
only one were updated.

## Failure scenarios (updates to `ARCHITECTURE.md`)

| Scenario | New behavior |
|---|---|
| Next-day early session falls inside today's window | Never merged into today's total, regardless of checkin count parity — filtered out by IN date after pairing, left unclaimed for its own day |
| Overnight session (single or back-to-back multiple) | Attributed entirely to its IN date; supported natively by pair-then-filter, no window-edge special casing |
| Device double-scan within 2 min | Collapsed by dedup before pairing, regardless of type |
| Pairing desync not caught by dedup (e.g. two real-but-close punches of unknown type) | Detected via implausible session duration (<10min or >20h); session-building stops at that point, anomaly logged via `frappe.log_error`, sessions before it are kept, everything from the anomaly onward is left unclaimed |
| Missing OUT (trailing unmatched IN) | Same as today: not included in any session, stays unclaimed until a future run finds its OUT |

Also update:
- **Assumption #4** ("shift never runs past noon the day after it starts")
  → replaced by the configurable `checkin_window_grace_hours` (default 12,
  same effective default as today).
- **Assumption #3** ("checkins strictly alternate IN/OUT... a double punch
  desyncs pairing for the rest of the day") → narrowed: dedup handles
  near-simultaneous duplicates; the duration-anomaly check contains (not
  eliminates) desyncs beyond that, to the point they occur rather than the
  rest of the day.

## Rollout

After deploy, run `reprocess_all` (existing bench-execute utility, already
documented in `CLAUDE.md`) over any historical date range suspected of the
old cross-day contamination bug — dates where an Absent Flexible Hours
employee might actually have worked a genuine early/late session that got
silently absorbed into a neighboring day. No new tooling needed; this is
already idempotent and two-phase (delete-then-reprocess) by design.

## Testing

Extend `test_daily_job.py`:

- Contamination case (the bug this design fixes): a 1/7 session plus a
  same-day-parity 2/7 early session that would previously coincide to an
  even total count → 1/7 gets only its own hours; 2/7, processed on its own
  run, gets its own hours (not zero/Absent).
- Multiple-overnight-in-a-row: `1/7 22:00→2/7 01:00`, `2/7 10:00→2/7 12:00`,
  `2/7 15:00→2/7 18:00` → 1/7 = 3h; 2/7 = 2h + 3h = 5h.
- Dedup: a same-type duplicate within 2 min collapses to one checkin and
  doesn't affect the resulting session.
- Duration-anomaly: `08:00 IN, 08:06 IN, 17:00 OUT` (dedup doesn't catch it,
  6 min > 2 min threshold) → no session built past the anomaly, no
  Attendance created from a bogus 6-minute pairing, `frappe.log_error`
  called.
- `test_holiday_not_marked_absent_by_next_day_checkin_bleed` (existing) must
  still pass unchanged — a single dangling next-day IN with no OUT yields no
  sessions for the holiday date either way.

## Files touched

- `employee_custom_attendance/attendance/daily_job.py` — `_get_checkins`,
  new `_dedupe_checkins`, new `_build_sessions`, `process_employee_for_date`,
  `_reset_attendance` (shared window-bound helper).
- `employee_custom_attendance/employee_custom_attendance/doctype/flexible_hours_settings/` —
  add `checkin_window_grace_hours` field (doctype JSON + migration).
- `employee_custom_attendance/attendance/test_daily_job.py` — new test cases
  above.
- `ARCHITECTURE.md` — update Assumptions #3/#4 and the Failure Scenarios
  table as described above.
